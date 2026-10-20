import json
from datetime import datetime

import anthropic
import httpx2
import pytest

from PaperBee.papers.claude_client import (
    BudgetExceededError,
    ClaudeCallError,
    ClaudeClient,
    UsageLedger,
    estimate_cost_usd,
)


def _message(text: str, stop_reason: str = "end_turn", model: str = "claude-opus-5-5") -> dict:
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [{"type": "text", "text": text}] if text else [],
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": 1000,
            "output_tokens": 200,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        },
    }


def _client_returning(status: int, body: dict, seen: list) -> anthropic.Anthropic:
    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(status, json=body)

    return anthropic.Anthropic(
        api_key="sk-ant-test",
        max_retries=0,
        http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(handler)),
    )


def test_estimate_cost_usd():
    # Opus 5.5: $4 / $20 per MTok
    assert estimate_cost_usd("claude-opus-5-5", 1_000_000, 0) == pytest.approx(4.0)
    assert estimate_cost_usd("claude-opus-5-5", 0, 1_000_000) == pytest.approx(20.0)
    # 未知のモデルは高めに見積もる
    assert estimate_cost_usd("claude-unknown", 1_000_000, 0) > estimate_cost_usd("claude-opus-5-5", 1_000_000, 0)


def test_ledger_counts_only_current_month_and_enforces_budget(tmp_path):
    path = tmp_path / "claude_usage.csv"
    path.write_text(
        "timestamp,tag,purpose,model,input_tokens,output_tokens,cache_creation_input_tokens,cache_read_input_tokens,cost_usd\n"
        "2026-09-30T09:00:00,history_a,filter,claude-opus-5-5,0,0,0,0,50.0\n"
        f"{datetime.now():%Y-%m}-01T09:00:00,history_a,filter,claude-opus-5-5,0,0,0,0,1.5\n",
        encoding="utf-8",
    )
    ledger = UsageLedger(str(path), monthly_budget_usd=2.0, tag="history_b")
    assert ledger.month_spend() == pytest.approx(1.5)
    ledger.check()  # 1.5 < 2.0

    class Usage:
        input_tokens = 0
        output_tokens = 30_000  # $0.60 on Opus 5.5
        cache_creation_input_tokens = 0
        cache_read_input_tokens = 0

    ledger.record("filter", "claude-opus-5-5", Usage())
    assert ledger.month_spend() == pytest.approx(2.1)
    with pytest.raises(BudgetExceededError):
        ledger.check()
    # 新しい行が追記され、別プロセスからも当月分として読める
    assert UsageLedger(str(path)).month_spend() == pytest.approx(2.1)


def test_judge_papers_request_shape_and_parsing(tmp_path):
    seen: list = []
    decisions = {"decisions": [
        {"id": 0, "relevant": True, "reason": "New method."},
        {"id": 1, "relevant": False, "reason": "Clinical study."},
    ]}
    ledger = UsageLedger(str(tmp_path / "usage.csv"), monthly_budget_usd=100)
    client = ClaudeClient(
        model="claude-opus-5-5", effort="low", ledger=ledger,
        client=_client_returning(200, _message(json.dumps(decisions)), seen),
    )

    result = client.judge_papers("Only methods papers.", [(0, "Title: A"), (1, "Title: B")])

    assert result[0].relevant is True
    assert result[1].relevant is False
    body = json.loads(seen[0].content)
    assert body["model"] == "claude-opus-5-5"
    assert body["output_config"]["effort"] == "low"
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert body["fallbacks"] == "default"
    assert "server-side-fallback-2026-07-01" in seen[0].headers["anthropic-beta"]
    assert body["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "Only methods papers." in body["system"][0]["text"]
    assert '<paper id="1">' in body["messages"][0]["content"]
    # 1000 in + 200 out on Opus 5.5
    assert ledger.run_cost_usd == pytest.approx((1000 * 4 + 200 * 20) / 1_000_000)


def test_haiku_requests_omit_server_fallback():
    seen: list = []
    client = ClaudeClient(model="claude-haiku-5-5", client=_client_returning(200, _message("ok"), seen))
    assert client.generate_text("Summarize") == "ok"
    body = json.loads(seen[0].content)
    assert "fallbacks" not in body
    assert "server-side-fallback" not in seen[0].headers.get("anthropic-beta", "")


def test_refusal_raises_retryable_error():
    client = ClaudeClient(client=_client_returning(200, _message("", stop_reason="refusal"), []))
    with pytest.raises(ClaudeCallError) as info:
        client.generate_text("Summarize")
    assert info.value.permanent is False


def test_authentication_error_is_permanent():
    error = {"type": "error", "error": {"type": "authentication_error", "message": "invalid x-api-key"}}
    client = ClaudeClient(client=_client_returning(401, error, []))
    with pytest.raises(ClaudeCallError) as info:
        client.generate_text("Summarize")
    assert info.value.permanent is True


def test_translate_abstract_dispatches_to_claude(monkeypatch):
    from PaperBee.papers import utils

    calls = []

    class Fake:
        def generate_text(self, prompt):
            calls.append(prompt)
            return "・要点1"

    monkeypatch.setattr(utils, "get_claude_client", lambda api_key, model: Fake())
    out = utils.translate_abstract("An abstract.", "claude", "claude-opus-5-5", "", "日本語で要約:\n\n{text}")
    assert out == "・要点1"
    assert calls == ["日本語で要約:\n\nAn abstract."]


def test_translate_abstract_returns_original_text_when_claude_fails(monkeypatch):
    from PaperBee.papers import utils

    class Failing:
        def generate_text(self, prompt):
            e = "over budget"
            raise BudgetExceededError(e)

    monkeypatch.setattr(utils, "get_claude_client", lambda api_key, model: Failing())
    assert utils.translate_abstract("An abstract.", "claude", "claude-opus-5-5", "", "{text}") == "An abstract."


def test_missing_credentials_is_permanent(tmp_path, monkeypatch):
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))  # `ant auth login` のプロファイルも見えないようにする
    client = ClaudeClient(api_key="")
    with pytest.raises(ClaudeCallError) as info:
        client.generate_text("Summarize")
    assert info.value.permanent is True



@pytest.mark.parametrize(
    ("text", "stop_reason"),
    [
        ('{"decisions": [{"id": 0, "rel', "max_tokens"),  # 出力の打ち切り
        ("not json at all", "end_turn"),  # 構造化出力の検証失敗
    ],
)
def test_usage_is_recorded_even_when_output_cannot_be_parsed(tmp_path, text, stop_reason):
    message = _message(text, stop_reason=stop_reason)
    message["usage"]["output_tokens"] = 16000
    ledger = UsageLedger(str(tmp_path / "usage.csv"), monthly_budget_usd=100)
    client = ClaudeClient(ledger=ledger, client=_client_returning(200, message, []))

    with pytest.raises(ClaudeCallError):
        client.judge_papers("criteria", [(0, "Title: A")])

    assert ledger.run_cost_usd == pytest.approx((1000 * 4 + 16000 * 20) / 1_000_000)
    assert UsageLedger(str(tmp_path / "usage.csv")).month_spend() == pytest.approx(ledger.run_cost_usd)


def test_usage_summary_survives_unreadable_ledger(tmp_path):
    from PaperBee.papers import claude_client

    unreadable = tmp_path / "claude_usage.csv"
    unreadable.mkdir()  # ファイルの代わりにディレクトリがあり、開けない
    claude_client.configure(ledger_path=str(unreadable), monthly_budget_usd=100)
    try:
        assert "不明" in claude_client.usage_summary()
    finally:
        claude_client.configure()
