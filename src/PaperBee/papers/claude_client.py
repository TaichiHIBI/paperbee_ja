"""Claude API (Anthropic) ラッパー。

PaperBee では Claude を「件数は少ないが精度が効く工程」に使う:

- LLMフィルタリングの最終判定（複数論文を1リクエストにまとめ、構造化出力で判定）
- 日本語要約の生成（SUMMARIZATION_PROVIDER / TRANSLATION_PROVIDER に "claude" を指定した場合）

件数の多い一次ふるいはローカルLLM（Ollama）が担当する（llm_filtering.py 参照）。

利用量は LOCAL_ROOT_DIR/claude_usage.csv に1リクエスト1行で記録し、当月の推定支出が
CLAUDE_MONTHLY_BUDGET_USD に達したら以降の呼び出しを止める（呼び出し側はローカルLLMへ
フォールバックする）。トピック別の設定ファイルは同じ LOCAL_ROOT_DIR を共有するので、
予算は全トピック合算で管理される。
"""

import csv
import os
from datetime import datetime
from time import sleep
from typing import Any, Dict, List, Optional, Sequence, Tuple

import anthropic
from pydantic import BaseModel

CLAUDE_PROVIDERS = ("claude", "anthropic")
DEFAULT_MODEL = "claude-opus-5-5"
DEFAULT_EFFORT = "low"

# 安全分類器が拒否したリクエストをサーバー側で別モデルに回す機能（fallbacks="default"）の対応モデル。
# 生物系のアブストラクトは bio 分類器に誤って拒否されることがあるため、対応モデルでは常に有効にする。
_SERVER_FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5", "claude-fable-5-1"}
_SERVER_FALLBACK_BETA = "server-side-fallback-2026-07-01"

# USD / 1M tokens: (input, output, cache_read)。キャッシュ書き込み（5分TTL）は input の1.25倍。
# 未知のモデルは予算ガードが甘くならないよう最上位ティア相当で見積もる。
_PRICING: Dict[str, Tuple[float, float, float]] = {
    "claude-opus-5-5": (4.00, 20.00, 0.20),
    "claude-sonnet-5-5": (2.00, 10.00, 0.20),
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
    "claude-opus-5": (5.00, 25.00, 0.50),
    "claude-sonnet-5": (2.00, 10.00, 0.20),
}
_UNKNOWN_MODEL_PRICING = (10.00, 50.00, 1.00)
_CACHE_WRITE_MULTIPLIER = 1.25

_LEDGER_FIELDS = (
    "timestamp", "tag", "purpose", "model", "input_tokens", "output_tokens",
    "cache_creation_input_tokens", "cache_read_input_tokens", "cost_usd",
)

FILTER_SYSTEM_PROMPT = """You screen newly published research papers for a research group's daily paper feed. The group's selection criteria follow.

<criteria>
{criteria}
</criteria>

The user message contains several papers, each wrapped in a <paper id="..."> tag. Judge each paper on its own against the criteria. Base the decision mainly on the title and abstract, and use the journal, authors and keywords as supporting evidence. If the criteria ask for a yes/no answer, give that answer as `relevant`. For each paper, give the reason as one short Japanese sentence (under 60 characters) naming the deciding factor, such as the lab and the methodological contribution; it is shown to readers next to each selected paper. Return exactly one decision for every paper id."""


class BudgetExceededError(RuntimeError):
    """当月の Claude 推定支出が予算に達した。"""


class ClaudeCallError(RuntimeError):
    """Claude 呼び出しが使える結果を返さなかった（API エラー、拒否、出力の打ち切りなど）。

    permanent=True は認証・モデル名・残高不足など設定側の問題で、同じ実行中に再試行しても無駄なもの。
    """

    def __init__(self, message: str, permanent: bool = False) -> None:
        super().__init__(message)
        self.permanent = permanent


class PaperDecision(BaseModel):
    id: int
    relevant: bool
    reason: str


class PaperDecisions(BaseModel):
    decisions: List[PaperDecision]


def is_claude_provider(provider: Optional[str]) -> bool:
    return str(provider or "").strip().lower() in CLAUDE_PROVIDERS


def estimate_cost_usd(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_creation_input_tokens: int = 0,
    cache_read_input_tokens: int = 0,
) -> float:
    """トークン数から推定コスト（USD）を返す。input_tokens はキャッシュ外の入力のみ（API の usage と同じ定義）。"""
    inp, out, cache_read = _PRICING.get(model, _UNKNOWN_MODEL_PRICING)
    return (
        input_tokens * inp
        + cache_creation_input_tokens * inp * _CACHE_WRITE_MULTIPLIER
        + cache_read_input_tokens * cache_read
        + output_tokens * out
    ) / 1_000_000


class UsageLedger:
    """Claude の利用量台帳（CSV）。当月の推定支出を集計し、予算超過を検知する。"""

    def __init__(self, path: str, monthly_budget_usd: float = 0.0, tag: str = "") -> None:
        self.path = path
        self.monthly_budget_usd = float(monthly_budget_usd or 0)  # 0以下なら上限なし
        self.tag = tag
        self.run_cost_usd = 0.0
        self._baseline_month: Optional[str] = None
        self._baseline_spend = 0.0

    def _read_month_spend(self, month: str) -> float:
        if not os.path.exists(self.path):
            return 0.0
        total = 0.0
        for attempt in range(3):
            try:
                with open(self.path, encoding="utf-8", newline="") as f:
                    rows = list(csv.DictReader(f))
                break
            except OSError as e:
                if e.errno == 11 and attempt < 2:  # EDEADLK on macOS (OneDrive同期)
                    sleep(2**attempt)
                else:
                    # 支出が分からないまま呼び出すと予算を守れないので、この実行では Claude を使わない
                    msg = f"利用量台帳を読み込めません ({self.path}): {e}"
                    raise ClaudeCallError(msg, permanent=True) from e
        else:
            return 0.0
        for row in rows:
            if str(row.get("timestamp", "")).startswith(month):
                try:
                    total += float(row.get("cost_usd") or 0)
                except ValueError:
                    continue
        return total

    def month_spend(self, now: Optional[datetime] = None) -> float:
        """当月の推定支出（この実行より前の記録 + この実行での支出）。台帳の読み込みは月ごとに1回。"""
        month = (now or datetime.now()).strftime("%Y-%m")
        if self._baseline_month != month:
            self._baseline_spend = self._read_month_spend(month)
            self._baseline_month = month
            self.run_cost_usd = 0.0
        return self._baseline_spend + self.run_cost_usd

    def check(self) -> None:
        if self.monthly_budget_usd <= 0:
            return
        spent = self.month_spend()
        if spent >= self.monthly_budget_usd:
            e = f"Claude の当月推定支出 ${spent:.2f} が予算 ${self.monthly_budget_usd:.2f} に達しました"
            raise BudgetExceededError(e)

    def record(self, purpose: str, model: str, usage: Any) -> float:
        self.month_spend()  # 基準値を確定させてから加算する
        def token_count(name: str) -> int:
            value = usage.get(name) if isinstance(usage, dict) else getattr(usage, name, None)
            return int(value or 0)

        tokens = {
            name: token_count(name)
            for name in ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
        }
        cost = estimate_cost_usd(model, **tokens)
        self.run_cost_usd += cost
        row = {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "tag": self.tag,
            "purpose": purpose,
            "model": model,
            **tokens,
            "cost_usd": f"{cost:.6f}",
        }
        self._append(row)
        return cost

    def _append(self, row: Dict[str, Any]) -> None:
        for attempt in range(3):
            try:
                is_new = not os.path.exists(self.path)
                with open(self.path, "a", encoding="utf-8", newline="") as f:
                    writer = csv.DictWriter(f, fieldnames=_LEDGER_FIELDS)
                    if is_new:
                        writer.writeheader()
                    writer.writerow(row)
            except OSError as e:
                if e.errno == 11 and attempt < 2:  # EDEADLK on macOS (OneDrive同期)
                    sleep(2**attempt)
                else:
                    # 台帳の書き込み失敗で論文処理全体を止めない（支出はメモリ上の run_cost_usd に残る）
                    print(f"  [Claude] 利用量台帳への書き込みに失敗しました: {e}")
                    return
            else:
                return


class ClaudeClient:
    """PaperBee 用の Claude 呼び出し。予算チェック・利用量記録・エラー分類をまとめて行う。"""

    def __init__(
        self,
        api_key: str = "",
        model: str = DEFAULT_MODEL,
        effort: str = DEFAULT_EFFORT,
        ledger: Optional[UsageLedger] = None,
        client: Optional[anthropic.Anthropic] = None,
    ) -> None:
        self.model = model or DEFAULT_MODEL
        self.effort = effort
        self.ledger = ledger
        # api_key が空なら SDK の既定の解決順（ANTHROPIC_API_KEY 環境変数 → `ant auth login` のプロファイル）に任せる
        self._client = client or anthropic.Anthropic(api_key=api_key or None, timeout=300.0, max_retries=3)

    def _build_params(self, messages: List[Dict[str, Any]], system: str, max_tokens: int) -> Dict[str, Any]:
        params: Dict[str, Any] = {"model": self.model, "max_tokens": max_tokens, "messages": messages}
        if system:
            # 判定基準（system）は同じ実行内の全バッチで共通なのでキャッシュする。
            # 最小キャッシュ長（Opus 5.5 等で512トークン）に満たない場合は無視されるだけで害はない。
            params["system"] = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
        if self.effort:
            params["output_config"] = {"effort": self.effort}
        if self.model in _SERVER_FALLBACK_MODELS:
            params["betas"] = [_SERVER_FALLBACK_BETA]
            params["fallbacks"] = "default"
        return params

    def _call_api(self, params: Dict[str, Any], output_format: Optional[type]) -> Any:
        """API を呼んで生の応答を返す。SDK の例外は ClaudeCallError（再試行の可否つき）にする。"""
        api = self._client.beta.messages.with_raw_response
        try:
            if output_format is not None:
                raw = api.parse(output_format=output_format, **params)
            else:
                raw = api.create(**params)
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError, anthropic.NotFoundError) as e:
            msg = f"Claude API の設定エラー ({type(e).__name__}): {e}"
            raise ClaudeCallError(msg, permanent=True) from e
        except anthropic.BadRequestError as e:
            # 残高不足もここに来る。同じ実行中に再試行しても通らない
            msg = f"Claude API がリクエストを受け付けません: {e}"
            raise ClaudeCallError(msg, permanent=True) from e
        except anthropic.RateLimitError as e:
            msg = f"Claude API のレート制限に達しました: {e}"
            raise ClaudeCallError(msg) from e
        except anthropic.APIStatusError as e:
            msg = f"Claude API エラー (HTTP {e.status_code}): {e}"
            raise ClaudeCallError(msg) from e
        except anthropic.APIConnectionError as e:
            msg = f"Claude API に接続できません: {e}"
            raise ClaudeCallError(msg) from e
        except TypeError as e:
            # API キーもプロファイルも見つからないと、SDK は送信時に TypeError を送出する
            msg = f"Claude API の認証情報を解決できません（ANTHROPIC_API_KEY を設定してください）: {e}"
            raise ClaudeCallError(msg, permanent=True) from e
        return raw

    def _send(self, purpose: str, params: Dict[str, Any], output_format: Optional[type]) -> Any:
        """API を呼び、利用量を記録してから応答を解釈する。

        構造化出力の検証は応答の解釈時に失敗しうる（打ち切りなど）ので、課金された利用量はその前に記録する。
        """
        raw = self._call_api(params, output_format)
        body = raw.http_response.json()
        if self.ledger is not None:
            # フォールバックが走った場合 model は実際に応答したモデル。費用はそのモデルの単価で概算する
            self.ledger.record(purpose, str(body.get("model") or self.model), body.get("usage") or {})

        stop_reason = body.get("stop_reason")
        if stop_reason == "refusal":
            category = (body.get("stop_details") or {}).get("category")
            msg = f"Claude がリクエストを拒否しました (category={category})"
            raise ClaudeCallError(msg)
        if stop_reason == "max_tokens":
            msg = "Claude の出力が max_tokens で打ち切られました"
            raise ClaudeCallError(msg)
        try:
            return raw.parse()
        except ValueError as e:  # 構造化出力の検証失敗（pydantic.ValidationError を含む）
            msg = f"Claude の出力を解析できませんでした: {e}"
            raise ClaudeCallError(msg) from e

    def _request(
        self,
        purpose: str,
        messages: List[Dict[str, Any]],
        system: str = "",
        output_format: Optional[type] = None,
        max_tokens: int = 16000,
    ) -> Any:
        if self.ledger is not None:
            self.ledger.check()
        return self._send(purpose, self._build_params(messages, system, max_tokens), output_format)

    def judge_papers(self, criteria: str, papers: Sequence[Tuple[int, str]]) -> Dict[int, PaperDecision]:
        """論文をまとめて1リクエストで判定する。papers は (id, 整形済みテキスト) の列。

        返り値は id → 判定。モデルが判定を返さなかった id は含まれない（呼び出し側で扱う）。
        """
        system = FILTER_SYSTEM_PROMPT.format(criteria=criteria.strip())
        body = "\n\n".join(f'<paper id="{pid}">\n{text}\n</paper>' for pid, text in papers)
        messages = [{"role": "user", "content": f"Judge these {len(papers)} papers.\n\n{body}"}]
        response = self._request("filter", messages, system=system, output_format=PaperDecisions)
        parsed = response.parsed_output
        if parsed is None:
            e = "Claude の判定結果が空でした"
            raise ClaudeCallError(e)
        wanted = {pid for pid, _ in papers}
        return {d.id: d for d in parsed.decisions if d.id in wanted}

    def generate_text(self, prompt: str, purpose: str = "summarize") -> str:
        response = self._request(purpose, [{"role": "user", "content": prompt}])
        return "".join(block.text for block in response.content if block.type == "text").strip()


# --- 実行単位の共有設定 ------------------------------------------------------------
# 要約・翻訳（utils.translate_abstract）は関数単位で呼ばれるため、台帳とクライアントは
# モジュール単位で共有する。daily_posting が起動時に configure() を呼ぶ。

_ledger: Optional[UsageLedger] = None
_default_effort: str = DEFAULT_EFFORT
_clients: Dict[Tuple[str, str, str], ClaudeClient] = {}


def configure(
    ledger_path: str = "",
    monthly_budget_usd: float = 0.0,
    tag: str = "",
    effort: str = DEFAULT_EFFORT,
) -> None:
    global _ledger, _default_effort
    _ledger = UsageLedger(ledger_path, monthly_budget_usd, tag) if ledger_path else None
    _default_effort = effort
    _clients.clear()


def get_client(api_key: str = "", model: str = "", effort: Optional[str] = None) -> ClaudeClient:
    resolved_effort = _default_effort if effort is None else effort
    key = (api_key, model or DEFAULT_MODEL, resolved_effort)
    if key not in _clients:
        _clients[key] = ClaudeClient(api_key=api_key, model=key[1], effort=resolved_effort, ledger=_ledger)
    return _clients[key]


def usage_summary() -> str:
    """この実行での Claude 支出と当月累計の1行サマリー（台帳未設定なら空文字）。"""
    if _ledger is None:
        return ""
    try:
        month = f"${_ledger.month_spend():.2f}"
    except ClaudeCallError:  # 台帳を読めなくても、ログ表示のために処理を止めない
        month = "不明（台帳を読めません）"
    budget = f" / 予算 ${_ledger.monthly_budget_usd:.2f}" if _ledger.monthly_budget_usd > 0 else ""
    return f"Claude 推定支出: 今回 ${_ledger.run_cost_usd:.4f}, 当月累計 {month}{budget}"
