import asyncio
from datetime import date
from typing import ClassVar

import pandas as pd
import pytest
import requests

from PaperBee.papers import papers_finder
from PaperBee.papers.papers_finder import HISTORY_COLUMNS, SLACK_PAPERS_PER_MESSAGE, PapersFinder


def _finder(tmp_path, **kwargs):
    finder = PapersFinder(
        root_dir=str(tmp_path),
        spreadsheet_id="",
        google_credentials_json="",
        sheet_name="Papers",
        history_file="history_test.csv",
        slack_bot_token="xoxb-test",  # noqa: S106
        slack_channel_id="C123",
        **kwargs,
    )
    finder.since = date(2026, 10, 19)
    finder.until = date(2026, 10, 21)
    return finder


def _articles(n):
    return pd.DataFrame([
        {
            "DOI": f"10.1/{i}", "Date": "2026-10-21", "PostedDate": "2026-10-20", "IsPreprint": "TRUE" if i % 2 else "FALSE",
            "Title": f"Paper {i}", "Journal": "", "Keywords": "", "Preprint": "", "Abstract_JP": "- 要点", "URL": f"https://doi.org/10.1/{i}",
        }
        for i in range(n)
    ])


class FakeSlack:
    """chat_postMessage を記録し、fail_on 通目（1始まり）で例外を出すテスト用クライアント。"""

    calls: ClassVar[list] = []
    fail_on: ClassVar[set] = set()

    def __init__(self, token):
        pass

    def chat_postMessage(self, **kwargs):
        FakeSlack.calls.append(kwargs)
        if len(FakeSlack.calls) in FakeSlack.fail_on:
            e = "invalid_blocks"
            raise RuntimeError(e)
        return {"ok": True, "ts": str(len(FakeSlack.calls))}


@pytest.fixture
def fake_slack(monkeypatch):
    FakeSlack.calls = []
    FakeSlack.fail_on = set()
    monkeypatch.setattr(papers_finder, "WebClient", FakeSlack)
    return FakeSlack


def _history_dois(tmp_path):
    path = tmp_path / "history_test.csv"
    if not path.exists():
        return []
    return list(pd.read_csv(path, dtype=str, encoding="utf-8-sig")["DOI"])


def test_many_papers_are_split_into_messages_under_the_block_limit(tmp_path, fake_slack):
    finder = _finder(tmp_path)
    rows = finder.select_new_papers(_articles(80))

    response, posted = finder._post_to_slack_in_chunks(rows)

    assert response is not None and len(posted) == 80
    assert len(fake_slack.calls) == 3  # 35 + 35 + 10
    assert all(len(call["blocks"]) <= 50 for call in fake_slack.calls)
    assert fake_slack.calls[0]["blocks"][0]["text"]["text"].strip().endswith("(1/3)")
    texts = [b["text"]["text"] for call in fake_slack.calls for b in call["blocks"] if b["type"] == "section"]
    assert sum(t.startswith((":pencil:", ":rolled_up_newspaper:")) for t in texts) == 80
    assert not any("found today" in t for t in texts)  # 分割時は空の欄の文言を出さない


def test_single_message_keeps_the_original_layout(tmp_path, fake_slack):
    finder = _finder(tmp_path)
    finder._post_to_slack_in_chunks([])
    texts = [b["text"]["text"] for b in fake_slack.calls[0]["blocks"] if b["type"] == "section"]
    assert "No preprints found today." in texts and "No papers found today." in texts
    assert not texts[0].strip().endswith(")")
    assert SLACK_PAPERS_PER_MESSAGE <= 39  # 固定の11ブロック＋論文で50を超えない


def test_history_records_only_papers_that_reached_slack(tmp_path, fake_slack, monkeypatch):
    finder = _finder(tmp_path)
    monkeypatch.setattr(finder, "find_and_process_papers", lambda: _articles(80))
    fake_slack.fail_on = {2}  # 2通目で失敗

    papers, *_ = asyncio.run(finder.run_daily(post_to_slack=True))

    assert len(papers) == 80
    assert len(fake_slack.calls) == 2  # 失敗したら残りは送らない
    assert _history_dois(tmp_path) == [f"10.1/{i}" for i in range(SLACK_PAPERS_PER_MESSAGE)]

    # 次の実行では、投稿できなかった論文だけが再び候補になる
    retry = finder.select_new_papers(_articles(80))
    assert [row[0] for row in retry] == [f"10.1/{i}" for i in range(SLACK_PAPERS_PER_MESSAGE, 80)]


def test_history_is_written_for_all_papers_when_slack_is_off(tmp_path, monkeypatch):
    finder = _finder(tmp_path)
    monkeypatch.setattr(finder, "find_and_process_papers", lambda: _articles(3))
    asyncio.run(finder.run_daily(post_to_slack=False))
    assert _history_dois(tmp_path) == ["10.1/0", "10.1/1", "10.1/2"]


def _http_error(status, retry_after=None):
    response = requests.Response()
    response.status_code = status
    if retry_after is not None:
        response.headers["Retry-After"] = retry_after
    return requests.HTTPError(f"{status} error", response=response)


def test_arxiv_retry_wait_honors_retry_after_and_backs_off():
    wait = papers_finder._arxiv_retry_wait
    assert wait(_http_error(429, "45"), 0) == 45
    assert wait(_http_error(429), 0) == papers_finder.ARXIV_RETRY_WAITS[0]
    assert wait(_http_error(503), 1) == papers_finder.ARXIV_RETRY_WAITS[1]
    assert wait(_http_error(429, "9999"), 0) == 300  # 長すぎる指定は打ち切る
    assert wait(requests.ConnectionError("reset"), 1) == 6  # 通信エラーは短めに


def test_fetch_arxiv_page_recovers_after_rate_limit(tmp_path, monkeypatch):
    sleeps = []
    responses = [_http_error(429), _http_error(429)]
    feed = b'<feed xmlns="http://www.w3.org/2005/Atom"></feed>'

    class Ok:
        content = feed

        def raise_for_status(self):
            return None

    class Limited:
        def __init__(self, error):
            self.error = error

        def raise_for_status(self):
            raise self.error

    def fake_get(url, params, timeout):
        return Limited(responses.pop(0)) if responses else Ok()

    monkeypatch.setattr(papers_finder.requests, "get", fake_get)
    monkeypatch.setattr(papers_finder, "sleep", sleeps.append)

    root = _finder(tmp_path)._fetch_arxiv_page({"search_query": "x"})

    assert root is not None
    assert sleeps == [30, 60, papers_finder.ARXIV_REQUEST_INTERVAL]


def test_history_rows_keep_canonical_columns(tmp_path):
    finder = _finder(tmp_path)
    finder.record_history(finder.select_new_papers(_articles(2)))
    df = pd.read_csv(tmp_path / "history_test.csv", dtype=str, encoding="utf-8-sig")
    assert list(df.columns) == HISTORY_COLUMNS
