import csv
import json
import os
from datetime import date, datetime, timedelta
from logging import Logger
from time import sleep
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

import defusedxml.ElementTree as ET
import findpapers
import pandas as pd
import requests
from slack_sdk import WebClient
from tqdm import tqdm

from .cli import InteractiveCLIFilter
from .google_sheet import GoogleSheetsUpdater
from .llm_filtering import LLMFilter
from .mattermost_papers_formatter import MattermostPaperPublisher
from .slack_papers_formatter import SlackPaperPublisher
from .telegram_papers_formatter import TelegramPaperPublisher
from .utils import ArticlesProcessor, PubMedClient
from .zulip_papers_formatter import ZulipPaperPublisher


def _patch_findpapers_merge_duplications() -> None:
    """findpapers の ``Search.merge_duplications`` を None 耐性のあるものに差し替える。

    PubMed等から不正レコード（publication_date が None 等）を取得すると、
    ``paper_by_key`` に None 値が混入し、``merge_duplications`` が
    ``'NoneType' object has no attribute 'publication_date'`` で例外を送出する。
    その結果 ``findpapers.search`` 全体が失敗し、**検索結果が丸ごと0件**になる
    （1件の不正レコードで全論文を取りこぼす）。

    site-packages を直接書き換えると再インストールで失われるため、import時に
    ラッパーで置換する。None エントリを事前に除去し、それでも例外が出る場合は
    マージ（重複統合）だけスキップする。重複は後段の ``_deduplicate_by_doi`` と
    history 照合で吸収されるため、スキップしても実害はない。
    """
    try:
        from findpapers.models.search import Search
    except Exception:  # pragma: no cover - findpapers構造変更時のフォールバック
        return

    if getattr(Search.merge_duplications, "_paperbee_patched", False):
        return

    original = Search.merge_duplications

    def safe_merge_duplications(self: Any, similarity_threshold: float = 0.95) -> None:
        # 混入した None エントリを各コレクションから除去
        bad_keys = [k for k, v in self.paper_by_key.items() if v is None]
        for k in bad_keys:
            del self.paper_by_key[k]
        if None in self.papers:
            self.papers = {p for p in self.papers if p is not None}
        try:
            return original(self, similarity_threshold)
        except Exception as e:  # 統合に失敗しても検索結果自体は失わない
            print(f"  [findpapers] merge_duplications をスキップ（{type(e).__name__}: {e}）")
            return None

    safe_merge_duplications._paperbee_patched = True  # type: ignore[attr-defined]
    Search.merge_duplications = safe_merge_duplications  # type: ignore[assignment]


_patch_findpapers_merge_duplications()

# history.csv の正準スキーマ（列順は Slack フォーマッタのインデックスにも依存するため変更不可）
HISTORY_COLUMNS = [
    "DOI", "Date", "PostedDate", "IsPreprint", "Title", "Journal",
    "Keywords", "Preprint", "Abstract_JP", "URL",
]

# 著者検索を分割する際の1バッチあたりの著者数。
# 著者を全員1本のクエリにORで連結するとGET URLが長くなり、PubMed(HTTP 414)や
# arXiv(HTTP 400)がリクエストを拒否するため、この単位で分割して検索・統合する。
AUTHOR_QUERY_BATCH_SIZE = 25


def _strip_trailing_empty(row: List[Any]) -> List[Any]:
    """末尾の空セル（表計算ソフト等が付与する余分なカンマ）を除去する。"""
    end = len(row)
    while end > 0 and (row[end - 1] is None or str(row[end - 1]).strip() == ""):
        end -= 1
    return list(row[:end])


def _normalize_history_row(row: List[Any]) -> Optional[List[Any]]:
    """任意の列数の履歴行を正準10列へ正規化する。DOIが無ければ None。

    過去のスキーマドリフト（Journal列の有無）や表計算ソフトによる列増殖に
    耐えるため、列名ではなく位置と内容から復元する。
    """
    eff = _strip_trailing_empty(row)
    if not eff or not str(eff[0]).strip():
        return None

    if len(eff) == len(HISTORY_COLUMNS):  # 10列: 現行スキーマ
        return list(eff)
    if len(eff) == len(HISTORY_COLUMNS) - 1:  # 9列: 旧スキーマ（Journalなし）
        return list(eff[:5]) + [""] + list(eff[5:])

    # 破損行: DOI と URL を最優先で救出するベストエフォート
    out = {c: "" for c in HISTORY_COLUMNS}
    out["DOI"] = eff[0]
    for f in reversed(eff):
        if isinstance(f, str) and f.startswith("http"):
            out["URL"] = f
            break
    for i, col in enumerate(["Date", "PostedDate", "IsPreprint", "Title"], start=1):
        if i < len(eff):
            out[col] = eff[i]
    return [out[c] for c in HISTORY_COLUMNS]


class PapersFinder:
    """
    A class to find, process, and update a list of papers into a Google Sheet.
    
    (Args descriptions updated implicitly)
    """

    def __init__(
        self,
        root_dir: str,
        spreadsheet_id: str,
        google_credentials_json: str,
        sheet_name: str,
        history_file: str = "history.csv",
        since: Optional[int] = None,
        query: Optional[str] = None,
        query_biorxiv: Optional[str] = None,
        query_pubmed_arxiv: Optional[str] = None,
        query_authors: Optional[str] = None,
        interactive: bool = False,
        llm_filtering: bool = False,
        filtering_prompt: Optional[str] = "",
        llm_provider: Optional[str] = "",
        model: Optional[str] = "",
        llm_api_key: Optional[str] = "", # OPENAI_API_KEY から変更
        filter_unedited: bool = False,
        slack_bot_token: str = "",
        slack_channel_id: str = "",
        telegram_bot_token: str = "",
        telegram_channel_id: str = "",
        zulip_prc: str = "",
        zulip_stream: str = "",
        zulip_topic: str = "",
        mattermost_url: str = "",
        mattermost_token: str = "",
        mattermost_team: str = "",
        mattermost_channel: str = "",
        ncbi_api_key: str = "",
        databases: Optional[List[str]] = None,
        translation_enabled: bool = False,
        translation_provider: str = "ollama",
        translation_model: str = "gpt-oss:20b",
        translation_api_key: str = "",
        translation_prompt: str = "",
        summarization_enabled: bool = False,
        summarization_provider: str = "ollama",
        summarization_model: str = "gemma2",
        summarization_api_key: str = "",
        summarization_prompt: str = "",
    ) -> None:
        self.root_dir: str = root_dir
        self.history_file: str = history_file
        # dates
        self.today: date = date.today()
        self.today_str: str = self.today.strftime("%Y-%m-%d")
        self.yesterday: date = self.today - timedelta(days=since if since is not None else 1)
        self.yesterday_str: str = self.yesterday.strftime("%Y-%m-%d")
        self.until: date = self.today
        self.since: date = self.yesterday
        # search args
        self.limit: int = 1200
        self.limit_per_database: int = 400
        allowed_databases = {"biorxiv", "arxiv", "pubmed"}
        self.databases = databases if databases else ["biorxiv", "pubmed"]
        if not all(db in allowed_databases for db in self.databases):
            e = f"Invalid database(s) in {self.databases}. Allowed values are: {allowed_databases}"
            raise ValueError(e)

        # Google Sheets
        self.google_credentials_json = google_credentials_json
        self.spreadsheet_id: str = spreadsheet_id
        self.sheet_name: str = sheet_name
        # Query and search files
        self.query_biorxiv: Optional[str] = query_biorxiv if query_biorxiv else None
        self.query_pub_arx: Optional[str] = query_pubmed_arxiv
        self.query: Optional[str] = query if query else None
        self.query_authors: Optional[str] = query_authors if query_authors else None
        self.search_file: str = os.path.join(root_dir, f"{self.today_str}.json")
        self.search_file_biorxiv: str = os.path.join(root_dir, f"{self.today_str}_biorxiv.json")
        self.search_file_pub_arx: str = os.path.join(root_dir, f"{self.today_str}_pub_arx.json")
        # Filter
        self.interactive_filtering: bool = interactive
        self.llm_filtering: bool = llm_filtering
        self.llm_provider: str = llm_provider or "openai"
        self.model: str = model or "gpt-3.5-turbo"
        self.filtering_prompt: str = filtering_prompt or ""
        self.llm_api_key: str = llm_api_key or "" # 変更
        self.filter_unedited: bool = filter_unedited
        # Messaging platforms
        self.slack_bot_token: str = slack_bot_token
        self.slack_channel_id: str = slack_channel_id
        self.telegram_bot_token: str = telegram_bot_token
        self.telegram_channel_id: str = telegram_channel_id
        self.zulip_prc: str = zulip_prc
        self.zulip_stream: str = zulip_stream
        self.zulip_topic: str = zulip_topic
        self.mattermost_url: str = mattermost_url
        self.mattermost_token: str = mattermost_token
        self.mattermost_team: str = mattermost_team
        self.mattermost_channel: str = mattermost_channel
        # Logger
        self.logger = Logger("PapersFinder")
        # NCBI API
        self.ncbi_api_key: str = ncbi_api_key
        # TRANSLATION API
        self.translation_enabled = translation_enabled
        self.translation_provider = translation_provider
        self.translation_model = translation_model
        self.translation_api_key = translation_api_key
        self.translation_prompt = translation_prompt
        # SUMMARIZATION API
        self.summarization_enabled = summarization_enabled
        self.summarization_provider = summarization_provider
        self.summarization_model = summarization_model
        self.summarization_api_key = summarization_api_key
        self.summarization_prompt = summarization_prompt

    def _run_findpapers(
        self,
        search_file: str,
        query: str,
        databases: List[str],
        retries: int = 2,
        retry_on_empty: bool = False,
    ) -> List[Dict[str, Any]]:
        """findpapers.search を実行して記事リストを返す。

        bioRxiv コネクタはレート制限等で同一クエリでも間欠的に0件を返すことがあるため、
        リトライして最も多く取得できた結果を採用する。

        Args:
            retries: 追加試行回数（合計 retries+1 回まで実行）。
            retry_on_empty: True の場合、0件でも（例外でなくても）再試行する。
                bioRxiv 検索のように取りこぼしを吸収したいときに使う。
        """
        best: List[Dict[str, Any]] = []
        for attempt in range(retries + 1):
            ok = False
            try:
                findpapers.search(
                    search_file,
                    query,
                    self.since,
                    self.until,
                    self.limit,
                    self.limit_per_database,
                    databases,
                    verbose=False,
                )
                with open(search_file) as papers_file:
                    papers = list(json.load(papers_file)["papers"])
                ok = True
            except Exception as e:
                self.logger.warning(
                    f"findpapers検索失敗 (試行 {attempt + 1}/{retries + 1}, {databases}): {e}"
                )
                papers = []

            if len(papers) > len(best):
                best = papers

            # 成功して非空、または（成功して空だが空リトライ不要）なら終了
            if ok and (papers or not retry_on_empty):
                break
            if attempt < retries:
                sleep(2)
        return best

    def find_and_process_papers(self) -> pd.DataFrame:
        """
        Executes the search for papers based on predefined criteria and processes them.

        Returns:
            pd.DataFrame: A DataFrame containing processed articles.
        """

        print("Searching papers...")
        articles: List[Dict[str, Any]] = []

        if self.query:
            articles = self._run_findpapers(self.search_file, self.query, self.databases)
        else:
            # PubMed/arXiv キーワード検索 (findpapers)
            non_biorxiv_dbs = [db for db in self.databases if db != "biorxiv"]
            if self.query_pub_arx and non_biorxiv_dbs:
                print(f"  PubMed/arXivキーワード検索中... {non_biorxiv_dbs}")
                articles_pub_arx = self._run_findpapers(
                    self.search_file_pub_arx, self.query_pub_arx, non_biorxiv_dbs
                )
                articles.extend(articles_pub_arx)
                print(f"    → {len(articles_pub_arx)}件")

            # bioRxiv 検索 (findpapers)。間欠的な0件返しに備えて空でもリトライする。
            if self.query_biorxiv and "biorxiv" in self.databases:
                print("  bioRxiv検索中...")
                articles_biorxiv = self._run_findpapers(
                    self.search_file_biorxiv, self.query_biorxiv, ["biorxiv"], retry_on_empty=True
                )
                articles.extend(articles_biorxiv)
                print(f"    → {len(articles_biorxiv)}件")

            # PubMed/arXiv 著者検索 (直接API、[Author]/au:フィールド使用)
            if self.query_authors:
                # YAMLリスト形式とカンマ区切り文字列の両方に対応
                if isinstance(self.query_authors, list):
                    authors = [str(a).strip() for a in self.query_authors if str(a).strip()]
                else:
                    authors = [a.strip() for a in str(self.query_authors).split(",") if a.strip()]
                if "pubmed" in self.databases:
                    print(f"  PubMed著者フィールド検索中... ({len(authors)}名)")
                    pubmed_author_articles = self.search_pubmed_by_authors(authors)
                    articles.extend(pubmed_author_articles)
                    print(f"    → {len(pubmed_author_articles)}件")
                if "arxiv" in self.databases:
                    print(f"  arXiv著者フィールド検索中... ({len(authors)}名)")
                    arxiv_author_articles = self.search_arxiv_by_authors(authors)
                    articles.extend(arxiv_author_articles)
                    print(f"    → {len(arxiv_author_articles)}件")

            # 重複除去
            before = len(articles)
            articles = self._deduplicate_by_doi(articles)
            print(f"  重複除去: {before}件 → {len(articles)}件")

        # DOI取得（著者検索でURL設定済みの記事はスキップ）
        doi_extractor = PubMedClient()
        for article in tqdm(articles, desc="DOI取得中"):
            if article.get("url"):
                continue  # 著者検索で設定済み
            if "PubMed" in article["databases"]:
                doi = doi_extractor.get_doi_from_title(article["title"], ncbi_api_key=self.ncbi_api_key)
                article["url"] = f"https://doi.org/{doi}" if doi else None
            else:
                article["url"] = next(
                    (s for s in article["urls"] if s.startswith("https://doi.org")),
                    None,
                )
        articles = [article for article in articles if article.get("url") is not None]

        # history.csv 既読除外（LLM処理・翻訳の前に実施してコスト削減）
        articles = self._filter_by_history(articles)

        # unedited version除外（CrossRef references-count=0）
        if self.filter_unedited:
            articles = self._filter_unedited_by_crossref(articles)

        processor = ArticlesProcessor(
            articles,
            self.today_str,
            translation_enabled=self.translation_enabled,
            translation_provider=self.translation_provider,
            translation_model=self.translation_model,
            translation_api_key=self.translation_api_key,
            translation_prompt=self.translation_prompt,
            summarization_enabled=self.summarization_enabled,
            summarization_provider=self.summarization_provider,
            summarization_model=self.summarization_model,
            summarization_api_key=self.summarization_api_key,
            summarization_prompt=self.summarization_prompt,
        )
        processed_articles = processor.articles
        self.logger.info(f"Found {len(processed_articles)} articles.")

        if self.llm_filtering:
            print("Filtering papers with LLM...")
            llm_filter = LLMFilter(
                processed_articles,
                llm_provider=self.llm_provider,
                model=self.model,
                filtering_prompt=self.filtering_prompt,
                llm_api_key=self.llm_api_key,
            )
            processed_articles = llm_filter.filter_articles()
            self.logger.info(f"Filtered down to {len(processed_articles)} articles using LLM.")

        if self.interactive_filtering:
            print("Filtering papers manually...")
            cli = InteractiveCLIFilter(processed_articles)
            processed_articles = cli.filter_articles()
            self.logger.info(f"Filtered down to {len(processed_articles)} articles manually.")

        processor.articles = processed_articles
        processor.run_llm_processing()
        processed_articles = processor.articles

        columns_to_drop = [col for col in ["abstract", "Authors"] if col in processed_articles.columns]
        if columns_to_drop:
            processed_articles = processed_articles.drop(columns=columns_to_drop)

        return processed_articles

    def search_pubmed_by_authors(self, authors: List[str]) -> List[Dict[str, Any]]:
        """PubMedを著者名フィールドで直接検索する（[Author]フィールド使用）。

        著者を全員1本のクエリにORで連結するとGET URLが長くなりHTTP 414になるため、
        AUTHOR_QUERY_BATCH_SIZE 名ずつに分割してesearchし、得られたPMIDを統合する。
        """
        date_range = (
            f"{self.since.strftime('%Y/%m/%d')}:{self.until.strftime('%Y/%m/%d')}"
            "[Date - Publication]"
        )

        api_key = f"&api_key={self.ncbi_api_key}" if self.ncbi_api_key else ""
        base_url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"

        pmids: List[str] = []
        seen_pmids: set = set()
        for start in range(0, len(authors), AUTHOR_QUERY_BATCH_SIZE):
            author_batch = authors[start : start + AUTHOR_QUERY_BATCH_SIZE]
            author_query = " OR ".join([f'{a}[Author]' for a in author_batch])
            full_query = f"({author_query}) AND ({date_range}) AND has abstract [FILT]"
            try:
                search_url = (
                    f"{base_url}esearch.fcgi?db=pubmed"
                    f"&term={quote(full_query)}"
                    f"&retmax={self.limit_per_database}&retmode=json{api_key}"
                )
                resp = requests.get(search_url, timeout=30)
                batch_pmids = resp.json()["esearchresult"]["idlist"]
            except Exception as e:
                batch_no = start // AUTHOR_QUERY_BATCH_SIZE + 1
                self.logger.error(f"PubMed esearch失敗 (著者バッチ {batch_no}): {e}")
                continue
            for pmid in batch_pmids:
                if pmid not in seen_pmids:
                    seen_pmids.add(pmid)
                    pmids.append(pmid)
            sleep(0.5)

        if not pmids:
            return []

        sleep(0.5)
        result: List[Dict[str, Any]] = []

        for i in range(0, len(pmids), 100):
            batch = ",".join(pmids[i : i + 100])
            fetch_url = f"{base_url}efetch.fcgi?db=pubmed&id={batch}&retmode=xml{api_key}"
            try:
                fetch_resp = requests.get(fetch_url, timeout=60)
                root = ET.fromstring(fetch_resp.content)
            except Exception as e:
                self.logger.error(f"PubMed efetch失敗: {e}")
                continue

            for pub_article in root.findall(".//PubmedArticle"):
                try:
                    medline = pub_article.find(".//MedlineCitation")
                    article_elem = medline.find(".//Article")

                    title_elem = article_elem.find(".//ArticleTitle")
                    title = "".join(title_elem.itertext()) if title_elem is not None else ""

                    abstract_elems = article_elem.findall(".//AbstractText")
                    abstract = " ".join("".join(t.itertext()) for t in abstract_elems)
                    if not abstract:
                        continue

                    author_list: List[str] = []
                    for auth in article_elem.findall(".//Author"):
                        last = auth.find("LastName")
                        fore = auth.find("ForeName")
                        if last is not None:
                            author_list.append(
                                f"{fore.text} {last.text}" if fore is not None else last.text or ""
                            )

                    doi = None
                    for loc in article_elem.findall(".//ELocationID"):
                        if loc.attrib.get("EIdType") == "doi":
                            doi = loc.text
                            break
                    if not doi:
                        continue

                    pub_date = self._extract_pubmed_date(article_elem, medline)
                    if not pub_date:
                        continue

                    kw_list = [f"/ {kw.text}" for kw in medline.findall(".//Keyword") if kw.text]
                    doi_url = f"https://doi.org/{doi}"

                    journal_elem = article_elem.find(".//Journal/Title")
                    journal_name = journal_elem.text if journal_elem is not None else None

                    result.append({
                        "title": title, "abstract": abstract, "authors": author_list,
                        "keywords": kw_list, "databases": ["PubMed"],
                        "publication_date": pub_date,
                        "urls": [doi_url], "url": doi_url, "doi": doi,
                        "selected": None, "citations": None, "comments": None,
                        "categories": None, "number_of_pages": None, "pages": None,
                        "publication": {"title": journal_name} if journal_name else None,
                    })
                except Exception as e:
                    self.logger.error(f"PubMed記事パース失敗: {e}")
                    continue

            sleep(0.5)

        return result

    def search_arxiv_by_authors(self, authors: List[str]) -> List[Dict[str, Any]]:
        """arXiv APIを著者フィールドで直接検索する（au:フィールド使用）。

        著者を全員1本のクエリにORで連結するとGET URLが長くなりHTTP 400になるため、
        AUTHOR_QUERY_BATCH_SIZE 名ずつに分割して検索し、DOIで重複除去して統合する。
        """
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        result: List[Dict[str, Any]] = []
        seen_dois: set = set()

        for start in range(0, len(authors), AUTHOR_QUERY_BATCH_SIZE):
            author_batch = authors[start : start + AUTHOR_QUERY_BATCH_SIZE]
            author_query = " OR ".join([f'au:"{a}"' for a in author_batch])
            url = (
                f"http://export.arxiv.org/api/query"
                f"?search_query={quote(author_query)}"
                f"&sortBy=submittedDate&sortOrder=descending"
                f"&max_results={self.limit_per_database}"
            )
            try:
                resp = requests.get(url, timeout=60)
                root = ET.fromstring(resp.content)
            except Exception as e:
                batch_no = start // AUTHOR_QUERY_BATCH_SIZE + 1
                self.logger.error(f"arXiv著者検索失敗 (著者バッチ {batch_no}): {e}")
                continue

            for entry in root.findall("atom:entry", ns):
                try:
                    pub_elem = entry.find("atom:published", ns)
                    if pub_elem is None:
                        continue
                    pub_str = pub_elem.text[:10]
                    pub_date = datetime.strptime(pub_str, "%Y-%m-%d").date()
                    if not (self.since <= pub_date <= self.until):
                        continue

                    title_elem = entry.find("atom:title", ns)
                    title = title_elem.text.strip() if title_elem is not None else ""

                    summary_elem = entry.find("atom:summary", ns)
                    abstract = summary_elem.text.strip() if summary_elem is not None else ""
                    if not abstract:
                        continue

                    author_list = [
                        a.find("atom:name", ns).text
                        for a in entry.findall("atom:author", ns)
                        if a.find("atom:name", ns) is not None
                    ]

                    id_elem = entry.find("atom:id", ns)
                    if id_elem is None:
                        continue
                    arxiv_id = id_elem.text.split("/abs/")[-1].split("v")[0]
                    doi = f"10.48550/arXiv.{arxiv_id}"
                    if doi in seen_dois:
                        continue
                    seen_dois.add(doi)
                    doi_url = f"https://doi.org/{doi}"

                    result.append({
                        "title": title, "abstract": abstract, "authors": author_list,
                        "keywords": [], "databases": ["arXiv"],
                        "publication_date": pub_str,
                        "urls": [doi_url, f"https://arxiv.org/abs/{arxiv_id}"],
                        "url": doi_url, "doi": doi,
                        "selected": None, "citations": None, "comments": None,
                        "categories": None, "number_of_pages": None, "pages": None,
                        "publication": None,
                    })
                except Exception as e:
                    self.logger.error(f"arXiv記事パース失敗: {e}")
                    continue

            sleep(0.5)

        return result

    def _extract_pubmed_date(self, article_elem: Any, medline: Any) -> Optional[str]:
        """PubMed XMLから出版日を抽出する（優先順位: ArticleDate > PubDate > DateCompleted）"""
        for ad in article_elem.findall(".//ArticleDate"):
            year = ad.findtext("Year")
            if year:
                month = ad.findtext("Month", "01")
                day = ad.findtext("Day", "01")
                return f"{year}-{month.zfill(2)}-{day.zfill(2)}"

        journal = article_elem.find(".//Journal")
        if journal is not None:
            pd_elem = journal.find(".//PubDate")
            if pd_elem is not None:
                year = pd_elem.findtext("Year")
                if year:
                    month = self._month_to_num(pd_elem.findtext("Month", "01"))
                    day = pd_elem.findtext("Day", "01")
                    return f"{year}-{month.zfill(2)}-{day.zfill(2)}"

        dc = medline.find("DateCompleted")
        if dc is not None:
            year = dc.findtext("Year")
            if year:
                month = dc.findtext("Month", "01") or "01"
                day = dc.findtext("Day", "01") or "01"
                return f"{year}-{month.zfill(2)}-{day.zfill(2)}"

        return None

    def _month_to_num(self, month: str) -> str:
        """月名（英語省略形）を2桁数字に変換する"""
        mapping = {
            "Jan": "01", "Feb": "02", "Mar": "03", "Apr": "04",
            "May": "05", "Jun": "06", "Jul": "07", "Aug": "08",
            "Sep": "09", "Oct": "10", "Nov": "11", "Dec": "12",
        }
        return mapping.get(month, month.zfill(2) if month.isdigit() else "01")

    def _deduplicate_by_doi(self, articles: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """DOIまたはタイトルで重複記事を除去する（最初の出現を優先）"""
        seen: set = set()
        unique: List[Dict[str, Any]] = []
        for article in articles:
            key = (article.get("doi") or "").strip().lower() or article.get("title", "").strip().lower()
            if key and key not in seen:
                seen.add(key)
                unique.append(article)
        return unique

    def _history_path(self) -> str:
        """history.csv の絶対パスを返す。"""
        if os.path.isabs(self.history_file):
            return self.history_file
        return os.path.join(self.root_dir, self.history_file)

    def _load_history_df(self, history_file_path: str) -> Optional[pd.DataFrame]:
        """history.csv を堅牢に読み込み、正準スキーマの DataFrame を返す。

        csv モジュールで1行ずつ読み、列数の揺れ（過去の Journal 列ドリフトや
        表計算ソフトによる列増殖）を正準10列へ正規化する。pandas の
        ``on_bad_lines='skip'`` のように行を無言で取りこぼさない。
        読み込めない場合は None。
        """
        if not os.path.exists(history_file_path):
            return None

        for attempt in range(3):
            try:
                with open(history_file_path, encoding="utf-8-sig", newline="") as f:
                    raw_rows = list(csv.reader(f))
                break
            except OSError as e:
                if e.errno == 11 and attempt < 2:  # EDEADLK on macOS (OneDrive同期)
                    sleep(2 ** attempt)
                else:
                    self.logger.error(f"履歴ファイルの読み込みに失敗しました: {e}")
                    return None
            except Exception as e:
                self.logger.error(f"履歴ファイルの読み込みに失敗しました: {e}")
                return None
        else:
            return None

        if not raw_rows:
            return pd.DataFrame(columns=HISTORY_COLUMNS)

        normalized = []
        dropped = 0
        for r in raw_rows[1:]:  # 先頭はヘッダー
            canon = _normalize_history_row(r)
            if canon is None:
                dropped += 1
                continue
            normalized.append(canon)
        if dropped:
            self.logger.warning(f"履歴ファイルでDOI不明の{dropped}行を除外しました: {history_file_path}")
        return pd.DataFrame(normalized, columns=HISTORY_COLUMNS)

    def _write_history_df(self, df: pd.DataFrame, history_file_path: str) -> bool:
        """history.csv を正準スキーマ・QUOTE_ALL で全体書き直し（原子的置換）。"""
        df = df.reindex(columns=HISTORY_COLUMNS, fill_value="")
        tmp_path = f"{history_file_path}.tmp"
        for attempt in range(3):
            try:
                df.to_csv(
                    tmp_path, index=False, header=True,
                    encoding="utf-8-sig", quoting=csv.QUOTE_ALL,
                )
                os.replace(tmp_path, history_file_path)
                return True
            except OSError as e:
                if e.errno == 11 and attempt < 2:  # EDEADLK on macOS
                    sleep(2 ** attempt)
                else:
                    self.logger.error(f"履歴ファイルへの書き込みに失敗しました: {e}")
                    return False
            except Exception as e:
                self.logger.error(f"履歴ファイルへの書き込みに失敗しました: {e}")
                return False
        return False

    def _filter_by_history(self, articles: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """history.csvを参照して既投稿論文をLLM処理前に除外する"""
        history_df = self._load_history_df(self._history_path())
        if history_df is None or "DOI" not in history_df.columns:
            return articles

        published_dois = set(history_df["DOI"].dropna().astype(str).str.strip().str.lower())

        def doi_from_url(url: str) -> str:
            idx = url.find("10.")
            return url[idx:].strip().lower() if idx >= 0 else url.strip().lower()

        filtered = [a for a in articles if doi_from_url(a.get("url", "")) not in published_dois]
        skipped = len(articles) - len(filtered)
        if skipped:
            print(f"  履歴除外: {skipped}件をスキップ（{len(filtered)}件が新規）")
        return filtered

    def _filter_unedited_by_crossref(self, articles: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """CrossRef API の references-count が 0 のジャーナル論文（unedited version）を除外する。
        bioRxiv (10.1101/) や arXiv (10.48550/) のプレプリントは対象外。"""
        PREPRINT_PREFIXES = ("10.1101/", "10.48550/")
        filtered = []
        headers = {"User-Agent": "PaperBee/1.0 (mailto:paperbee@example.com)"}
        skipped = 0

        for article in articles:
            doi = article.get("doi", "")
            if not doi or doi.startswith(PREPRINT_PREFIXES):
                filtered.append(article)
                continue

            try:
                resp = requests.get(
                    f"https://api.crossref.org/works/{doi}",
                    headers=headers,
                    timeout=10,
                )
                if resp.status_code == 200:
                    ref_count = resp.json().get("message", {}).get("references-count", None)
                    if ref_count == 0:
                        skipped += 1
                        sleep(0.3)
                        continue
            except Exception as e:
                self.logger.warning(f"CrossRef参照数チェック失敗 ({doi}): {e}")

            filtered.append(article)
            sleep(0.3)

        if skipped:
            print(f"  unedited除外: {skipped}件をスキップ（CrossRef references-count=0）")
        return filtered

    def update_google_sheet(self, processed_articles: pd.DataFrame, row: int = 2) -> List[List[Any]]:
        return [list(row) for row in processed_articles.values.tolist()]

    def post_paper_to_slack(self, papers: List[List[str]]) -> Any:
        self.slack_publisher: SlackPaperPublisher = SlackPaperPublisher(
            WebClient(self.slack_bot_token),
            Logger("SlackPaperPublisher"),
            channel_id=self.slack_channel_id,
        )
        papers_pub, preprints = self.slack_publisher.format_papers_for_slack(papers)
        response = self.slack_publisher.publish_papers_to_slack(
            papers_pub, preprints, self.today_str, self.spreadsheet_id
        )
        return response

    async def post_paper_to_telegram(self, papers: List[List[str]]) -> Any:
        telegram_publisher = TelegramPaperPublisher(
            Logger("TelegramPaperPublisher"),
            channel_id=self.telegram_channel_id,
            bot_token=self.telegram_bot_token,
        )

        papers_pub, preprints = telegram_publisher.format_papers(papers)
        response = await telegram_publisher.publish_papers(papers_pub, preprints, self.today_str, self.spreadsheet_id)
        return response

    async def post_paper_to_zulip(self, papers: List[List[str]]) -> Any:
        zulip_publisher = ZulipPaperPublisher(
            Logger("ZulipPaperPublisher"),
            prc=self.zulip_prc,
            stream_name=self.zulip_stream,
            topic_name=self.zulip_topic,
        )

        papers_pub, preprints = zulip_publisher.format_papers_for_zulip(papers)
        response = await zulip_publisher.publish_papers_to_zulip(
            papers_pub, preprints, self.today_str, self.spreadsheet_id
        )
        return response

    async def post_paper_to_mattermost(self, papers: List[List[str]]) -> Any:
        mattermost_publisher = MattermostPaperPublisher(
            Logger("MattermostPaperPublisher"),
            url=self.mattermost_url,
            token=self.mattermost_token,
            team=self.mattermost_team,
            channel=self.mattermost_channel,
        )
        response = await mattermost_publisher.publish_papers(papers)
        return response

    def cleanup_files(self) -> None:
        yesterday_file = os.path.join(self.root_dir, f"{self.yesterday_str}.json")
        if os.path.exists(yesterday_file):
            os.remove(yesterday_file)
            print(f"Deleted yesterday's file: {yesterday_file}")
        else:
            print(f"File not found, no deletion needed for: {yesterday_file}")
        yesterday_file_biorxiv = os.path.join(self.root_dir, f"{self.yesterday_str}_biorxiv.json")
        if os.path.exists(yesterday_file_biorxiv):
            os.remove(yesterday_file_biorxiv)
            print(f"Deleted yesterday's file: {yesterday_file_biorxiv}")
        else:
            print(f"File not found, no deletion needed for: {yesterday_file_biorxiv}")
        yesterday_file_pub_arx = os.path.join(self.root_dir, f"{self.yesterday_str}_pub_arx.json")
        if os.path.exists(yesterday_file_pub_arx):
            os.remove(yesterday_file_pub_arx)
            print(f"Deleted yesterday's file: {yesterday_file_pub_arx}")
        else:
            print(f"File not found, no deletion needed for: {yesterday_file_pub_arx}")

    async def run_daily(
        self,
        post_to_slack: bool = True,
        post_to_telegram: bool = False,
        post_to_zulip: bool = False,
        post_to_mattermost: bool = False,
    ) -> Tuple[List[List[Any]], Any | None, Any | None, Any | None, Any | None]:
        processed_articles = self.find_and_process_papers()
        papers = self.update_local_history(processed_articles)

        response_slack = None
        response_telegram = None
        response_zulip = None
        response_mattermost = None

        if post_to_slack:
            print("Posting to Slack...")
            response_slack = self.post_paper_to_slack(papers)

        if post_to_telegram:
            print("Posting to Telegram...")
            response_telegram = await self.post_paper_to_telegram(papers)

        if post_to_zulip:
            print("Posting to Zulip...")
            response_zulip = await self.post_paper_to_zulip(papers)

        if post_to_mattermost:
            print("Posting to Mattermost...")
            response_mattermost = await self.post_paper_to_mattermost(papers)

        self.cleanup_files()

        return papers, response_slack, response_telegram, response_zulip, response_mattermost

    def send_csv(self, user_id: str, user_query: str) -> Tuple[pd.DataFrame, Any]:
        processed_articles = self.find_and_process_papers()
        response = self.slack_publisher._send_csv(
            processed_articles,
            root_dir=self.root_dir,
            user_id=user_id,
            user_query=user_query,
        )
        return processed_articles, response
    
    def update_local_history(self, processed_articles: pd.DataFrame) -> List[List[Any]]:
        history_file_path = self._history_path()

        # 既存履歴を堅牢に読み込む（列ドリフトを正規化して取りこぼさない）
        history_df = self._load_history_df(history_file_path)
        published_dois: set = set()
        if history_df is not None and "DOI" in history_df.columns:
            published_dois = set(history_df["DOI"].dropna().astype(str).str.strip().str.lower())

        # 新規論文のみ抽出（DOIを正規化して照合）
        if not processed_articles.empty and "DOI" in processed_articles.columns:
            norm_doi = processed_articles["DOI"].astype(str).str.strip().str.lower()
            new_articles = processed_articles[~norm_doi.isin(published_dois)]
        else:
            new_articles = processed_articles

        if new_articles.empty:
            self.logger.info("新しい論文は見つかりませんでした（すべて履歴済み）。")
            return []

        # 既存＋新規を正準スキーマで結合し、ファイル全体を書き直す（追記による列ドリフトを根絶）
        new_for_history = new_articles.reindex(columns=HISTORY_COLUMNS, fill_value="")
        if history_df is not None and not history_df.empty:
            combined = pd.concat([history_df, new_for_history], ignore_index=True)
        else:
            combined = new_for_history
        combined = combined.drop_duplicates(
            subset="DOI", keep="first"
        ) if "DOI" in combined.columns else combined

        if self._write_history_df(combined, history_file_path):
            self.logger.info(f"{len(new_articles)} 件の新しい論文を {history_file_path} に保存しました。")

        new_articles = new_for_history

        return new_articles.values.tolist()