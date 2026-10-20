from datetime import date

import pandas as pd
import pytest

from PaperBee.papers import papers_finder
from PaperBee.papers.papers_finder import PapersFinder, author_match_key


def _finder(tmp_path, **kwargs):
    finder = PapersFinder(
        root_dir=str(tmp_path),
        spreadsheet_id="",
        google_credentials_json="",
        sheet_name="Papers",
        history_file="history_test.csv",
        **kwargs,
    )
    finder.since = date(2026, 10, 18)
    finder.until = date(2026, 10, 20)
    return finder


@pytest.mark.parametrize(
    ("query_name", "biorxiv_name"),
    [
        ("Fabian J. Theis", "Theis, F. J."),
        ("F. J. Theis", "Theis, F."),
        ("Dana Pe'er", "Pe'er, D."),
        ("David van Dijk", "van Dijk, D."),
        ("Jean-Philippe Vert", "Vert, J.-P."),
        ("Theis FJ", "Theis, F. J."),
        ("Pe'er D", "Pe'er, D."),
        ("Ma J", "Ma, J."),
    ],
)
def test_author_match_key_matches_across_formats(query_name, biorxiv_name):
    assert author_match_key(query_name) == author_match_key(biorxiv_name)


def test_author_match_key_requires_given_name_initial():
    assert author_match_key("Theis") is None
    assert author_match_key("Fabian Theis") != author_match_key("Theis, J.")


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


def test_search_biorxiv_by_authors_paginates_and_matches(tmp_path, monkeypatch):
    def item(doi, authors, version="1"):
        return {"doi": doi, "title": f"T {doi}", "authors": authors, "abstract": "abs", "date": "2026-10-19",
                "version": version, "category": "bioinformatics"}

    pages = {
        0: {"messages": [{"total": "3"}], "collection": [
            item("10.1101/a", "Smith, J.; Theis, F. J."),
            item("10.1101/b", "Theis, A.; Doe, J."),  # 姓は同じだが頭文字が違う
        ]},
        2: {"messages": [{"total": "3"}], "collection": [
            item("10.1101/c", "Pe'er, D.", version="2"),  # 改訂版は対象外
        ]},
    }
    requested = []

    urls = []

    def fake_get(url, timeout):
        urls.append(url)
        cursor = int(url.split("/")[-2])
        requested.append(cursor)
        return FakeResponse(pages[cursor])

    monkeypatch.setattr(papers_finder.requests, "get", fake_get)
    monkeypatch.setattr(papers_finder, "sleep", lambda s: None)

    result = _finder(tmp_path).search_biorxiv_by_authors(["Fabian J. Theis", "Dana Pe'er"])

    assert requested == [0, 2]
    assert [r["doi"] for r in result] == ["10.1101/a"]
    assert result[0]["authors"] == ["J. Smith", "F. J. Theis"]
    assert result[0]["databases"] == ["bioRxiv"]
    assert urls[0] == f"{papers_finder.BIORXIV_API_URL}/2026-10-18/2026-10-20/0/json"


def test_rejected_papers_are_not_judged_again(tmp_path, monkeypatch):
    judged = []

    class FakeFilter:
        def __init__(self, df, **kwargs):
            self.df = df
            self.decisions = {}

        def filter_articles(self):
            judged.extend(self.df["DOI"])
            for index, row in self.df.iterrows():
                relevant = "keep" in row["Title"]
                self.decisions[index] = (relevant, "claude", "reason")
            return self.df[self.df["Title"].str.contains("keep")]

    monkeypatch.setattr(papers_finder, "LLMFilter", FakeFilter)
    articles = pd.DataFrame({"DOI": ["10.1/keep", "10.1/drop"], "Title": ["keep me", "drop me"]})

    finder = _finder(tmp_path, llm_filtering=True, llm_provider="claude", model="claude-opus-5-5",
                     filtering_prompt="criteria")
    assert list(finder._apply_llm_filter(articles)["DOI"]) == ["10.1/keep"]
    assert judged == ["10.1/keep", "10.1/drop"]

    log = pd.read_csv(tmp_path / "history_test_decisions.csv", dtype=str, encoding="utf-8-sig")
    assert set(log["Relevant"]) == {"TRUE", "FALSE"}

    # 翌日: 不採用だった論文は再判定しない（採用分は本来 history.csv で除外される）
    judged.clear()
    finder._apply_llm_filter(articles)
    assert judged == ["10.1/keep"]

    # 判定基準を変えたらキャッシュは使わない
    judged.clear()
    _finder(tmp_path, llm_filtering=True, llm_provider="claude", model="claude-opus-5-5",
            filtering_prompt="new criteria")._apply_llm_filter(articles)
    assert judged == ["10.1/keep", "10.1/drop"]


def test_unedited_filter_skips_preprints_without_querying_crossref(tmp_path, monkeypatch):
    queried = []

    class Resp:
        status_code = 200

        def json(self):
            return {"message": {"references-count": 0}}

    def fake_get(url, headers, timeout):
        queried.append(url)
        return Resp()

    monkeypatch.setattr(papers_finder.requests, "get", fake_get)
    monkeypatch.setattr(papers_finder, "sleep", lambda s: None)
    articles = [
        {"doi": "10.64898/2026.10.15.759509", "databases": ["bioRxiv"]},  # bioRxiv の新しい接頭辞
        {"doi": "10.64898/2026.10.16.759756", "databases": []},
        {"doi": "10.48550/arXiv.2610.00001", "databases": ["arXiv"]},
        {"doi": "10.1038/s41592-026-00001-1", "databases": ["PubMed"]},  # 参照数0のジャーナル論文
    ]

    kept = _finder(tmp_path)._filter_unedited_by_crossref(articles)

    assert [a["doi"] for a in kept] == [a["doi"] for a in articles[:3]]
    assert queried == ["https://api.crossref.org/works/10.1038/s41592-026-00001-1"]


def _paper(doi, authors):
    return {"doi": doi, "authors": authors}


def test_filter_by_senior_position_only_restricts_common_names():
    wanted = {author_match_key(n) for n in ["Bo Wang", "Fabian J. Theis"]}
    common = {author_match_key("Bo Wang")}
    papers = [
        _paper("senior", ["A One", "B Two", "Bo Wang"]),  # 同姓同名が多いが末尾 → 残す
        _paper("middle", ["Bo Wang", "B Two", "C Three", "D Four"]),  # 先頭の Wang B は別人の可能性が高い → 落とす
        _paper("distinct", ["Fabian J. Theis", "B Two", "C Three", "D Four"]),  # 珍しい名前は位置を問わない
        _paper("unknown", ["Consortium X"]),  # 対象著者が見つからない → 判断できないので残す
    ]
    kept = papers_finder.filter_by_senior_position(papers, wanted, common, positions=2)
    assert [p["doi"] for p in kept] == ["senior", "distinct", "unknown"]


def test_auto_common_names_use_cached_pubmed_counts(tmp_path, monkeypatch):
    requested = []

    class Resp:
        def __init__(self, count):
            self.count = count

        def raise_for_status(self):
            return None

        def json(self):
            return {"esearchresult": {"count": str(self.count)}}

    def fake_get(url, params, timeout):
        requested.append(params["term"])
        return Resp(7567 if params["term"].startswith("Wang B") else 45)

    monkeypatch.setattr(papers_finder.requests, "get", fake_get)
    monkeypatch.setattr(papers_finder, "sleep", lambda s: None)
    authors = ["Wang B", "Bo Wang", "Fabian J. Theis"]

    finder = _finder(tmp_path, author_common_names="auto")
    assert finder._resolve_common_name_keys(authors) == {author_match_key("Bo Wang")}
    assert len(requested) == 3

    # 翌日の実行では保存した論文数を使い、PubMed に問い合わせない
    requested.clear()
    assert _finder(tmp_path, author_common_names="auto")._resolve_common_name_keys(authors) == {
        author_match_key("Bo Wang")
    }
    assert requested == []


def test_common_names_from_list_or_off(tmp_path):
    authors = ["Bo Wang", "Fabian J. Theis"]
    listed = _finder(tmp_path, author_common_names=["Bo Wang"])
    assert listed._resolve_common_name_keys(authors) == {author_match_key("Bo Wang")}
    assert _finder(tmp_path)._resolve_common_name_keys(authors) == set()  # 既定は off
    assert _finder(tmp_path, author_common_names=["Bo Wang"], author_senior_positions=0)._resolve_common_name_keys(
        authors
    ) == set()


def test_slack_format_adds_reason_under_journal():
    from PaperBee.papers.slack_papers_formatter import SlackPaperPublisher

    row = ["10.1/ABC", "2026-10-20", "2026-10-19", "FALSE", "Title", "Nature Methods", "", "", "- 要点", "https://doi.org/10.1/ABC"]
    papers, _ = SlackPaperPublisher.format_papers_for_slack([row], reasons={"10.1/abc": "Theis 研の新手法"})
    assert papers[0] == (
        ":rolled_up_newspaper: <https://doi.org/10.1/ABC|Title>\n> *Nature Methods*\n> :bulb: Theis 研の新手法\n> - 要点"
    )
    # 理由が無ければ従来どおり
    papers, _ = SlackPaperPublisher.format_papers_for_slack([row])
    assert ":bulb:" not in papers[0]


def test_llm_reasons_are_kept_only_for_adopted_papers(tmp_path, monkeypatch):
    class FakeFilter:
        def __init__(self, df, **kwargs):
            self.df = df
            self.decisions = {}

        def filter_articles(self):
            for index, row in self.df.iterrows():
                self.decisions[index] = ("keep" in row["Title"], "claude", f"reason for {row['Title']}")
            return self.df[self.df["Title"].str.contains("keep")]

    monkeypatch.setattr(papers_finder, "LLMFilter", FakeFilter)
    finder = _finder(tmp_path, llm_filtering=True, llm_provider="claude", model="claude-opus-5-5", filtering_prompt="c")
    finder._apply_llm_filter(pd.DataFrame({"DOI": ["10.1/KEEP", "10.1/drop"], "Title": ["keep me", "drop me"]}))
    assert finder.llm_reasons == {"10.1/keep": "reason for keep me"}
