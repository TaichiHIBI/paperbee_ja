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

    def find_and_process_papers(self) -> pd.DataFrame:
        """
        Executes the search for papers based on predefined criteria and processes them.

        Returns:
            pd.DataFrame: A DataFrame containing processed articles.
        """

        print("Searching papers...")
        articles: List[Dict[str, Any]] = []

        if self.query:
            findpapers.search(
                self.search_file,
                self.query,
                self.since,
                self.until,
                self.limit,
                self.limit_per_database,
                self.databases,
                verbose=False,
            )
            with open(self.search_file) as papers_file:
                articles_dict: List[Dict[str, Any]] = json.load(papers_file)["papers"]
            articles = list(articles_dict)
        else:
            # PubMed/arXiv キーワード検索 (findpapers)
            non_biorxiv_dbs = [db for db in self.databases if db != "biorxiv"]
            if self.query_pub_arx and non_biorxiv_dbs:
                print(f"  PubMed/arXivキーワード検索中... {non_biorxiv_dbs}")
                findpapers.search(
                    self.search_file_pub_arx,
                    self.query_pub_arx,
                    self.since,
                    self.until,
                    self.limit,
                    self.limit_per_database,
                    non_biorxiv_dbs,
                    verbose=False,
                )
                with open(self.search_file_pub_arx) as papers_file:
                    articles_pub_arx: List[Dict[str, Any]] = json.load(papers_file)["papers"]
                articles.extend(articles_pub_arx)
                print(f"    → {len(articles_pub_arx)}件")

            # bioRxiv 検索 (findpapers)
            if self.query_biorxiv and "biorxiv" in self.databases:
                print("  bioRxiv検索中...")
                findpapers.search(
                    self.search_file_biorxiv,
                    self.query_biorxiv,
                    self.since,
                    self.until,
                    self.limit,
                    self.limit_per_database,
                    ["biorxiv"],
                    verbose=False,
                )
                with open(self.search_file_biorxiv) as papers_file:
                    articles_biorxiv: List[Dict[str, Any]] = json.load(papers_file)["papers"]
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
        """PubMedを著者名フィールドで直接検索する（[Author]フィールド使用）"""
        author_query = " OR ".join([f'{a}[Author]' for a in authors])
        date_range = (
            f"{self.since.strftime('%Y/%m/%d')}:{self.until.strftime('%Y/%m/%d')}"
            "[Date - Publication]"
        )
        full_query = f"({author_query}) AND ({date_range}) AND has abstract [FILT]"

        api_key = f"&api_key={self.ncbi_api_key}" if self.ncbi_api_key else ""
        base_url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"

        try:
            search_url = (
                f"{base_url}esearch.fcgi?db=pubmed"
                f"&term={quote(full_query)}"
                f"&retmax={self.limit_per_database}&retmode=json{api_key}"
            )
            resp = requests.get(search_url, timeout=30)
            pmids = resp.json()["esearchresult"]["idlist"]
        except Exception as e:
            self.logger.error(f"PubMed esearch失敗: {e}")
            return []

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
                        "publication": {"name": journal_name} if journal_name else None,
                    })
                except Exception as e:
                    self.logger.error(f"PubMed記事パース失敗: {e}")
                    continue

            sleep(0.5)

        return result

    def search_arxiv_by_authors(self, authors: List[str]) -> List[Dict[str, Any]]:
        """arXiv APIを著者フィールドで直接検索する（au:フィールド使用）"""
        author_query = " OR ".join([f'au:"{a}"' for a in authors])
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
            self.logger.error(f"arXiv著者検索失敗: {e}")
            return []

        ns = {"atom": "http://www.w3.org/2005/Atom"}
        result: List[Dict[str, Any]] = []

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

    def _filter_by_history(self, articles: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """history.csvを参照して既投稿論文をLLM処理前に除外する"""
        if os.path.isabs(self.history_file):
            history_file_path = self.history_file
        else:
            history_file_path = os.path.join(self.root_dir, self.history_file)

        if not os.path.exists(history_file_path):
            return articles

        try:
            history_df = pd.read_csv(history_file_path, on_bad_lines='skip')
            if "DOI" not in history_df.columns:
                return articles
            published_dois = set(history_df["DOI"].dropna().astype(str).str.strip().str.lower())
        except OSError as e:
            if e.errno == 11:  # EDEADLK on macOS (OneDrive sync conflict)
                sleep(3)
                try:
                    history_df = pd.read_csv(history_file_path, on_bad_lines='skip')
                    if "DOI" not in history_df.columns:
                        return articles
                    published_dois = set(history_df["DOI"].dropna().astype(str).str.strip().str.lower())
                except Exception as e2:
                    self.logger.error(f"履歴ファイルの読み込みに失敗しました（リトライ後）: {e2}")
                    return articles
            else:
                self.logger.error(f"履歴ファイルの読み込みに失敗しました: {e}")
                return articles
        except Exception as e:
            self.logger.error(f"履歴ファイルの読み込みに失敗しました: {e}")
            return articles

        def doi_from_url(url: str) -> str:
            idx = url.find("10.")
            return url[idx:].strip().lower() if idx >= 0 else url.strip().lower()

        filtered = [a for a in articles if doi_from_url(a.get("url", "")) not in published_dois]
        skipped = len(articles) - len(filtered)
        if skipped:
            print(f"  履歴除外: {skipped}件をスキップ（{len(filtered)}件が新規）")
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
        if os.path.isabs(self.history_file):
            history_file_path = self.history_file
        else:
            history_file_path = os.path.join(self.root_dir, self.history_file)
        
        if os.path.exists(history_file_path):
            for attempt in range(3):
                try:
                    history_df = pd.read_csv(history_file_path, on_bad_lines='skip')
                    if "DOI" in history_df.columns:
                        published_dois = history_df["DOI"].tolist()
                        new_articles = processed_articles[~processed_articles["DOI"].isin(published_dois)]
                    else:
                        new_articles = processed_articles
                    break
                except OSError as e:
                    if e.errno == 11 and attempt < 2:  # EDEADLK on macOS
                        sleep(2 ** attempt)
                    else:
                        self.logger.error(f"履歴ファイルの読み込みに失敗しました: {e}")
                        new_articles = processed_articles
                        break
                except Exception as e:
                    self.logger.error(f"履歴ファイルの読み込みに失敗しました: {e}")
                    new_articles = processed_articles
                    break
            else:
                new_articles = processed_articles
        else:
            new_articles = processed_articles

        if new_articles.empty:
            self.logger.info("新しい論文は見つかりませんでした（すべて履歴済み）。")
            return []

        for attempt in range(3):
            try:
                mode = 'a' if os.path.exists(history_file_path) else 'w'
                header = not os.path.exists(history_file_path)
                new_articles.to_csv(history_file_path, mode=mode, index=False, header=header, encoding='utf-8-sig')
                self.logger.info(f"{len(new_articles)} 件の新しい論文を {history_file_path} に保存しました。")
                break
            except OSError as e:
                if e.errno == 11 and attempt < 2:  # EDEADLK on macOS
                    sleep(2 ** attempt)
                else:
                    self.logger.error(f"履歴ファイルへの書き込みに失敗しました: {e}")
                    break
            except Exception as e:
                self.logger.error(f"履歴ファイルへの書き込みに失敗しました: {e}")
                break

        return new_articles.values.tolist()