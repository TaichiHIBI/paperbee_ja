from datetime import date, datetime
from time import sleep
from typing import List, Optional, Union

import ollama
import os

import defusedxml.ElementTree as ET  # Using defusedxml for security
import pandas as pd
import requests

# google-genai import check
try:
    from google import genai
except ImportError:
    genai = None

def translate_abstract(text: str, provider: str, model_name: str, api_key: str = "", prompt_template: str = "") -> str:
    if not text or not isinstance(text, str):
        return ""
    
    # プロンプトの組み立て
    if "{text}" in prompt_template:
        prompt = prompt_template.format(text=text)
    else:
        prompt = f"{prompt_template}\n\nAbstract:\n{text}"

    try:
        if provider == "ollama":
            import ollama
            response = ollama.chat(model=model_name, messages=[
                {'role': 'user', 'content': prompt},
            ])
            return response['message']['content']

        elif provider == "gemini":
            if genai is None:
                return text + " (Error: google-genai not installed)"
            if not api_key:
                return text + " (Error: API Key missing)"
            
            # v1.0 SDK usage
            client = genai.Client(api_key=api_key)
            response = client.models.generate_content(
                model=model_name,
                contents=prompt
            )
            from time import sleep
            sleep(1) # Rate limit handling
            return response.text
            
        elif provider == "openai":
            from openai import OpenAI
            
            # クライアントの初期化
            client = OpenAI(api_key=api_key)
            
            response = client.chat.completions.create(
                model=model_name,
                messages=[
                    {"role": "system", "content": "You are a helpful assistant for summarizing scientific papers."},
                    {"role": "user", "content": prompt}
                ]
            )
            return response.choices[0].message.content
        # -----------------

        else:
            return text

    except Exception as e:
        print(f"Translation failed ({provider}): {e}")
        return text


class ArticlesProcessor:
    # ... (既存のクラス定義は変更なし) ...
    # __init__ や process_articles などはそのまま維持してください
    
    def __init__(
        self, 
        articles: List[dict], 
        today_str: str, 
        # ↓ 引数を追加
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
        """
        Initializes the ArticlesProcessor with articles data and the current date.

        Args:
            articles (List[dict]): A list of dictionaries where each dictionary contains article data.
            today_str (str): The current date formatted as a string.
        """
        # 要約用の引数
        self.summarization_enabled = summarization_enabled
        self.summarization_provider = summarization_provider
        self.summarization_model = summarization_model
        self.summarization_api_key = summarization_api_key
        self.summarization_prompt = summarization_prompt

        # 翻訳用の引数
        self.translation_enabled = translation_enabled
        self.translation_provider = translation_provider
        self.translation_model = translation_model
        self.translation_api_key = translation_api_key
        self.translation_prompt = translation_prompt

        self.articles = pd.DataFrame.from_dict(articles)
        self.today_str = today_str
        self.process_articles()

    def process_articles(self) -> None:
        self.filter_columns()
        self.extract_doi()
        self.set_dates()
        self.determine_preprint_status()
        self.rename_and_process_columns()
        self.select_last_columns()

    def filter_columns(self) -> None:
        """Filters the DataFrame to include specific columns."""
        columns = ["databases", "publication_date", "title", "authors", "keywords", "url", "abstract"]

        if self.articles.empty:
            self.articles = pd.DataFrame(columns=columns + ["publication"])
            return

        if "publication" not in self.articles.columns:
            self.articles["publication"] = None

        self.articles = self.articles.loc[:, columns + ["publication"]]

    def extract_doi(self) -> None:
        """Extracts DOIs from URLs and adds them as a new column."""
        if not self.articles.empty:
            self.articles["DOI"] = self.articles["url"].apply(lambda x: x[x.find("10.") :])

    def set_dates(self) -> None:
        """Sets the publication date and the date of processing."""
        if not self.articles.empty:
            self.articles["Date"] = self.today_str
            self.articles["PostedDate"] = self.articles["publication_date"]

    def determine_preprint_status(self) -> None:
        """Determines whether each article is a preprint based on its database."""
        if not self.articles.empty:
            self.articles["IsPreprint"] = self.articles["databases"].apply(
                lambda dbs: "FALSE" if "PubMed" in dbs else "TRUE"
            )

    def rename_and_process_columns(self) -> None:
        """Renames columns and processes keywords."""
        if not self.articles.empty:
            self.articles["Title"] = self.articles["title"]
            self.articles["Keywords"] = self.articles["keywords"].apply(lambda kws: ", ".join(kw[2:] for kw in kws))
            self.articles["URL"] = self.articles["url"]
            # 追加: authorsをカンマ区切りの文字列にする
            self.articles["Authors"] = self.articles["authors"].apply(lambda auths: ", ".join(auths) if isinstance(auths, list) else str(auths))
            self.articles["Journal"] = self.articles["publication"].apply(
                lambda x: x.get("name", "") if isinstance(x, dict) else ""
            )
            # ここでは要約・翻訳を行わず、カラムの初期化のみを行う
            self.articles["Abstract_JP"] = ""

    def select_last_columns(self) -> None:
        """Selects and rearranges the final set of columns for the DataFrame."""
        # abstract (原文) は翻訳処理のために必要なので残しておく
        expected_columns = ["DOI", "Date", "PostedDate", "IsPreprint", "Title", "Journal", "Authors", "Keywords", "Preprint", "Abstract_JP", "URL", "abstract"]
        if self.articles.empty:
            self.articles["Preprint"] = []
            self.articles = pd.DataFrame(columns=expected_columns)
        else:
            self.articles["Preprint"] = None
            # カラムが存在しない場合は作成しておく（空のデータフレーム対策）
            for col in expected_columns:
                if col not in self.articles.columns:
                    self.articles[col] = ""
            self.articles = self.articles[expected_columns]

    def run_llm_processing(self) -> None:
        """
        Executes summarization and translation on the current articles.
        This method should be called AFTER filtering to save costs.
        """
        if self.articles.empty:
            return

        if self.summarization_enabled or self.translation_enabled:
            print(f"Processing abstracts for {len(self.articles)} papers...")
            
            # 処理用のテキストカラムを初期化（最初は原文を入れる）
            # abstractカラムがない場合は処理できないためリターン
            if "abstract" not in self.articles.columns:
                return

            self.articles["processing_text"] = self.articles["abstract"]

            # Step 1: 要約 (Summarization)
            if self.summarization_enabled:
                print(f"🔸 Summarizing with {self.summarization_provider} ({self.summarization_model})...")
                self.articles["processing_text"] = self.articles["processing_text"].apply(
                    lambda x: translate_abstract(
                        x, 
                        self.summarization_provider, 
                        self.summarization_model, 
                        self.summarization_api_key,
                        self.summarization_prompt
                    ) if x and isinstance(x, str) else ""
                )

            # Step 2: 翻訳 (Translation)
            if self.translation_enabled:
                print(f"🔹 Translating with {self.translation_provider} ({self.translation_model})...")
                self.articles["processing_text"] = self.articles["processing_text"].apply(
                    lambda x: translate_abstract(
                        x, 
                        self.translation_provider, 
                        self.translation_model, 
                        self.translation_api_key,
                        self.translation_prompt
                    ) if x and isinstance(x, str) else ""
                )

            # 最終結果を Abstract_JP カラムに格納
            self.articles["Abstract_JP"] = self.articles["processing_text"]
            
            # 一時カラムを削除
            self.articles.drop(columns=["processing_text"], inplace=True)


class PubMedClient:
    """
    A client for fetching DOI (Digital Object Identifier) information for publications from PubMed.
    """

    @staticmethod
    def get_doi_from_title(
        title: str,
        seconds_to_wait: float = 1 / 10,
        ncbi_api_key: Optional[str] = None,
        n_retries: int = 3,
    ) -> Optional[str]:
        """
        Retrieve the DOI (Digital Object Identifier) of a publication given its title by querying PubMed's database.

        Args:
            title (str): The title of the publication.
            seconds_to_wait (float): Time in seconds to wait between requests (default is 1/10 seconds).
            ncbi_api_key (Optional[str]): Optional API key for NCBI.

        Returns:
            Optional[str]: The DOI of the publication if found, otherwise None.
        """
        api_key = f"&api_key={ncbi_api_key}" if ncbi_api_key else ""
        base_url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"
        search_url = f"{base_url}esearch.fcgi?db=pubmed&term={title}&retmode=json{api_key}"

        for _ in range(n_retries):
            try:
                search_response = requests.get(search_url, timeout=10)  # Added timeout
                search_data = search_response.json()

                # NCBI does not allow more than 3 requests per second (10 with an API key)
                if seconds_to_wait:
                    sleep(seconds_to_wait)

                pubmed_id = (
                    search_data["esearchresult"]["idlist"][0] if search_data["esearchresult"]["idlist"] else None
                )
                if not pubmed_id:
                    return None
                else:
                    break
            except Exception as e:
                print(f"Error fetching DOI from PubMed: {e}")
                print("Increasing timeout and retrying...")
                seconds_to_wait *= 2

                if seconds_to_wait:
                    sleep(seconds_to_wait)

                continue

        if seconds_to_wait:
            sleep(seconds_to_wait)
        fetch_url = f"{base_url}efetch.fcgi?db=pubmed&id={pubmed_id}&retmode=xml"
        fetch_response = requests.get(fetch_url, timeout=10)  # Added timeout
        root = ET.fromstring(fetch_response.content)  # Using defusedxml for parsing

        for article in root.findall(".//Article"):
            for el in article.findall(".//ELocationID"):
                if el.attrib.get("EIdType") == "doi":
                    return str(el.text)
        return None


def parse_date(date_str: Union[str, date]) -> date:
    """
    Parses a string to a datetime.date object.

    Args:
        date_str (Union[str, datetime.date]): The date string to parse, or a date object.

    Returns:
        datetime.date: A parsed date object.

    Raises:
        ValueError: If the input string is not in the expected format (YYYY-MM-DD).
    """
    if isinstance(date_str, date):
        return date_str
    try:
        return datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError as err:
        e = f"Invalid date format: {date_str}. Expected YYYY-MM-DD."
        raise ValueError(e) from err