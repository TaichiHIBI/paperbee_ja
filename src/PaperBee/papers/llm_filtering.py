import re
import time
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
from ollama import Client
from openai import OpenAI

try:
    from google import genai
except ImportError:
    genai = None

from .claude_client import BudgetExceededError, ClaudeCallError, get_client, is_claude_provider

OLLAMA_HOST = "http://localhost:11434"
# 応答が止まったローカルLLMで実行全体がハングし、後続トピックが走らなくなるのを防ぐ上限（秒）
OLLAMA_TIMEOUT_SECONDS = 600

# LLM に渡すアブストラクトの上限。通常のアブストラクトは収まり、本文が混入した不正レコードだけを切る
ABSTRACT_MAX_CHARS = 4000
# 著者数の多い論文は先頭と末尾（責任著者がいる側）だけを渡す
AUTHORS_SHOWN_EACH_END = 3

# 一次ふるい（ローカルLLM）用の追加指示。最終判定は後段で行うので、取りこぼしを避けて甘めに通す
PREFILTER_INSTRUCTIONS = (
    "You are a first-pass screener. A senior reviewer makes the final decision on every paper you keep, "
    "so answer 'no' only when the paper is clearly unrelated to the criteria above. "
    "If it might be relevant, or you are unsure, answer 'yes'. Answer with a single word: yes or no."
)

_YES_NO = re.compile(r"\b(yes|no)\b", re.IGNORECASE)


def parse_yes_no(content: Optional[str]) -> Optional[bool]:
    """LLM の yes/no 回答を解釈する。最初に現れた yes/no を採用し、見つからなければ None。"""
    if not content:
        return None
    match = _YES_NO.search(content)
    if match is None:
        return None
    return match.group(1).lower() == "yes"


def _shorten_authors(authors: str) -> str:
    names = [a.strip() for a in authors.split(",") if a.strip()]
    if len(names) <= 2 * AUTHORS_SHOWN_EACH_END + 1:
        return ", ".join(names)
    head = names[:AUTHORS_SHOWN_EACH_END]
    tail = names[-AUTHORS_SHOWN_EACH_END:]
    return f"{', '.join(head)}, …, {', '.join(tail)} ({len(names)} authors)"


def format_paper_for_llm(article: Any) -> str:
    """フィルタリング用に論文1件をテキスト化する（タイトル・誌名・著者・キーワード・アブストラクト）。"""

    def field(name: str) -> str:
        value = article.get(name, "")
        if value is None or (isinstance(value, float) and pd.isna(value)):
            return ""
        return str(value).strip()

    lines = [f"Title: {field('Title')}"]
    if field("Journal"):
        lines.append(f"Journal: {field('Journal')}")
    authors = _shorten_authors(field("Authors"))
    if authors:
        lines.append(f"Authors: {authors}")
    if field("Keywords"):
        lines.append(f"Keywords: {field('Keywords')}")
    abstract = field("abstract")
    if abstract:
        if len(abstract) > ABSTRACT_MAX_CHARS:
            abstract = abstract[:ABSTRACT_MAX_CHARS] + " …"
        lines.append(f"Abstract: {abstract}")
    return "\n".join(lines)


class LLMFilter:
    """
    LLM で論文を選別する。

    構成:
      - ローカルLLM（local_provider="ollama"）: Claude と組み合わせるサブエージェント。次の2役を担う
          * 一次ふるい（use_prefilter=True のとき）: 1件ずつ、明らかに無関係なものだけを落とす
          * 代行判定: Claude が使えない場合（予算超過・API障害・拒否）に最終判定を代行する
      - 最終判定: llm_provider で指定したLLM。"claude" の場合は claude_batch_size 件ずつまとめて判定する
      - 代行役がいないまま Claude が使えなくなった論文は投稿しない
        （履歴に記録されないため、--since の範囲内なら次回の実行で再判定される）

    判定結果は self.decisions に index → (relevant, stage, reason) で残る。stage は
    "prefilter" / "claude" / ローカルの最終判定プロバイダー名 / "local-fallback" / "unjudged"。

    Args:
        df (pd.DataFrame): 判定対象の論文（Title, Journal, Authors, Keywords, abstract 列を参照）。
        llm_provider (str): 最終判定のプロバイダー（"claude", "ollama", "openai", "gemini"）。
        model (str): 最終判定のモデル名。
        filtering_prompt (str): 選別基準のプロンプト。
        llm_api_key (str): 最終判定プロバイダーの API キー（claude で空なら SDK の既定解決に任せる）。
        local_provider (str): ローカルLLMのプロバイダー（"ollama" のみ）。空ならローカルLLMなし。
        local_model (str): ローカルLLMのモデル名。
        use_prefilter (bool): 最終判定の前にローカルLLMで一次ふるいをかけるか。
        claude_batch_size (int): Claude に1リクエストでまとめて渡す論文数。
    """

    def __init__(
        self,
        df: pd.DataFrame,
        llm_provider: str = "openai",
        model: str = "gpt-3.5-turbo",
        filtering_prompt: str = "",
        llm_api_key: str = "",
        local_provider: str = "",
        local_model: str = "",
        use_prefilter: bool = False,
        claude_batch_size: int = 20,
    ) -> None:
        self.df: pd.DataFrame = df
        self.llm_provider: str = llm_provider.lower()
        self.model: str = model
        self.filtering_prompt: str = filtering_prompt
        self.claude_batch_size: int = max(1, int(claude_batch_size))
        self.decisions: Dict[Any, Tuple[bool, str, str]] = {}

        self.claude: Any = None
        self.client: Any = None
        if is_claude_provider(self.llm_provider):
            self.claude = get_client(api_key=llm_api_key, model=model)
        else:
            self.client = self._make_client(self.llm_provider, llm_api_key)

        self.local_provider: str = (local_provider or "").lower()
        self.local_model: str = local_model
        self.local_client: Any = None
        if self.local_provider:
            if self.local_provider != "ollama":
                e = f"LOCAL_LLM_PROVIDER は 'ollama' のみ対応しています: {self.local_provider}"
                raise ValueError(e)
            self.local_client = self._make_client("ollama", "")
        self.use_prefilter: bool = bool(use_prefilter) and self.local_client is not None

    @staticmethod
    def _make_client(provider: str, api_key: str) -> Any:
        if provider == "openai":
            return OpenAI(api_key=api_key)
        if provider == "ollama":
            return Client(host=OLLAMA_HOST, timeout=OLLAMA_TIMEOUT_SECONDS)
        if provider == "gemini":
            if genai is None:
                e = "google-genai package is not installed. Please install it with 'pip install google-genai'."
                raise ImportError(e)
            return genai.Client(api_key=api_key)
        e = "Invalid LLM provider. Choose 'claude', 'ollama', 'openai', or 'gemini'."
        raise ValueError(e)

    def _paper_text(self, index: Any) -> str:
        return format_paper_for_llm(self.df.loc[index])

    @staticmethod
    def _ask(provider: str, client: Any, model: str, system: str, message: str) -> Optional[str]:
        if provider == "ollama":
            response = client.chat(
                model=model,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": message}],
            )
            return str(response["message"]["content"])
        if provider == "openai":
            response = client.chat.completions.create(
                model=model,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": message}],
            )
            content = response.choices[0].message.content
            return None if content is None else str(content)
        if provider == "gemini":
            prompt_text = f"{system}\n\nTask: Based on the title and abstract, answer 'yes' or 'no'.\n\n{message}"
            response = client.models.generate_content(model=model, contents=prompt_text)
            return None if response.text is None else str(response.text)
        e = f"Invalid client type or provider mismatch: {provider}"
        raise TypeError(e)

    def _ask_safely(self, provider: str, client: Any, model: str, system: str, message: str) -> Optional[str]:
        """1件の問い合わせ。失敗しても実行全体は止めず None を返す。"""
        try:
            return self._ask(provider, client, model, system, message)
        except Exception as e:
            print(f"  [{provider}:{model}] 問い合わせ失敗: {type(e).__name__}: {e}")
            return None
        finally:
            if provider in ("openai", "gemini"):
                time.sleep(0.5)  # リモートAPIのレート制限対策（ローカルの Ollama は待たない）

    def _judge_locally(self, index: Any, provider: str, client: Any, model: str, stage: str) -> None:
        answer = self._ask_safely(provider, client, model, self.filtering_prompt, self._paper_text(index))
        verdict = parse_yes_no(answer)
        if verdict is None:
            # 判定できなかった論文は不採用として記録しない（キャッシュされず、次回の実行で判定し直す）
            reason = f"{provider} error" if answer is None else "no yes/no in answer"
            self.decisions[index] = (False, "unjudged", reason)
        else:
            self.decisions[index] = (verdict, stage, "")

    def _prefilter(self, indices: List[Any]) -> List[Any]:
        system = f"{self.filtering_prompt}\n\n{PREFILTER_INSTRUCTIONS}"
        kept: List[Any] = []
        for index in indices:
            answer = self._ask_safely(
                self.local_provider, self.local_client, self.local_model, system, self._paper_text(index)
            )
            if parse_yes_no(answer) is False:
                self.decisions[index] = (False, "prefilter", "")
            else:  # yes・判定不能・エラーは最終判定へ回す（取りこぼし防止）
                kept.append(index)
        print(f"  一次ふるい ({self.local_model}): {len(indices)}件 → {len(kept)}件")
        return kept

    def _judge_fallback(self, indices: List[Any]) -> None:
        if self.local_client is None:
            for index in indices:
                self.decisions[index] = (False, "unjudged", "Claude unavailable")
            print(f"  [Claude] 代行役のローカルLLM（LOCAL_LLM_PROVIDER）が未設定のため {len(indices)}件を見送ります")
            return
        for index in indices:
            self._judge_locally(
                index, self.local_provider, self.local_client, self.local_model, stage="local-fallback"
            )

    def _judge_with_claude(self, indices: List[Any]) -> None:
        for start in range(0, len(indices), self.claude_batch_size):
            batch = indices[start : start + self.claude_batch_size]
            if self.claude is None:  # この実行中に使えなくなった
                self._judge_fallback(batch)
                continue
            try:
                decisions = self.claude.judge_papers(
                    self.filtering_prompt, [(i, self._paper_text(index)) for i, index in enumerate(batch)]
                )
            except BudgetExceededError as e:
                print(f"  [Claude] {e}。残りはローカルLLMで判定します。")
                self.claude = None
                self._judge_fallback(batch)
                continue
            except ClaudeCallError as e:
                print(f"  [Claude] {e}")
                if e.permanent:
                    self.claude = None
                self._judge_fallback(batch)
                continue

            missing = []
            for i, index in enumerate(batch):
                decision = decisions.get(i)
                if decision is None:
                    missing.append(index)
                else:
                    self.decisions[index] = (decision.relevant, "claude", decision.reason)
            if missing:
                print(f"  [Claude] {len(missing)}件の判定が返らなかったため、ローカルLLMで判定します")
                self._judge_fallback(missing)

    def filter_articles(self) -> pd.DataFrame:
        """
        Filters the articles in the DataFrame by determining their relevance using the LLM.

        Returns:
            pd.DataFrame: A filtered DataFrame containing only the articles deemed relevant by the LLM.
        """
        candidates = list(self.df.index)
        if self.use_prefilter and candidates:
            candidates = self._prefilter(candidates)

        if self.claude is not None:
            self._judge_with_claude(candidates)
        else:
            for index in candidates:
                self._judge_locally(index, self.llm_provider, self.client, self.model, stage=self.llm_provider)

        retained = [index for index in self.df.index if self.decisions.get(index, (False, "", ""))[0]]
        return self.df.loc[retained]
