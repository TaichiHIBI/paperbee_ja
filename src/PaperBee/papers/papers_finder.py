import json
import os
from datetime import date, timedelta
from logging import Logger
from typing import Any, Dict, List, Optional, Tuple

import findpapers
import pandas as pd
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
            if not self.query_biorxiv or not self.query_pub_arx:
                e = "Both query_biorxiv and query_pubmed_arxiv must be provided if query is not provided."
                raise ValueError(e)

            findpapers.search(
                self.search_file_pub_arx,
                self.query_pub_arx,
                self.since,
                self.until,
                self.limit,
                self.limit_per_database,
                [
                    database for database in self.databases if database != "biorxiv"
                ],  # Biorxiv requires a different query
                verbose=False,
            )
            if "biorxiv" in self.databases:
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
            with open(self.search_file_pub_arx) as papers_file:
                articles_pub_arx_dict: List[Dict[str, Any]] = json.load(papers_file)["papers"]
            with open(self.search_file_biorxiv) as papers_file:
                articles_biorxiv_dict: List[Dict[str, Any]] = json.load(papers_file)["papers"]
            articles = articles_pub_arx_dict + articles_biorxiv_dict

        doi_extractor = PubMedClient()
        for article in tqdm(articles):
            if "PubMed" in article["databases"]:
                doi = doi_extractor.get_doi_from_title(article["title"], ncbi_api_key=self.ncbi_api_key)
                article["url"] = f"https://doi.org/{doi}" if doi else None
            else:
                article["url"] = next(
                    (s for s in article["urls"] if s.startswith("https://doi.org")),
                    None,
                )
        articles = [article for article in articles if article.get("url") is not None]
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
            summarization_prompt=self.summarization_prompt
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
                llm_api_key=self.llm_api_key, # 変更
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

        if "abstract" in processed_articles.columns:
            processed_articles = processed_articles.drop(columns=["abstract"])

        return processed_articles

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
            try:
                history_df = pd.read_csv(history_file_path)
                if "DOI" in history_df.columns:
                    published_dois = history_df["DOI"].tolist()
                    new_articles = processed_articles[~processed_articles["DOI"].isin(published_dois)]
                else:
                    new_articles = processed_articles
            except Exception as e:
                self.logger.error(f"履歴ファイルの読み込みに失敗しました: {e}")
                new_articles = processed_articles
        else:
            new_articles = processed_articles

        if new_articles.empty:
            self.logger.info("新しい論文は見つかりませんでした（すべて履歴済み）。")
            return []

        try:
            mode = 'a' if os.path.exists(history_file_path) else 'w'
            header = not os.path.exists(history_file_path)
            new_articles.to_csv(history_file_path, mode=mode, index=False, header=header, encoding='utf-8-sig')
            self.logger.info(f"{len(new_articles)} 件の新しい論文を {history_file_path} に保存しました。")
        except Exception as e:
            self.logger.error(f"履歴ファイルへの書き込みに失敗しました: {e}")

        return new_articles.values.tolist()