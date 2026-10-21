import csv
import hashlib
import json
import os
import re
import unicodedata
from collections import Counter
from datetime import date, datetime, timedelta
from functools import partial
from logging import Logger
from time import sleep
from typing import Any, Callable, Dict, List, Optional, Tuple

import defusedxml.ElementTree as ET
import pandas as pd
import requests
from slack_sdk import WebClient

from .claude_client import usage_summary as claude_usage_summary
from .cli import InteractiveCLIFilter
from .google_sheet import GoogleSheetsUpdater
from .llm_filtering import LLMFilter
from .mattermost_papers_formatter import MattermostPaperPublisher
from .search_query import compile_matcher, normalize_text, parse_query, to_arxiv, to_pubmed
from .slack_papers_formatter import SlackPaperPublisher
from .telegram_papers_formatter import TelegramPaperPublisher
from .utils import ArticlesProcessor
from .zulip_papers_formatter import ZulipPaperPublisher

# history.csv の正準スキーマ（列順は Slack フォーマッタのインデックスにも依存するため変更不可）
HISTORY_COLUMNS = [
    "DOI", "Date", "PostedDate", "IsPreprint", "Title", "Journal",
    "Keywords", "Preprint", "Abstract_JP", "URL",
]

# 著者検索を分割する際の1バッチあたりの著者数。
# 著者を全員1本のクエリにORで連結するとGET URLが長くなり、PubMed(HTTP 414)や
# arXiv(HTTP 400)がリクエストを拒否するため、この単位で分割して検索・統合する。
AUTHOR_QUERY_BATCH_SIZE = 25

BIORXIV_API_URL = "https://api.biorxiv.org/details/biorxiv"
# 期間内の bioRxiv 新着は全トピックで同じなので、取得し終えたら root_dir に保存して
# cron で続けて走る他トピックと共有する。新着は日中も増えるため、保存から一定時間で取り直す。
BIORXIV_CACHE_FILE = "biorxiv_cache.json"
BIORXIV_CACHE_TTL = timedelta(hours=6)
BIORXIV_CACHE_FIELDS = ("doi", "title", "abstract", "authors", "date", "category", "version")

ARXIV_API_URL = "https://export.arxiv.org/api/query"
ARXIV_PAGE_SIZE = 200
ARXIV_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "opensearch": "http://a9.com/-/spec/opensearch/1.1/",
    "arxiv": "http://arxiv.org/schemas/atom",
}
# arXiv は投稿から公開（announce）まで数日空くことがある（木曜午後〜月曜午前の投稿は月曜夜（米東部）の
# 公開）。初版の投稿日（published）で期間を切ると週末をまたいだ論文が公開時点で既に期間外になるため、
# 期間の始まりをこの日数だけ前に広げる。前回までに見た論文は history と判定キャッシュで除外される。
ARXIV_LOOKBACK_DAYS = 4
# arXiv API は連続アクセスに間隔を求め、超えると 429（混雑時は 503）を返す（3秒間隔・3〜12秒の再試行では
# 回復しないことがある）。通常の間隔を広めに取り、429・503 では Retry-After
# （無ければ下の秒数）だけ待って再試行する
ARXIV_REQUEST_INTERVAL = 5
ARXIV_RETRY_WAITS = (30, 60, 120)

PUBMED_EUTILS_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"
# PubMed の期間は収載日（Entrez Date）で切る。出版日（Date - Publication）で切ると、出版日より遅れて
# 収載された論文は収載時点で既に期間外になり、どの日の検索にも掛からない。
PUBMED_DATE_FIELD = "Date - Entrez"

# 同姓同名の多い著者名（"Wang B" など）は、著者検索で別人の論文が大量に掛かる。PubMed で過去1年の
# 論文数がしきい値を超える名前をそう判定し、その名前にしか一致しない論文は、著者リストの末尾
# （責任著者側）にいる場合だけ候補にする。2026/10 の実測では、研究室主宰者は年150件以下、
# ノイズ源の名前は年376件以上だった。論文数は名前ごとに root_dir に保存し、一定期間で取り直す。
DEFAULT_COMMON_NAME_THRESHOLD = 300
AUTHOR_COUNT_CACHE_FILE = "author_pubmed_counts.json"
AUTHOR_COUNT_TTL = timedelta(days=30)

# Slack は1メッセージ50ブロックまで。固定のブロック（見出し・区切り・フッター）を除いた残りに収まる論文数で分割する
SLACK_PAPERS_PER_MESSAGE = 35

# LLM判定ログ（{履歴ファイル名}_decisions.csv）の列と保持期間
DECISION_COLUMNS = ["DOI", "Date", "PromptKey", "Stage", "Relevant", "Reason", "Title"]
DECISION_RETENTION_DAYS = 60
# 判定キャッシュとして再利用してよい判定段階（代行判定と判定不能は次回やり直す）
CACHEABLE_STAGES = {"prefilter", "claude", "ollama", "openai", "gemini"}

# 姓の前に付く前置詞（"David van Dijk" の姓は "van Dijk"）と、姓の後ろの敬称
_NAME_PARTICLES = {"van", "von", "de", "der", "den", "da", "di", "del", "della", "du", "dos", "das", "le", "la", "ter", "ten", "zu"}
_NAME_SUFFIXES = {"jr", "sr", "ii", "iii", "iv"}


def _normalize_name(text: str) -> str:
    """アクセント・記号・空白を除いて小文字化する（"Pe'er" → "peer", "van Dijk" → "vandijk"）。"""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if ch.isalnum() and not unicodedata.combining(ch)).lower()


def author_match_key(name: str) -> Optional[Tuple[str, str]]:
    """著者名を照合用の (正規化した姓, 名の頭文字) にする。

    "Fabian J. Theis"（検索設定の書式）、"Theis FJ"（PubMed の書式）、"Theis, F. J."（bioRxiv API の書式）が
    同じキーになる。名が無く頭文字で照合できない名前は None。
    """
    name = name.strip()
    if "," in name:
        surname_part, _, given_part = name.partition(",")
    else:
        tokens = name.split()
        while len(tokens) > 2 and _normalize_name(tokens[-1]) in _NAME_SUFFIXES:
            tokens = tokens[:-1]
        if len(tokens) < 2:
            return None
        last = tokens[-1]
        if len(last) <= 3 and last.isascii() and last.isalpha() and last.isupper():
            # PubMed 形式（姓 + 名の頭文字の大文字列）
            surname, given = _normalize_name(" ".join(tokens[:-1])), last.lower()
            return (surname, given[0]) if surname else None
        start = len(tokens) - 1
        for i in range(1, len(tokens) - 1):
            if tokens[i].lower() in _NAME_PARTICLES:
                start = i
                break
        surname_part, given_part = " ".join(tokens[start:]), " ".join(tokens[:start])
    surname, given = _normalize_name(surname_part), _normalize_name(given_part)
    if not surname or not given:
        return None
    return surname, given[0]


def filter_by_senior_position(
    articles: List[Dict[str, Any]], wanted: set, common: set, positions: int
) -> List[Dict[str, Any]]:
    """著者検索の結果から、同姓同名の多い名前（common）にしか一致しない論文を絞る。

    残すのは、common 以外の対象著者を含む論文と、common の著者が著者リストの末尾 positions 人に
    入っている論文。対象著者が著者リスト上で見つからない論文は判断できないので残す。
    """
    if not common or positions <= 0:
        return articles
    kept = []
    for article in articles:
        keys = [author_match_key(str(a)) for a in article.get("authors") or []]
        matched = [(i, key) for i, key in enumerate(keys) if key in wanted]
        if not matched or any(key not in common or i >= len(keys) - positions for i, key in matched):
            kept.append(article)
    return kept


def _arxiv_retry_wait(error: Exception, attempt: int) -> float:
    """arXiv の再試行までの待ち時間（秒）。429・503 は Retry-After を優先し、無ければ ARXIV_RETRY_WAITS。"""
    response = getattr(error, "response", None)
    status = getattr(response, "status_code", None)
    if status in (429, 503):
        retry_after = str(response.headers.get("Retry-After", "")).strip() if response is not None else ""
        if retry_after.isdigit():
            return float(min(max(int(retry_after), ARXIV_REQUEST_INTERVAL), 300))
        return float(ARXIV_RETRY_WAITS[min(attempt, len(ARXIV_RETRY_WAITS) - 1)])
    return float(3 * 2**attempt)  # 通信エラーなどは短めに


def _biorxiv_display_name(name: str) -> str:
    """"Theis, F. J." → "F. J. Theis"（他DBの著者表記にそろえる）"""
    surname, sep, given = name.partition(",")
    return f"{given.strip()} {surname.strip()}" if sep else name.strip()


def _biorxiv_author_names(item: Dict[str, Any]) -> List[str]:
    return [n.strip() for n in str(item.get("authors", "")).split(";") if n.strip()]


def _biorxiv_item_to_article(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """bioRxiv API の1レコードを記事 dict にする。新着（version 1）でDOIとアブストラクトがあるものだけ。"""
    if str(item.get("version", "1")) != "1":
        return None  # 改訂版は新着ではない
    doi = str(item.get("doi") or "").strip()
    abstract = str(item.get("abstract") or "").strip()
    if not doi or not abstract:
        return None
    doi_url = f"https://doi.org/{doi}"
    category = str(item.get("category") or "").strip()
    return {
        "title": str(item.get("title") or "").strip(), "abstract": abstract,
        "authors": [_biorxiv_display_name(n) for n in _biorxiv_author_names(item)],
        "keywords": [f"/ {category}"] if category else [], "databases": ["bioRxiv"],
        "publication_date": item.get("date"),
        "urls": [doi_url], "url": doi_url, "doi": doi,
        "selected": None, "citations": None, "comments": None,
        "categories": None, "number_of_pages": None, "pages": None,
        "publication": None,
    }


def _arxiv_entry_to_article(entry: Any, since: date, until: date) -> Optional[Dict[str, Any]]:
    """arXiv API の entry 要素を記事 dict にする。初版の投稿日が期間外か、アブストラクトが無ければ None。

    DOI は arXiv が全論文に振る 10.48550/arXiv.{ID}（ジャーナル掲載後の DOI ではなく）にそろえる。
    """
    published = (entry.findtext("atom:published", default="", namespaces=ARXIV_NS) or "")[:10]
    try:
        pub_date = datetime.strptime(published, "%Y-%m-%d").date()
    except ValueError:
        return None
    if not since <= pub_date <= until:
        return None  # 期間内に改訂版（v2以降）が出ただけの論文もここで落ちる
    abstract = " ".join((entry.findtext("atom:summary", default="", namespaces=ARXIV_NS) or "").split())
    id_match = re.search(r"/abs/(.+?)(?:v\d+)?$", entry.findtext("atom:id", default="", namespaces=ARXIV_NS) or "")
    if not abstract or not id_match:
        return None
    arxiv_id = id_match.group(1)
    doi = f"10.48550/arXiv.{arxiv_id}"
    doi_url = f"https://doi.org/{doi}"
    authors = [a.findtext("atom:name", default="", namespaces=ARXIV_NS) for a in entry.findall("atom:author", ARXIV_NS)]
    category_elem = entry.find("arxiv:primary_category", ARXIV_NS)
    category = category_elem.attrib.get("term", "") if category_elem is not None else ""
    return {
        "title": " ".join((entry.findtext("atom:title", default="", namespaces=ARXIV_NS) or "").split()),
        "abstract": abstract, "authors": [a for a in authors if a],
        "keywords": [f"/ {category}"] if category else [], "databases": ["arXiv"],
        "publication_date": published,
        "urls": [doi_url, f"https://arxiv.org/abs/{arxiv_id}"],
        "url": doi_url, "doi": doi,
        "selected": None, "citations": None, "comments": None,
        "categories": None, "number_of_pages": None, "pages": None,
        "publication": None,
    }


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
        author_common_names: Any = "off",
        author_senior_positions: int = 2,
        author_common_threshold: int = DEFAULT_COMMON_NAME_THRESHOLD,
        interactive: bool = False,
        llm_filtering: bool = False,
        filtering_prompt: Optional[str] = "",
        llm_provider: Optional[str] = "",
        model: Optional[str] = "",
        llm_api_key: Optional[str] = "", # OPENAI_API_KEY から変更
        local_provider: str = "",
        local_model: str = "",
        use_prefilter: bool = False,
        claude_batch_size: int = 20,
        slack_show_reason: bool = True,
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
        # 同姓同名の多い著者の扱い: "auto"（PubMed の論文数で判定）/ 名前のリスト / "off"
        self.author_common_names: Any = author_common_names
        self.author_senior_positions: int = int(author_senior_positions)
        self.author_common_threshold: int = int(author_common_threshold)
        self._common_name_keys: Optional[set] = None
        # 検索ごとの件数（重複除去前）。ログと検索だけの試走で使う
        self.search_counts: Dict[str, int] = {}
        self._biorxiv_records: Optional[List[Dict[str, Any]]] = None
        # Filter
        self.interactive_filtering: bool = interactive
        self.llm_filtering: bool = llm_filtering
        self.llm_provider: str = llm_provider or "openai"
        self.model: str = model or "gpt-3.5-turbo"
        self.filtering_prompt: str = filtering_prompt or ""
        self.llm_api_key: str = llm_api_key or "" # 変更
        self.local_provider: str = local_provider or ""
        self.local_model: str = local_model or ""
        self.use_prefilter: bool = use_prefilter
        self.claude_batch_size: int = claude_batch_size
        # LLM が採用理由を返した論文（小文字の DOI → 理由）。Slack 投稿に添える
        self.slack_show_reason: bool = slack_show_reason
        self.llm_reasons: Dict[str, str] = {}
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

    def _author_list(self) -> List[str]:
        """query_authors を著者名のリストにする（YAMLリスト形式とカンマ区切り文字列の両方に対応）。"""
        if not self.query_authors:
            return []
        if isinstance(self.query_authors, list):
            return [str(a).strip() for a in self.query_authors if str(a).strip()]
        return [a.strip() for a in str(self.query_authors).split(",") if a.strip()]

    def _search_by_authors(
        self, search: Callable[[List[str]], List[Dict[str, Any]]], authors: List[str]
    ) -> List[Dict[str, Any]]:
        """著者検索を実行し、同姓同名の多い名前にしか一致しない論文を責任著者側の位置で絞る。"""
        found = search(authors)
        common = self._resolve_common_name_keys(authors)
        if not common:
            return found
        wanted = {key for key in (author_match_key(a) for a in authors) if key}
        kept = filter_by_senior_position(found, wanted, common, self.author_senior_positions)
        if len(kept) < len(found):
            print(f"    同姓同名の絞り込み: {len(found)}件 → {len(kept)}件")
        return kept

    def _resolve_common_name_keys(self, authors: List[str]) -> set:
        """同姓同名が多いとみなす著者のキーを返す（実行ごとに1回だけ決める）。"""
        if self._common_name_keys is not None:
            return self._common_name_keys
        setting = self.author_common_names
        names: List[str] = []
        if self.author_senior_positions > 0:
            if isinstance(setting, list):
                names = [str(n) for n in setting]
            elif str(setting).strip().lower() == "auto":
                counts = self._pubmed_author_counts(authors)
                names = [n for n, count in counts.items() if count > self.author_common_threshold]
        keys = {key for key in (author_match_key(n) for n in names) if key}
        if keys:
            shown = sorted({n for n in authors if author_match_key(n) in keys})
            print(f"  同姓同名が多い著者（著者リストの末尾{self.author_senior_positions}人のときだけ候補）: {', '.join(shown)}")
        self._common_name_keys = keys
        return keys

    def _pubmed_author_counts(self, names: List[str]) -> Dict[str, int]:
        """各著者名の PubMed での過去1年の論文数。root_dir に保存し、AUTHOR_COUNT_TTL ごとに取り直す。

        取得に失敗した名前は含めない（同姓同名とはみなさず、絞り込みの対象外になる）。
        """
        path = os.path.join(self.root_dir, AUTHOR_COUNT_CACHE_FILE)
        cache: Dict[str, Dict[str, Any]] = {}
        try:
            with open(path, encoding="utf-8") as f:
                cache = dict(json.load(f))
        except FileNotFoundError:
            pass
        except Exception as e:
            self.logger.warning(f"著者の論文数キャッシュを読めません（取り直します）: {e}")

        stale_before = (self.today - AUTHOR_COUNT_TTL).strftime("%Y-%m-%d")
        year_ago = self.today - timedelta(days=365)
        date_range = f"{year_ago:%Y/%m/%d}:{self.today:%Y/%m/%d}[{PUBMED_DATE_FIELD}]"
        updated = False
        for name in dict.fromkeys(names):
            entry = cache.get(name)
            if entry and str(entry.get("date", "")) >= stale_before:
                continue
            params = self._pubmed_params(term=f"{name}[Author] AND {date_range}", rettype="count", retmode="json")
            try:
                resp = requests.get(f"{PUBMED_EUTILS_URL}esearch.fcgi", params=params, timeout=30)
                resp.raise_for_status()
                cache[name] = {"count": int(resp.json()["esearchresult"]["count"]), "date": self.today_str}
                updated = True
            except Exception as e:
                self.logger.warning(f"PubMed の論文数を取得できません ({name}): {e}")
            sleep(0.12 if self.ncbi_api_key else 0.4)  # NCBI の上限: キーありで毎秒10件、なしで3件

        if updated:
            try:
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(cache, f, ensure_ascii=False, indent=1, sort_keys=True)
            except OSError as e:
                self.logger.warning(f"著者の論文数キャッシュを保存できません: {e}")
        return {name: int(cache[name]["count"]) for name in names if name in cache}

    def search_papers(self) -> List[Dict[str, Any]]:
        """各データベースを検索し、DOIで重複除去した候補（DOI の URL 付き）を返す。

        PubMed は E-utilities、arXiv は arXiv API、bioRxiv は bioRxiv API を直接使う。以前使っていた
        findpapers は、bioRxiv では検索画面（medrxiv.org/search）が Cloudflare のボット判定で 403 になり、
        arXiv では API の応答形式の変更で件数を読めずに例外を握りつぶし、どちらも常に0件だった。
        検索ごとの件数は self.search_counts に残す。
        """
        query_pub_arx = self.query or self.query_pub_arx
        query_biorxiv = self.query or self.query_biorxiv
        authors = self._author_list()

        searches: List[Tuple[str, Callable[[], List[Dict[str, Any]]]]] = []
        if query_pub_arx and "pubmed" in self.databases:
            searches.append(("PubMedキーワード", partial(self.search_pubmed, query_pub_arx)))
        if query_pub_arx and "arxiv" in self.databases:
            searches.append(("arXivキーワード", partial(self.search_arxiv, query_pub_arx)))
        if query_biorxiv and "biorxiv" in self.databases:
            searches.append(("bioRxivキーワード", partial(self.search_biorxiv, query_biorxiv)))
        if authors and "pubmed" in self.databases:
            searches.append((f"PubMed著者（{len(authors)}名）", partial(self._search_by_authors, self.search_pubmed_by_authors, authors)))
        if authors and "arxiv" in self.databases:
            searches.append((f"arXiv著者（{len(authors)}名）", partial(self._search_by_authors, self.search_arxiv_by_authors, authors)))
        if authors and "biorxiv" in self.databases:
            searches.append((f"bioRxiv著者（{len(authors)}名）", partial(self._search_by_authors, self.search_biorxiv_by_authors, authors)))

        articles: List[Dict[str, Any]] = []
        for label, run_search in searches:
            print(f"  {label}検索中...")
            try:
                found = run_search()
            except ValueError as e:  # クエリの書式誤り。他の検索は続ける
                self.logger.error(f"{label}検索のクエリを解釈できません: {e}")
                found = []
            except Exception as e:
                self.logger.error(f"{label}検索に失敗しました: {e}")
                found = []
            self.search_counts[label] = len(found)
            articles.extend(found)
            print(f"    → {len(found)}件")

        before = len(articles)
        articles = self._deduplicate_by_doi(articles)
        print(f"  重複除去: {before}件 → {len(articles)}件")
        return [article for article in articles if article.get("url")]

    def find_and_process_papers(self) -> pd.DataFrame:
        """
        Executes the search for papers based on predefined criteria and processes them.

        Returns:
            pd.DataFrame: A DataFrame containing processed articles.
        """

        print("Searching papers...")
        articles = self.search_papers()

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
            local_provider=self.local_provider,
            local_model=self.local_model,
        )
        processed_articles = processor.articles
        self.logger.info(f"Found {len(processed_articles)} articles.")

        if self.llm_filtering:
            print("Filtering papers with LLM...")
            processed_articles = self._apply_llm_filter(processed_articles)
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

    def _pubmed_params(self, **params: Any) -> Dict[str, Any]:
        params["db"] = "pubmed"
        if self.ncbi_api_key:
            params["api_key"] = self.ncbi_api_key
        return params

    def _pubmed_esearch(self, term: str) -> List[str]:
        """期間内（収載日）でアブストラクトのある論文の PMID を返す。失敗したら例外。

        PubMed に収載されたプレプリント（preprint[pt]）は除く。PubMed 由来の記事は出版済み論文として
        扱われるうえ、bioRxiv 分は bioRxiv API から取得しているため（findpapers 時代の挙動に合わせる）。
        """
        date_range = f"{self.since:%Y/%m/%d}:{self.until:%Y/%m/%d}[{PUBMED_DATE_FIELD}]"
        params = self._pubmed_params(
            term=f"({term}) AND ({date_range}) AND has abstract [FILT] NOT preprint[pt]",
            retmax=self.limit_per_database,
            retmode="json",
        )
        resp = requests.get(f"{PUBMED_EUTILS_URL}esearch.fcgi", params=params, timeout=30)
        resp.raise_for_status()
        result = resp.json()["esearchresult"]
        if "ERROR" in result:
            raise ValueError(result["ERROR"])
        count = int(result.get("count", 0))
        if count > self.limit_per_database:
            self.logger.warning(f"PubMed: {count}件中 {self.limit_per_database}件だけ取得します")
        return list(result.get("idlist", []))

    def search_pubmed(self, query: str) -> List[Dict[str, Any]]:
        """PubMed をキーワードで検索する。各語はタイトル・アブストラクト（[TIAB]）のフレーズ一致。"""
        pmids = self._pubmed_esearch(to_pubmed(parse_query(query)))
        sleep(0.5)
        return self._pubmed_fetch_articles(pmids)

    def search_pubmed_by_authors(self, authors: List[str]) -> List[Dict[str, Any]]:
        """PubMedを著者名フィールドで直接検索する（[Author]フィールド使用）。

        著者を全員1本のクエリにORで連結するとGET URLが長くなりHTTP 414になるため、
        AUTHOR_QUERY_BATCH_SIZE 名ずつに分割してesearchし、得られたPMIDを統合する。
        """
        pmids: List[str] = []
        seen_pmids: set = set()
        for start in range(0, len(authors), AUTHOR_QUERY_BATCH_SIZE):
            author_batch = authors[start : start + AUTHOR_QUERY_BATCH_SIZE]
            author_query = " OR ".join([f'{a}[Author]' for a in author_batch])
            try:
                batch_pmids = self._pubmed_esearch(author_query)
            except Exception as e:
                batch_no = start // AUTHOR_QUERY_BATCH_SIZE + 1
                self.logger.error(f"PubMed esearch失敗 (著者バッチ {batch_no}): {e}")
                continue
            for pmid in batch_pmids:
                if pmid not in seen_pmids:
                    seen_pmids.add(pmid)
                    pmids.append(pmid)
            sleep(0.5)

        return self._pubmed_fetch_articles(pmids)

    def _pubmed_fetch_articles(self, pmids: List[str]) -> List[Dict[str, Any]]:
        """PMID の論文を efetch で取得して記事 dict にする。DOI・アブストラクト・日付の無いものは除く。"""
        result: List[Dict[str, Any]] = []

        for i in range(0, len(pmids), 100):
            params = self._pubmed_params(id=",".join(pmids[i : i + 100]), retmode="xml")
            try:
                fetch_resp = requests.get(f"{PUBMED_EUTILS_URL}efetch.fcgi", params=params, timeout=60)
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

    def search_arxiv(self, query: str) -> List[Dict[str, Any]]:
        """arXiv をキーワードで検索する。各語はタイトルかアブストラクトでのフレーズ一致。"""
        return self._arxiv_search(to_arxiv(parse_query(query)))

    def search_arxiv_by_authors(self, authors: List[str]) -> List[Dict[str, Any]]:
        """arXiv APIを著者フィールドで直接検索する（au:フィールド使用）。

        著者を全員1本のクエリにORで連結するとGET URLが長くなりHTTP 400になるため、
        AUTHOR_QUERY_BATCH_SIZE 名ずつに分割して検索し、DOIで重複除去して統合する。
        """
        result: List[Dict[str, Any]] = []
        seen_dois: set = set()
        for start in range(0, len(authors), AUTHOR_QUERY_BATCH_SIZE):
            author_batch = authors[start : start + AUTHOR_QUERY_BATCH_SIZE]
            author_query = " OR ".join([f'au:"{a}"' for a in author_batch])
            for article in self._arxiv_search(author_query):
                if article["doi"] not in seen_dois:
                    seen_dois.add(article["doi"])
                    result.append(article)
        return result

    def _arxiv_search(self, search_query: str) -> List[Dict[str, Any]]:
        """arXiv API を投稿日で期間を絞って検索し、全ページを記事 dict にして返す。

        期間の始まりは ARXIV_LOOKBACK_DAYS 日前に広げる（公開の遅れで取りこぼさないため）。
        """
        since = self.since - timedelta(days=ARXIV_LOOKBACK_DAYS)
        date_filter = f"submittedDate:[{since:%Y%m%d}0000 TO {self.until:%Y%m%d}2359]"
        params: Dict[str, Any] = {
            "search_query": f"({search_query}) AND {date_filter}",
            "sortBy": "submittedDate",
            "sortOrder": "descending",
            "max_results": ARXIV_PAGE_SIZE,
        }
        result: List[Dict[str, Any]] = []
        seen_dois: set = set()
        start, total = 0, None
        while (total is None or start < total) and start < self.limit_per_database:
            root = self._fetch_arxiv_page({**params, "start": start})
            if root is None:
                self.logger.error(f"arXiv API の取得を中断しました (start={start})。取得済みの範囲で続行します")
                break
            if total is None:
                total = int(root.findtext("opensearch:totalResults", default="0", namespaces=ARXIV_NS) or 0)
                if total > self.limit_per_database:
                    self.logger.warning(f"arXiv: {total}件中 {self.limit_per_database}件だけ取得します")
            entries = root.findall("atom:entry", ARXIV_NS)
            if not entries:
                break
            start += len(entries)
            for entry in entries:
                article = _arxiv_entry_to_article(entry, since, self.until)
                if article is not None and article["doi"] not in seen_dois:
                    seen_dois.add(article["doi"])
                    result.append(article)
        return result

    def _fetch_arxiv_page(self, params: Dict[str, Any]) -> Optional[Any]:
        """arXiv API の1ページを取得して XML のルートを返す（混雑・制限時は待って再試行）。取得できなければ None。"""
        attempts = len(ARXIV_RETRY_WAITS) + 1
        for attempt in range(attempts):
            try:
                resp = requests.get(ARXIV_API_URL, params=params, timeout=60)
                resp.raise_for_status()
                root = ET.fromstring(resp.content)
            except Exception as e:
                self.logger.warning(f"arXiv API 取得失敗 (試行 {attempt + 1}/{attempts}): {e}")
                if attempt < attempts - 1:
                    sleep(_arxiv_retry_wait(e, attempt))
                continue
            sleep(ARXIV_REQUEST_INTERVAL)
            # クエリの書式誤りはエラー内容を1件の entry として返す（再試行しても同じ）
            for entry in root.findall("atom:entry", ARXIV_NS):
                if "/api/errors" in (entry.findtext("atom:id", default="", namespaces=ARXIV_NS) or ""):
                    summary = entry.findtext("atom:summary", default="", namespaces=ARXIV_NS)
                    self.logger.error(f"arXiv API がクエリを受け付けませんでした: {summary}")
                    return None
            return root
        return None

    def search_biorxiv(self, query: str) -> List[Dict[str, Any]]:
        """bioRxiv をキーワードで検索する。

        期間内の新着プレプリント（version 1）を bioRxiv API で全件取得し、タイトルとアブストラクトに
        クエリをローカルで照合する（語の境界で一致、ハイフンは空白扱い、最後の語の複数形も許す）。
        bioRxiv の検索画面（findpapers が使っていた medrxiv.org/search）はボット判定で取得できない。
        """
        matches = compile_matcher(parse_query(query))
        result: List[Dict[str, Any]] = []
        for item in self._biorxiv_window():
            if matches(normalize_text(f"{item.get('title') or ''} {item.get('abstract') or ''}")):
                article = _biorxiv_item_to_article(item)
                if article is not None:
                    result.append(article)
        return result

    def search_biorxiv_by_authors(self, authors: List[str]) -> List[Dict[str, Any]]:
        """bioRxiv を著者名で検索する。

        キーワード検索はタイトル・アブストラクトだけを対象にするため、query_biorxiv に
        著者名を書いても著者としては一致しない。代わりに bioRxiv API で期間内の新着プレプリント
        （version 1）を全件取得し、著者名（姓＋名の頭文字）でローカルに照合する。
        """
        wanted = {key for key in (author_match_key(a) for a in authors) if key}
        if not wanted:
            return []

        result: List[Dict[str, Any]] = []
        for item in self._biorxiv_window():
            if any(author_match_key(n) in wanted for n in _biorxiv_author_names(item)):
                article = _biorxiv_item_to_article(item)
                if article is not None:
                    result.append(article)
        return result

    def _biorxiv_window(self) -> List[Dict[str, Any]]:
        """期間内に bioRxiv に新規投稿された（version 1 の）レコードを返す。

        キーワード検索と著者検索で共有し、取得し終えた結果は root_dir に保存して他トピックとも共有する。
        """
        if self._biorxiv_records is None:
            records = self._load_biorxiv_cache()
            if records is None:
                records, complete = self._fetch_biorxiv_window()
                if not complete:
                    return records  # 途中で失敗した結果は覚えず、同じ実行の次の検索で取り直す
                self._save_biorxiv_cache(records)
            self._biorxiv_records = records
        return self._biorxiv_records

    def _fetch_biorxiv_window(self) -> Tuple[List[Dict[str, Any]], bool]:
        """bioRxiv API を全ページ取得し、(version 1 のレコード, 最後まで取得できたか) を返す。"""
        since, until = self.since.strftime("%Y-%m-%d"), self.until.strftime("%Y-%m-%d")
        records: List[Dict[str, Any]] = []
        seen_dois: set = set()
        cursor, total = 0, None
        while total is None or cursor < total:
            data = self._fetch_biorxiv_page(f"{BIORXIV_API_URL}/{since}/{until}/{cursor}/json")
            if data is None:
                self.logger.error(f"bioRxiv API の取得を中断しました (cursor={cursor})。取得済みの範囲で続行します")
                return records, False

            collection = data.get("collection") or []
            try:
                total = int((data.get("messages") or [{}])[0].get("total", 0))
            except (TypeError, ValueError):
                total = 0
            if not collection:
                break
            cursor += len(collection)

            for item in collection:
                doi = str(item.get("doi") or "").strip()
                if str(item.get("version", "1")) == "1" and doi and doi not in seen_dois:
                    seen_dois.add(doi)
                    records.append({key: item.get(key) for key in BIORXIV_CACHE_FIELDS})
            sleep(0.2)

        return records, True

    def _biorxiv_cache_path(self) -> str:
        return os.path.join(self.root_dir, BIORXIV_CACHE_FILE)

    def _load_biorxiv_cache(self) -> Optional[List[Dict[str, Any]]]:
        """同じ期間で BIORXIV_CACHE_TTL 以内に保存した bioRxiv 新着があれば返す。"""
        path = self._biorxiv_cache_path()
        if not os.path.exists(path):
            return None
        try:
            with open(path, encoding="utf-8") as f:
                cache = json.load(f)
            fetched_at = datetime.fromisoformat(cache["fetched_at"])
            same_window = (cache["since"], cache["until"]) == (f"{self.since:%Y-%m-%d}", f"{self.until:%Y-%m-%d}")
            if not same_window or datetime.now() - fetched_at > BIORXIV_CACHE_TTL:
                return None
            records = list(cache["records"])
        except Exception as e:
            self.logger.warning(f"bioRxiv のキャッシュを読めませんでした（取得し直します）: {e}")
            return None
        print(f"  bioRxiv 新着はキャッシュを使用（{fetched_at:%H:%M} 取得, {len(records)}件）")
        return records

    def _save_biorxiv_cache(self, records: List[Dict[str, Any]]) -> None:
        cache = {
            "since": f"{self.since:%Y-%m-%d}",
            "until": f"{self.until:%Y-%m-%d}",
            "fetched_at": datetime.now().isoformat(timespec="seconds"),
            "records": records,
        }
        path = self._biorxiv_cache_path()
        try:
            with open(f"{path}.tmp", "w", encoding="utf-8") as f:
                json.dump(cache, f, ensure_ascii=False)
            os.replace(f"{path}.tmp", path)
        except Exception as e:  # キャッシュは無くても動く
            self.logger.warning(f"bioRxiv のキャッシュを保存できませんでした: {e}")

    def _fetch_biorxiv_page(self, url: str) -> Optional[Dict[str, Any]]:
        """bioRxiv API の1ページを取得する（3回まで再試行）。取得できなければ None。"""
        for attempt in range(3):
            try:
                resp = requests.get(url, timeout=60)
                resp.raise_for_status()
                return dict(resp.json())
            except Exception as e:
                self.logger.warning(f"bioRxiv API 取得失敗 ({url}, 試行 {attempt + 1}/3): {e}")
                sleep(2**attempt)
        return None

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
        return self._write_csv_atomic(df, history_file_path)

    def _write_csv_atomic(self, df: pd.DataFrame, path: str) -> bool:
        """CSV を QUOTE_ALL で全体書き直し（一時ファイルからの原子的置換）。"""
        tmp_path = f"{path}.tmp"
        for attempt in range(3):
            try:
                df.to_csv(
                    tmp_path, index=False, header=True,
                    encoding="utf-8-sig", quoting=csv.QUOTE_ALL,
                )
                os.replace(tmp_path, path)
                return True
            except OSError as e:
                if e.errno == 11 and attempt < 2:  # EDEADLK on macOS
                    sleep(2 ** attempt)
                else:
                    self.logger.error(f"CSVファイルへの書き込みに失敗しました ({path}): {e}")
                    return False
            except Exception as e:
                self.logger.error(f"CSVファイルへの書き込みに失敗しました ({path}): {e}")
                return False
        return False

    def _decisions_path(self) -> str:
        """LLM判定ログのパス（履歴ファイルの隣に {履歴ファイル名}_decisions.csv）。"""
        history_path = self._history_path()
        stem = os.path.splitext(os.path.basename(history_path))[0]
        return os.path.join(os.path.dirname(history_path), f"{stem}_decisions.csv")

    def _prompt_key(self) -> str:
        """判定基準とモデル構成の指紋。これが変わったら過去の判定は再利用しない。

        ローカルLLMは一次ふるいに使うときだけ含める（代行判定はキャッシュしないので無関係）。
        """
        prefilter = [self.local_provider, self.local_model] if self.use_prefilter else []
        raw = "\n".join([self.filtering_prompt, self.llm_provider, self.model, *prefilter])
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]

    def _load_decisions_df(self) -> pd.DataFrame:
        path = self._decisions_path()
        if not os.path.exists(path):
            return pd.DataFrame(columns=DECISION_COLUMNS)
        for attempt in range(3):
            try:
                df = pd.read_csv(path, dtype=str, keep_default_na=False, encoding="utf-8-sig")
                return df.reindex(columns=DECISION_COLUMNS, fill_value="")
            except OSError as e:
                if e.errno == 11 and attempt < 2:  # EDEADLK on macOS
                    sleep(2 ** attempt)
                else:
                    break
            except Exception as e:
                self.logger.warning(f"LLM判定ログを読み込めませんでした（キャッシュなしで続行）: {e}")
                break
        return pd.DataFrame(columns=DECISION_COLUMNS)

    def _apply_llm_filter(self, articles: pd.DataFrame) -> pd.DataFrame:
        """LLMで論文を選別し、判定を {履歴ファイル名}_decisions.csv に記録する。

        同じ判定基準（_prompt_key）で過去に不採用となった論文は再判定しない。--since の期間が
        重なるため、キャッシュなしでは同じ論文を数日続けて判定することになる。
        採用された論文は history.csv に入り、そもそもここまで来ない。
        """
        if articles.empty:
            return articles

        prompt_key = self._prompt_key()
        log_df = self._load_decisions_df()
        cached = log_df[
            (log_df["PromptKey"] == prompt_key)
            & (log_df["Relevant"] == "FALSE")
            & (log_df["Stage"].isin(CACHEABLE_STAGES))
        ]
        rejected_dois = set(cached["DOI"].str.strip().str.lower())
        norm_doi = articles["DOI"].astype(str).str.strip().str.lower()
        is_cached = norm_doi.isin(rejected_dois)
        if is_cached.any():
            print(f"  判定キャッシュ: {int(is_cached.sum())}件は判定済み（不採用）のためスキップ")
        to_judge = articles[~is_cached]
        if to_judge.empty:
            return to_judge

        llm_filter = LLMFilter(
            to_judge,
            llm_provider=self.llm_provider,
            model=self.model,
            filtering_prompt=self.filtering_prompt,
            llm_api_key=self.llm_api_key,
            local_provider=self.local_provider,
            local_model=self.local_model,
            use_prefilter=self.use_prefilter,
            claude_batch_size=self.claude_batch_size,
            with_reason=self.slack_show_reason,
        )
        kept = llm_filter.filter_articles()
        for index, (relevant, _, reason) in llm_filter.decisions.items():
            if relevant and reason:
                self.llm_reasons[str(to_judge.at[index, "DOI"]).strip().lower()] = reason

        new_rows = [
            {
                "DOI": str(to_judge.at[index, "DOI"]).strip(),
                "Date": self.today_str,
                "PromptKey": prompt_key,
                "Stage": stage,
                "Relevant": "TRUE" if relevant else "FALSE",
                "Reason": reason,
                "Title": str(to_judge.at[index, "Title"]),
            }
            for index, (relevant, stage, reason) in llm_filter.decisions.items()
            if stage != "unjudged"
        ]
        stages = Counter(stage for _, stage, _ in llm_filter.decisions.values())
        print("  判定内訳: " + ", ".join(f"{stage}={n}" for stage, n in sorted(stages.items())))
        usage = claude_usage_summary()
        if usage:
            print(f"  {usage}")

        if new_rows:
            cutoff = (self.today - timedelta(days=DECISION_RETENTION_DAYS)).strftime("%Y-%m-%d")
            combined = pd.concat([log_df[log_df["Date"] >= cutoff], pd.DataFrame(new_rows)], ignore_index=True)
            self._write_csv_atomic(combined.reindex(columns=DECISION_COLUMNS, fill_value=""), self._decisions_path())

        return kept

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

        プレプリントは CrossRef 上の参照数が0のことが多いので対象外にする。取得元DBで判定し、
        DOI 接頭辞でも補う（bioRxiv は 2026年から 10.64898/ に移行し、旧 10.1101/ と併存している）。
        """
        PREPRINT_PREFIXES = ("10.1101/", "10.64898/", "10.48550/")
        PREPRINT_DATABASES = {"biorxiv", "medrxiv", "arxiv"}
        filtered = []
        headers = {"User-Agent": "PaperBee/1.0 (mailto:paperbee@example.com)"}
        skipped = 0

        for article in articles:
            doi = article.get("doi", "")
            from_preprint_server = any(
                str(db).lower() in PREPRINT_DATABASES for db in article.get("databases") or []
            )
            if not doi or from_preprint_server or doi.startswith(PREPRINT_PREFIXES):
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
        response, _ = self._post_to_slack_in_chunks(papers)
        return response

    def _post_to_slack_in_chunks(self, papers: List[List[Any]]) -> Tuple[Any, List[List[Any]]]:
        """SLACK_PAPERS_PER_MESSAGE 件ずつのメッセージに分けて Slack に投稿する。

        途中のメッセージで失敗したらそこで止める。返り値は (最後に成功した応答, 投稿できた論文の行)。
        """
        self.slack_publisher: SlackPaperPublisher = SlackPaperPublisher(
            WebClient(self.slack_bot_token),
            Logger("SlackPaperPublisher"),
            channel_id=self.slack_channel_id,
        )
        reasons = self.llm_reasons if self.slack_show_reason else None
        chunks = [papers[i : i + SLACK_PAPERS_PER_MESSAGE] for i in range(0, len(papers), SLACK_PAPERS_PER_MESSAGE)]
        chunks = chunks or [[]]  # 0件の日も「No papers found today.」を投稿する
        response: Any = None
        posted: List[List[Any]] = []
        for number, chunk in enumerate(chunks, start=1):
            papers_pub, preprints = self.slack_publisher.format_papers_for_slack(chunk, reasons=reasons)
            part = (number, len(chunks)) if len(chunks) > 1 else None
            result = self.slack_publisher.publish_papers_to_slack(
                papers_pub, preprints, self.today_str, self.spreadsheet_id, part=part
            )
            if result is None:
                self.logger.error(f"Slack への投稿に失敗しました（{number}/{len(chunks)}通目）。残りは送りません")
                break
            response = result
            posted.extend(chunk)
        return response, posted

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
        papers = self.select_new_papers(processed_articles)
        # 履歴には投稿できた論文だけを記録する。失敗した分は次回の実行で再び候補になる
        posted = papers

        response_slack = None
        response_telegram = None
        response_zulip = None
        response_mattermost = None

        if post_to_slack:
            print("Posting to Slack...")
            response_slack, posted = self._post_to_slack_in_chunks(papers)
            if len(posted) < len(papers):
                print(f"  Slack に投稿できなかった {len(papers) - len(posted)}件は履歴に記録しません（次回の実行で再投稿します）")

        if post_to_telegram:
            print("Posting to Telegram...")
            response_telegram = await self.post_paper_to_telegram(papers)

        if post_to_zulip:
            print("Posting to Zulip...")
            response_zulip = await self.post_paper_to_zulip(papers)

        if post_to_mattermost:
            print("Posting to Mattermost...")
            response_mattermost = await self.post_paper_to_mattermost(papers)

        self.record_history(posted)
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
        """履歴に無い論文を選んで、そのまま履歴に記録する（投稿の成否を待たない従来の動作）。"""
        papers = self.select_new_papers(processed_articles)
        self.record_history(papers)
        return papers

    def _load_published_dois(self) -> Tuple[Optional[pd.DataFrame], set]:
        history_df = self._load_history_df(self._history_path())
        published_dois: set = set()
        if history_df is not None and "DOI" in history_df.columns:
            published_dois = set(history_df["DOI"].dropna().astype(str).str.strip().str.lower())
        return history_df, published_dois

    def select_new_papers(self, processed_articles: pd.DataFrame) -> List[List[Any]]:
        """履歴に無い論文を HISTORY_COLUMNS の列順の行にして返す（履歴にはまだ書かない）。"""
        _, published_dois = self._load_published_dois()
        if not processed_articles.empty and "DOI" in processed_articles.columns:
            norm_doi = processed_articles["DOI"].astype(str).str.strip().str.lower()
            new_articles = processed_articles[~norm_doi.isin(published_dois)]
        else:
            new_articles = processed_articles

        if new_articles.empty:
            self.logger.info("新しい論文は見つかりませんでした（すべて履歴済み）。")
            return []
        return list(new_articles.reindex(columns=HISTORY_COLUMNS, fill_value="").values.tolist())

    def record_history(self, papers: List[List[Any]]) -> None:
        """投稿した論文（HISTORY_COLUMNS の列順の行）を履歴に追記する。

        既存＋新規を正準スキーマで結合してファイル全体を書き直す（追記による列ドリフトを根絶）。
        """
        if not papers:
            return
        history_df, _ = self._load_published_dois()
        new_for_history = pd.DataFrame(papers, columns=HISTORY_COLUMNS)
        if history_df is not None and not history_df.empty:
            combined = pd.concat([history_df, new_for_history], ignore_index=True)
        else:
            combined = new_for_history
        combined = combined.drop_duplicates(subset="DOI", keep="first")

        history_file_path = self._history_path()
        if self._write_history_df(combined, history_file_path):
            self.logger.info(f"{len(papers)} 件の新しい論文を {history_file_path} に保存しました。")
