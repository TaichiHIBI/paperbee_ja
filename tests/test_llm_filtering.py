import pandas as pd
import pytest

from PaperBee.papers import llm_filtering
from PaperBee.papers.claude_client import BudgetExceededError, ClaudeCallError, PaperDecision
from PaperBee.papers.llm_filtering import LLMFilter, format_paper_for_llm, parse_yes_no


def _df(titles):
    return pd.DataFrame({
        "DOI": [f"10.1/{i}" for i in range(len(titles))],
        "Title": titles,
        "Journal": ["J"] * len(titles),
        "Authors": ["A One, B Two"] * len(titles),
        "Keywords": [""] * len(titles),
        "abstract": [f"Abstract of {t}" for t in titles],
    })


class FakeClaude:
    """judge_papers の振る舞いを差し替えるテスト用 Claude。タイトルに 'good' を含むものを採用する。"""

    def __init__(self, errors=None, drop_ids=()):
        self.errors = list(errors or [])
        self.drop_ids = set(drop_ids)
        self.calls = 0

    def judge_papers(self, criteria, papers):
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return {
            pid: PaperDecision(id=pid, relevant="good" in text, reason="r")
            for pid, text in papers
            if pid not in self.drop_ids
        }


def _make_filter(monkeypatch, df, fake_claude, local_answer=None, prefilter=True, local=True, batch_size=2):
    monkeypatch.setattr(llm_filtering, "get_client", lambda **kwargs: fake_claude)
    monkeypatch.setattr(LLMFilter, "_make_client", staticmethod(lambda provider, key: object()))
    llm_filter = LLMFilter(
        df,
        llm_provider="claude",
        model="claude-opus-5-5",
        filtering_prompt="criteria",
        local_provider="ollama" if local else "",
        local_model="gemma4:26b",
        use_prefilter=prefilter,
        claude_batch_size=batch_size,
    )
    local_calls = []

    def fake_ask(provider, client, model, system, message, json_schema=None):
        local_calls.append((system, message))
        return local_answer(system, message)

    monkeypatch.setattr(llm_filter, "_ask", fake_ask)
    return llm_filter, local_calls


def test_parse_yes_no():
    assert parse_yes_no("Yes") is True
    assert parse_yes_no("**No**.") is False
    assert parse_yes_no("yes, but no clinical data") is True
    assert parse_yes_no("") is None
    assert parse_yes_no("I cannot tell") is None
    assert parse_yes_no("Nobody knows") is None


def test_format_paper_truncates_authors_and_abstract():
    article = {
        "Title": "T",
        "Journal": "",
        "Authors": ", ".join(f"Author {i}" for i in range(20)),
        "Keywords": "kw",
        "abstract": "x" * (llm_filtering.ABSTRACT_MAX_CHARS + 100),
    }
    text = format_paper_for_llm(article)
    assert "Author 0, Author 1, Author 2, …, Author 17, Author 18, Author 19 (20 authors)" in text
    assert "Journal" not in text
    assert text.endswith("x …")


def test_prefilter_then_claude(monkeypatch):
    df = _df(["good A", "off-topic B", "good C", "bad D"])
    fake = FakeClaude()
    # 一次ふるいは "off-topic" だけを明確に不採用にする
    llm_filter, _ = _make_filter(monkeypatch, df, fake, lambda s, m: "no" if "off-topic" in m else "yes")

    kept = llm_filter.filter_articles()

    assert list(kept["Title"]) == ["good A", "good C"]
    assert llm_filter.decisions[1][1] == "prefilter"
    assert llm_filter.decisions[3] == (False, "claude", "r")
    assert fake.calls == 2  # 残り3件を batch_size=2 で


def test_budget_exceeded_falls_back_to_local_for_remaining_batches(monkeypatch):
    df = _df(["good A", "good B", "good C"])
    fake = FakeClaude(errors=[BudgetExceededError("over budget")])
    llm_filter, _ = _make_filter(
        monkeypatch, df, fake, lambda s, m: "yes" if "C" in m or "first-pass" in s else "no"
    )

    kept = llm_filter.filter_articles()

    assert fake.calls == 1  # 予算超過以降は Claude を呼ばない
    assert {stage for _, stage, _ in llm_filter.decisions.values()} == {"local-fallback"}
    assert list(kept["Title"]) == ["good C"]


def test_missing_ids_are_judged_locally(monkeypatch):
    df = _df(["good A", "good B"])
    fake = FakeClaude(drop_ids={1})
    llm_filter, _ = _make_filter(monkeypatch, df, fake, lambda s, m: "yes")

    kept = llm_filter.filter_articles()

    assert llm_filter.decisions[0][1] == "claude"
    assert llm_filter.decisions[1][1] == "local-fallback"
    assert len(kept) == 2


def test_without_local_fallback_unavailable_claude_posts_nothing(monkeypatch):
    df = _df(["good A", "good B", "good C"])
    fake = FakeClaude(errors=[ClaudeCallError("bad key", permanent=True)])
    llm_filter, _ = _make_filter(monkeypatch, df, fake, lambda s, m: "yes", prefilter=False, local=False)

    kept = llm_filter.filter_articles()

    assert kept.empty
    assert fake.calls == 1  # 恒久的なエラーの後は再試行しない
    assert {stage for _, stage, _ in llm_filter.decisions.values()} == {"unjudged"}


def test_local_error_does_not_crash_run(monkeypatch):
    df = _df(["good A"])
    monkeypatch.setattr(LLMFilter, "_make_client", staticmethod(lambda provider, key: object()))
    llm_filter = LLMFilter(df, llm_provider="ollama", model="gemma4:26b", filtering_prompt="c")

    def boom(*args):
        e = "ollama timed out"
        raise TimeoutError(e)

    monkeypatch.setattr(llm_filter, "_ask", boom)
    assert llm_filter.filter_articles().empty
    assert llm_filter.decisions[0] == (False, "unjudged", "ollama error")


def test_local_fallback_without_prefilter(monkeypatch):
    df = _df(["good A", "good B"])
    fake = FakeClaude(errors=[ClaudeCallError("server error")])
    llm_filter, local_calls = _make_filter(monkeypatch, df, fake, lambda s, m: "yes", prefilter=False, batch_size=1)

    kept = llm_filter.filter_articles()

    # 一次ふるいは走らず、失敗したバッチだけローカルが代行し、次のバッチは Claude に戻る
    assert all("first-pass" not in system for system, _ in local_calls)
    assert llm_filter.decisions[0][1] == "local-fallback"
    assert llm_filter.decisions[1][1] == "claude"
    assert len(kept) == 2


def test_local_provider_must_be_ollama():
    with pytest.raises(ValueError):
        LLMFilter(_df(["a"]), llm_provider="ollama", model="m", local_provider="openai")



def test_answer_without_yes_or_no_is_unjudged(monkeypatch):
    df = _df(["good A"])
    monkeypatch.setattr(LLMFilter, "_make_client", staticmethod(lambda provider, key: object()))
    llm_filter = LLMFilter(df, llm_provider="ollama", model="gemma4:26b", filtering_prompt="c")
    monkeypatch.setattr(llm_filter, "_ask", lambda *args: "I cannot evaluate this paper.")

    assert llm_filter.filter_articles().empty
    # 不採用（キャッシュ対象）ではなく判定不能として残り、次回の実行で判定し直される
    assert llm_filter.decisions[0] == (False, "unjudged", "no yes/no in answer")


def test_parse_judgment_reads_json_or_falls_back_to_yes_no():
    from PaperBee.papers.llm_filtering import parse_judgment

    assert parse_judgment('{"relevant": true, "reason": "Theis 研の新手法"}') == (True, "Theis 研の新手法")
    assert parse_judgment('```json\n{"relevant": false, "reason": "臨床研究"}\n```') == (False, "臨床研究")
    assert parse_judgment("Yes.") == (True, "")  # JSON で返らなくても判定は読む
    assert parse_judgment('{"relevant": "maybe"}') is None
    assert parse_judgment("") is None


def test_local_final_judge_returns_reason_with_json_schema(monkeypatch):
    calls = []

    class FakeOllama:
        def chat(self, model, messages, format=None):
            calls.append((messages[0]["content"], format))
            return {"message": {"content": '{"relevant": true, "reason": "Theis 研の生成モデル"}'}}

    monkeypatch.setattr(LLMFilter, "_make_client", staticmethod(lambda provider, key: FakeOllama()))
    llm_filter = LLMFilter(_df(["good A"]), llm_provider="ollama", model="gemma4:26b", filtering_prompt="c", with_reason=True)

    assert len(llm_filter.filter_articles()) == 1
    assert llm_filter.decisions[0] == (True, "ollama", "Theis 研の生成モデル")
    system, schema = calls[0]
    assert "Japanese" in system and schema == llm_filtering.JUDGMENT_SCHEMA


def test_prefilter_stays_single_word_without_schema(monkeypatch):
    df = _df(["good A"])
    fake = FakeClaude()
    llm_filter, local_calls = _make_filter(monkeypatch, df, fake, lambda s, m: "yes")
    llm_filter.with_reason = True
    llm_filter.filter_articles()
    # 一次ふるいは速度優先で yes/no の1語のまま（理由は求めない）
    assert all("JSON" not in system for system, _ in local_calls)
