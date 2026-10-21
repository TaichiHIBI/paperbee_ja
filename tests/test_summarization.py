import pytest

from PaperBee.papers import utils
from PaperBee.papers.claude_client import BudgetExceededError, ClaudeCallError


class FakeOllama:
    """ollama.Client の代わり。呼ばれたプロンプトを記録し、決めた内容を返す。"""

    calls: list = []  # noqa: RUF012
    reply = "- ローカルの要約"

    def __init__(self, timeout=None):
        pass

    def chat(self, model, messages):
        FakeOllama.calls.append((model, messages[0]["content"]))
        if FakeOllama.reply is None:
            e = "ollama timed out"
            raise TimeoutError(e)
        return {"message": {"content": FakeOllama.reply}}


class FailingClaude:
    def __init__(self, error):
        self.error = error

    def generate_text(self, prompt):
        raise self.error


@pytest.fixture
def fake_ollama(monkeypatch):
    FakeOllama.calls = []
    FakeOllama.reply = "- ローカルの要約"
    monkeypatch.setattr(utils.ollama, "Client", FakeOllama)
    return FakeOllama


@pytest.mark.parametrize("error", [BudgetExceededError("over budget"), ClaudeCallError("server error")])
def test_local_llm_takes_over_when_claude_fails(monkeypatch, fake_ollama, error):
    monkeypatch.setattr(utils, "get_claude_client", lambda api_key, model: FailingClaude(error))
    out = utils.translate_abstract("An abstract.", "claude", "claude-sonnet-5-5", "", "要約:\n{text}", "ollama", "gemma4:26b")
    assert out == "- ローカルの要約"
    assert fake_ollama.calls == [("gemma4:26b", "要約:\nAn abstract.")]  # 同じ指示で代行する


def test_original_text_when_fallback_also_fails(monkeypatch, fake_ollama):
    monkeypatch.setattr(utils, "get_claude_client", lambda api_key, model: FailingClaude(ClaudeCallError("x")))
    fake_ollama.reply = None
    assert utils.translate_abstract("An abstract.", "claude", "m", "", "{text}", "ollama", "gemma4:26b") == "An abstract."


def test_no_fallback_configured_keeps_original_text(monkeypatch, fake_ollama):
    monkeypatch.setattr(utils, "get_claude_client", lambda api_key, model: FailingClaude(ClaudeCallError("x")))
    assert utils.translate_abstract("An abstract.", "claude", "m", "", "{text}") == "An abstract."
    assert fake_ollama.calls == []


def test_empty_response_counts_as_failure(monkeypatch, fake_ollama):
    class Empty:
        def generate_text(self, prompt):
            return "   "

    monkeypatch.setattr(utils, "get_claude_client", lambda api_key, model: Empty())
    assert utils.translate_abstract("An abstract.", "claude", "m", "", "{text}", "ollama", "gemma4:26b") == "- ローカルの要約"


def test_gemini_without_key_does_not_post_error_text(fake_ollama):
    out = utils.translate_abstract("An abstract.", "gemini", "gemini-2.5-flash", "", "{text}")
    assert out == "An abstract."  # 以前は "(Error: API Key missing)" が付いたまま投稿されていた


def test_unknown_provider_returns_text_untouched(fake_ollama):
    assert utils.translate_abstract("An abstract.", "none", "m", "", "{text}", "ollama", "gemma4:26b") == "An abstract."
    assert fake_ollama.calls == []


def test_same_local_model_is_not_retried(fake_ollama):
    fake_ollama.reply = None
    assert utils.translate_abstract("An abstract.", "ollama", "gemma4:26b", "", "{text}", "ollama", "gemma4:26b") == "An abstract."
    assert len(fake_ollama.calls) == 1


def test_articles_processor_passes_local_fallback(monkeypatch, fake_ollama):
    monkeypatch.setattr(utils, "get_claude_client", lambda api_key, model: FailingClaude(ClaudeCallError("x")))
    processor = utils.ArticlesProcessor(
        [{"databases": ["bioRxiv"], "publication_date": "2026-10-20", "title": "T", "authors": ["A B"],
          "keywords": [], "url": "https://doi.org/10.64898/x", "abstract": "An abstract.", "publication": None}],
        "2026-10-21",
        summarization_enabled=True, summarization_provider="claude", summarization_model="claude-sonnet-5-5",
        summarization_prompt="{text}", local_provider="ollama", local_model="gemma4:26b",
    )
    processor.run_llm_processing()
    assert list(processor.articles["Abstract_JP"]) == ["- ローカルの要約"]
