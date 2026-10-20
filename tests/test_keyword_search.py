"""キーワード検索（PubMed E-utilities / arXiv API / bioRxiv API の直接検索）のオフラインテスト。"""

import json
from datetime import date, datetime, timedelta

import pytest

from PaperBee.papers import papers_finder
from PaperBee.papers.papers_finder import PapersFinder
from PaperBee.papers.search_query import compile_matcher, normalize_text, parse_query, to_arxiv, to_pubmed

SINCE, UNTIL = date(2026, 10, 18), date(2026, 10, 20)


def _finder(tmp_path, **kwargs):
    finder = PapersFinder(
        root_dir=str(tmp_path),
        spreadsheet_id="",
        google_credentials_json="",
        sheet_name="Papers",
        history_file="history_test.csv",
        **kwargs,
    )
    finder.since, finder.until = SINCE, UNTIL
    return finder


class FakeResponse:
    def __init__(self, payload=None, content=b""):
        self.payload = payload
        self.content = content

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(papers_finder, "sleep", lambda s: None)


# ---- クエリの解釈と変換 ----

CONFIG_STYLE = "([single-cell] OR [scRNA-seq]) AND ([deep learning] OR [AI])"


def test_to_pubmed_uses_tiab_phrases_and_keeps_grouping():
    assert to_pubmed(parse_query(CONFIG_STYLE)) == (
        '("single-cell"[TIAB] OR "scRNA-seq"[TIAB]) AND ("deep learning"[TIAB] OR "AI"[TIAB])'
    )
    assert to_pubmed(parse_query("[a] AND NOT ([b] OR [c])")) == '"a"[TIAB] NOT ("b"[TIAB] OR "c"[TIAB])'


def test_to_arxiv_searches_title_or_abstract_without_hyphens():
    assert to_arxiv(parse_query("[single-cell] AND NOT [review]")) == (
        '(ti:"single cell" OR abs:"single cell") ANDNOT (ti:"review" OR abs:"review")'
    )


def test_and_binds_tighter_than_or():
    assert to_pubmed(parse_query("[a] OR [b] AND [c]")) == '"a"[TIAB] OR ("b"[TIAB] AND "c"[TIAB])'


@pytest.mark.parametrize("query", ["[a] or [b]", "[a] AND", "([a] OR [b]", "[]", "a OR [b]", "[a] [b]"])
def test_invalid_queries_raise(query):
    with pytest.raises(ValueError):
        parse_query(query)


@pytest.mark.parametrize(
    ("query", "text", "expected"),
    [
        ("[single-cell]", "Single cell RNA sequencing", True),  # ハイフンは空白扱い
        ("[single cell]", "single-cell atlas", True),
        ("[diffusion model]", "We train diffusion models on cells", True),  # 最後の語の複数形
        ("[GEM]", "gemcitabine resistance", False),  # 語の途中には一致しない
        ("[AI]", "as said before", False),
        ("[Brbic]", "work by Brbić et al.", True),  # アクセントを無視
        ("[omic*]", "multi-omics integration", True),
        ("[gr?y matter]", "grey matter volume", True),  # 語の途中のワイルドカード
        ("[immun*therapy]", "Immunotherapy response", True),
        ("[*omics]", "spatial proteomics", True),
        (CONFIG_STYLE, "A deep learning model for scRNA-seq data", True),
        (CONFIG_STYLE, "A deep learning model for bulk RNA-seq", False),
        ("[cancer] AND NOT [mouse]", "cancer in mouse models", False),
    ],
)
def test_local_matcher(query, text, expected):
    assert compile_matcher(parse_query(query))(normalize_text(text)) is expected


# ---- bioRxiv ----


def _biorxiv_item(doi, title, abstract="We study cells.", authors="Doe, J.", version="1"):
    return {"doi": doi, "title": title, "abstract": abstract, "authors": authors, "date": "2026-10-19",
            "version": version, "category": "bioinformatics", "type": "new results", "server": "bioRxiv"}


BIORXIV_PAGES = {
    0: {"messages": [{"total": "3"}], "collection": [
        _biorxiv_item("10.1101/a", "Spatial transcriptomics of the gut"),
        _biorxiv_item("10.1101/b", "A protein structure", abstract="Using Visium HD we map tissue.",
                      authors="Theis, F. J."),
    ]},
    2: {"messages": [{"total": "3"}], "collection": [
        _biorxiv_item("10.1101/c", "Spatial transcriptomics revisited", version="2"),  # 改訂版は対象外
    ]},
}


def _fake_biorxiv(requested):
    def fake_get(url, timeout):
        cursor = int(url.split("/")[-2])
        requested.append(cursor)
        return FakeResponse(BIORXIV_PAGES[cursor])

    return fake_get


def test_search_biorxiv_matches_title_and_abstract(tmp_path, monkeypatch):
    requested = []
    monkeypatch.setattr(papers_finder.requests, "get", _fake_biorxiv(requested))

    result = _finder(tmp_path).search_biorxiv("[spatial transcriptomics] OR [Visium HD]")

    assert [a["doi"] for a in result] == ["10.1101/a", "10.1101/b"]
    assert result[0]["url"] == "https://doi.org/10.1101/a"
    assert result[0]["databases"] == ["bioRxiv"]
    assert requested == [0, 2]


def test_biorxiv_keyword_and_author_search_share_one_fetch(tmp_path, monkeypatch):
    requested = []
    monkeypatch.setattr(papers_finder.requests, "get", _fake_biorxiv(requested))
    finder = _finder(tmp_path)

    assert [a["doi"] for a in finder.search_biorxiv("[spatial transcriptomics]")] == ["10.1101/a"]
    assert [a["doi"] for a in finder.search_biorxiv_by_authors(["Fabian J. Theis"])] == ["10.1101/b"]
    assert requested == [0, 2]


def test_biorxiv_cache_is_shared_across_topics_until_it_expires(tmp_path, monkeypatch):
    requested = []
    monkeypatch.setattr(papers_finder.requests, "get", _fake_biorxiv(requested))

    _finder(tmp_path).search_biorxiv("[spatial transcriptomics]")
    assert requested == [0, 2]

    # 別トピック（別インスタンス・同じ root_dir と期間）は API を叩かずキャッシュを使う
    assert [a["doi"] for a in _finder(tmp_path).search_biorxiv("[Visium HD]")] == ["10.1101/b"]
    assert requested == [0, 2]

    # 期間が違えば使わない
    other = _finder(tmp_path)
    other.since = date(2026, 10, 19)
    other.search_biorxiv("[Visium HD]")
    assert requested == [0, 2, 0, 2]

    # 古くなったキャッシュは使わない
    cache_path = tmp_path / papers_finder.BIORXIV_CACHE_FILE
    cache = json.loads(cache_path.read_text(encoding="utf-8"))
    cache["fetched_at"] = (datetime.now() - papers_finder.BIORXIV_CACHE_TTL - timedelta(minutes=1)).isoformat()
    cache_path.write_text(json.dumps(cache), encoding="utf-8")
    _finder(tmp_path).search_biorxiv("[Visium HD]")
    assert requested == [0, 2, 0, 2, 0, 2]


def test_incomplete_biorxiv_fetch_is_not_cached(tmp_path, monkeypatch):
    failures = {"left": 3}  # 1回目の検索では cursor=2 のページが3回とも失敗する

    def fake_get(url, timeout):
        cursor = int(url.split("/")[-2])
        if cursor == 2 and failures["left"] > 0:
            failures["left"] -= 1
            raise ConnectionError("boom")
        return FakeResponse(BIORXIV_PAGES[cursor])

    monkeypatch.setattr(papers_finder.requests, "get", fake_get)
    finder = _finder(tmp_path)

    result = finder.search_biorxiv("[spatial transcriptomics]")

    assert [a["doi"] for a in result] == ["10.1101/a"]  # 取得できた範囲は使う
    assert not (tmp_path / papers_finder.BIORXIV_CACHE_FILE).exists()

    # 同じ実行の次の検索（著者検索）は、途中までの結果を使い回さずに取り直す
    assert [a["doi"] for a in finder.search_biorxiv_by_authors(["Fabian J. Theis"])] == ["10.1101/b"]
    assert (tmp_path / papers_finder.BIORXIV_CACHE_FILE).exists()


# ---- arXiv ----


def _arxiv_entry(arxiv_id, published, title="Deep learning for single-cell data", category="q-bio.GN"):
    return f"""
  <entry>
    <id>http://arxiv.org/abs/{arxiv_id}</id>
    <title>{title}</title>
    <updated>{published}</updated>
    <published>{published}</published>
    <summary>We propose a
      new model.</summary>
    <author><name>Jane Doe</name></author>
    <author><name>John Roe</name></author>
    <arxiv:primary_category term="{category}" scheme="http://arxiv.org/schemas/atom"/>
  </entry>"""


def _arxiv_feed(total, entries):
    # 2026年時点の応答形式: totalResults に属性が無い（findpapers はここで落ちていた）
    return f"""<?xml version='1.0' encoding='UTF-8'?>
<feed xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/" xmlns:arxiv="http://arxiv.org/schemas/atom" xmlns="http://www.w3.org/2005/Atom">
  <id>https://arxiv.org/api/x</id>
  <opensearch:itemsPerPage>2</opensearch:itemsPerPage>
  <opensearch:totalResults>{total}</opensearch:totalResults>
  <opensearch:startIndex>0</opensearch:startIndex>{"".join(entries)}
</feed>""".encode()


def test_search_arxiv_paginates_and_filters_by_submission_date(tmp_path, monkeypatch):
    pages = {
        0: _arxiv_feed(3, [_arxiv_entry("2610.00001v2", "2026-10-19T10:00:00Z"),
                           _arxiv_entry("2610.00002v1", "2026-10-15T10:00:00Z")]),  # 公開遅れを見込んだ範囲内
        2: _arxiv_feed(3, [_arxiv_entry("2609.00003v3", "2026-09-01T10:00:00Z")]),  # 改訂版だけが期間内
    }
    calls = []

    def fake_get(url, params, timeout):
        calls.append((url, params))
        return FakeResponse(content=pages[params["start"]])

    monkeypatch.setattr(papers_finder.requests, "get", fake_get)

    result = _finder(tmp_path).search_arxiv("[single-cell] AND ([deep learning] OR [AI])")

    assert [a["doi"] for a in result] == ["10.48550/arXiv.2610.00001", "10.48550/arXiv.2610.00002"]
    article = result[0]
    assert article["url"] == "https://doi.org/10.48550/arXiv.2610.00001"
    assert article["urls"][1] == "https://arxiv.org/abs/2610.00001"
    assert article["abstract"] == "We propose a new model."
    assert article["authors"] == ["Jane Doe", "John Roe"]
    assert article["keywords"] == ["/ q-bio.GN"]
    assert article["publication_date"] == "2026-10-19"

    assert [p["start"] for _, p in calls] == [0, 2]
    url, params = calls[0]
    assert url == papers_finder.ARXIV_API_URL
    lookback_since = SINCE - timedelta(days=papers_finder.ARXIV_LOOKBACK_DAYS)
    assert params["search_query"] == (
        '((ti:"single cell" OR abs:"single cell") AND ((ti:"deep learning" OR abs:"deep learning") '
        'OR (ti:"AI" OR abs:"AI"))) '
        f"AND submittedDate:[{lookback_since:%Y%m%d}0000 TO {UNTIL:%Y%m%d}2359]"
    )


def test_arxiv_query_error_returns_nothing(tmp_path, monkeypatch):
    error_feed = _arxiv_feed(1, ["""
  <entry>
    <id>http://arxiv.org/api/errors#incorrect_id_format</id>
    <title>Error</title>
    <summary>malformed query</summary>
  </entry>"""])
    monkeypatch.setattr(papers_finder.requests, "get", lambda url, params, timeout: FakeResponse(content=error_feed))

    assert _finder(tmp_path).search_arxiv("[x]") == []


# ---- PubMed ----

EFETCH_XML = b"""<?xml version="1.0" ?>
<PubmedArticleSet>
  <PubmedArticle>
    <MedlineCitation>
      <PMID>111</PMID>
      <Article>
        <Journal><Title>Nature Methods</Title></Journal>
        <ArticleTitle>Deep learning for <i>single-cell</i> data</ArticleTitle>
        <Abstract><AbstractText>Background.</AbstractText><AbstractText>Results.</AbstractText></Abstract>
        <AuthorList><Author><LastName>Doe</LastName><ForeName>Jane</ForeName></Author></AuthorList>
        <ELocationID EIdType="doi">10.1038/s41592-026-00001-1</ELocationID>
        <ArticleDate><Year>2026</Year><Month>10</Month><Day>2</Day></ArticleDate>
      </Article>
      <KeywordList><Keyword>scRNA-seq</Keyword></KeywordList>
    </MedlineCitation>
  </PubmedArticle>
  <PubmedArticle>
    <MedlineCitation>
      <PMID>222</PMID>
      <Article>
        <ArticleTitle>No DOI</ArticleTitle>
        <Abstract><AbstractText>Text.</AbstractText></Abstract>
        <ArticleDate><Year>2026</Year><Month>10</Month><Day>19</Day></ArticleDate>
      </Article>
    </MedlineCitation>
  </PubmedArticle>
</PubmedArticleSet>"""


def test_search_pubmed_uses_entrez_date_and_parses_articles(tmp_path, monkeypatch):
    calls = []

    def fake_get(url, params, timeout):
        calls.append((url, params))
        if url.endswith("esearch.fcgi"):
            return FakeResponse({"esearchresult": {"count": "2", "idlist": ["111", "222"]}})
        return FakeResponse(content=EFETCH_XML)

    monkeypatch.setattr(papers_finder.requests, "get", fake_get)

    result = _finder(tmp_path, ncbi_api_key="KEY").search_pubmed("([single-cell]) AND ([deep learning])")

    assert len(result) == 1  # DOI の無い論文は除く
    article = result[0]
    assert article["title"] == "Deep learning for single-cell data"
    assert article["abstract"] == "Background. Results."
    assert article["url"] == "https://doi.org/10.1038/s41592-026-00001-1"
    assert article["publication"] == {"title": "Nature Methods"}
    assert article["databases"] == ["PubMed"]

    (search_url, search_params), (fetch_url, fetch_params) = calls
    assert search_url.endswith("esearch.fcgi")
    assert search_params["term"] == (
        '("single-cell"[TIAB] AND "deep learning"[TIAB]) '
        "AND (2026/10/18:2026/10/20[Date - Entrez]) AND has abstract [FILT] NOT preprint[pt]"
    )
    assert search_params["api_key"] == "KEY"
    assert fetch_url.endswith("efetch.fcgi")
    assert fetch_params["id"] == "111,222"


# ---- 検索全体 ----


def _stub(doi, db):
    return {"doi": doi, "url": f"https://doi.org/{doi}", "title": doi, "databases": [db]}


def test_search_papers_runs_each_source_and_deduplicates(tmp_path, monkeypatch):
    finder = _finder(tmp_path, query_pubmed_arxiv="[a]", query_biorxiv="[b]", query_authors="Jane Doe",
                     databases=["pubmed", "arxiv", "biorxiv"])
    monkeypatch.setattr(finder, "search_pubmed", lambda q: [_stub("10.1/p", "PubMed")])
    monkeypatch.setattr(finder, "search_arxiv", lambda q: (_ for _ in ()).throw(ValueError("bad query")))
    monkeypatch.setattr(finder, "search_biorxiv", lambda q: [_stub("10.1101/x", "bioRxiv")])
    monkeypatch.setattr(finder, "search_pubmed_by_authors", lambda a: [_stub("10.1/P", "PubMed")])  # 大文字違いの重複
    monkeypatch.setattr(finder, "search_arxiv_by_authors", lambda a: [])
    monkeypatch.setattr(finder, "search_biorxiv_by_authors", lambda a: [_stub("10.1101/y", "bioRxiv")])

    result = finder.search_papers()

    assert [a["doi"] for a in result] == ["10.1/p", "10.1101/x", "10.1101/y"]
    assert finder.search_counts == {
        "PubMedキーワード": 1, "arXivキーワード": 0, "bioRxivキーワード": 1,
        "PubMed著者（1名）": 1, "arXiv著者（1名）": 0, "bioRxiv著者（1名）": 1,
    }


def test_legacy_query_searches_every_database(tmp_path, monkeypatch):
    finder = _finder(tmp_path, query="[a]", databases=["pubmed", "arxiv", "biorxiv"])
    seen = []
    for name in ("search_pubmed", "search_arxiv", "search_biorxiv"):
        monkeypatch.setattr(finder, name, lambda q, name=name: seen.append((name, q)) or [])

    finder.search_papers()

    assert seen == [("search_pubmed", "[a]"), ("search_arxiv", "[a]"), ("search_biorxiv", "[a]")]



def test_pubmed_esearch_excludes_preprints(tmp_path, monkeypatch):
    seen = []

    class Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"esearchresult": {"count": "0", "idlist": []}}

    def fake_get(url, params, timeout):
        seen.append(params["term"])
        return Resp()

    monkeypatch.setattr(papers_finder.requests, "get", fake_get)
    _finder(tmp_path)._pubmed_esearch('"cancer"[TIAB]')
    assert seen[0].endswith("NOT preprint[pt]")
