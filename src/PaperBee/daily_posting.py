import argparse
import asyncio
import os
import time
from typing import Any, List, Optional, Tuple, cast

import yaml

from PaperBee.papers import (
    PapersFinder,
    claude_client,
    validate_configuration,
    validate_llm_args,
    validate_platform_args,
)
from PaperBee.papers.papers_finder import DEFAULT_COMMON_NAME_THRESHOLD

DEFAULT_CLAUDE_MONTHLY_BUDGET_USD = 100.0
CLAUDE_USAGE_FILE = "claude_usage.csv"


def resolve_claude_api_key(config: dict, stage_key_name: str) -> str:
    """Claude 用の API キーを決める。

    設定ファイルの ANTHROPIC_API_KEY を優先し、無ければ工程別のキー（LLM_API_KEY 等）が Anthropic の
    キー（sk-ant-）である場合だけ使う。どちらも無ければ空文字を返し、SDK の既定の解決
    （ANTHROPIC_API_KEY 環境変数 → `ant auth login` のプロファイル）に任せる。
    工程別のキー欄には Gemini/OpenAI のキーが残っていることが多いため、無条件には流用しない。
    """
    key = str(config.get("ANTHROPIC_API_KEY") or "").strip()
    if key:
        return key
    stage_key = str(config.get(stage_key_name) or "").strip()
    return stage_key if stage_key.startswith("sk-ant-") else ""


def load_config(config_path: str) -> dict[Any, Any]:
    # OneDrive sync can cause EDEADLK (errno 11) on macOS; retry with backoff
    for attempt in range(3):
        try:
            with open(config_path) as f:
                return cast(dict[Any, Any], yaml.safe_load(f))
        except OSError as e:
            if e.errno == 11 and attempt < 2:
                time.sleep(2 ** attempt)
            else:
                raise
    raise OSError(11, "設定ファイルの読み込みに複数回失敗しました", config_path)


async def daily_papers_search(
    config: dict,
    interactive: bool = False,
    since: Optional[int] = None,
    databases: Optional[List[str]] = None,
) -> Tuple[List[List[Any]], Any, Any, Any, Any]:
    """
    Searches for daily papers and posts them to Telegram.

    Returns:
        Tuple[List[List[Any]], Any, Any, Any, Any]:
            - List of papers (list of lists of Any)
            - Slack response
            - Telegram response
            - Zulip response
            - Mattermost response
    """
    root_dir, query, query_biorxiv, query_pubmed_arxiv, query_authors = validate_configuration(config)

    slack_args = validate_platform_args(config, "SLACK")
    zulip_args = validate_platform_args(config, "ZULIP")
    telegram_args = validate_platform_args(config, "TELEGRAM")
    mattermost_args = validate_platform_args(config, "MATTERMOST")

    if telegram_args == {}:
        telegram_args = {"bot_token": "", "channel_id": "", "is_posting_on": False}
    if zulip_args == {}:
        zulip_args = {"prc": "", "stream": "", "topic": "", "is_posting_on": False}
    if slack_args == {}:
        slack_args = {"bot_token": "", "channel_id": "", "is_posting_on": False}
    if mattermost_args == {}:
        mattermost_args = {"url": "", "token": "", "team": "", "channel": "", "is_posting_on": False}

    llm_filtering = config.get("LLM_FILTERING", False)
    if llm_filtering:
        # OPENAI_API_KEY -> llm_api_key に変更
        filtering_prompt, LLM_PROVIDER, LANGUAGE_MODEL, llm_api_key = validate_llm_args(config, root_dir)
        if claude_client.is_claude_provider(LLM_PROVIDER):
            llm_api_key = resolve_claude_api_key(config, "LLM_API_KEY")
    else:
        filtering_prompt = ""
        LLM_PROVIDER = ""
        LANGUAGE_MODEL = ""
        llm_api_key = ""

    # ローカルLLMのサブエージェント。Claude が使えないときの代行判定と、任意で一次ふるいを担う
    local_provider = str(config.get("LOCAL_LLM_PROVIDER") or "").lower()
    local_model = str(config.get("LOCAL_LLM_MODEL") or "")
    use_prefilter = bool(config.get("LOCAL_PREFILTER", False))

    # 著者検索で同姓同名の多い名前の扱い（"auto" / 名前のリスト / "off"）。未設定なら従来どおり絞らない
    author_common_names = config.get("AUTHOR_COMMON_NAMES", "off") or "off"
    author_senior_positions = int(config.get("AUTHOR_SENIOR_POSITIONS", 2))
    author_common_threshold = int(config.get("AUTHOR_COMMON_NAME_THRESHOLD", DEFAULT_COMMON_NAME_THRESHOLD))
    claude_batch_size = int(config.get("CLAUDE_BATCH_SIZE") or 20)
    # LLM の採用理由を Slack 投稿に添えるか（理由を返すのは Claude の判定のみ）
    slack_show_reason = bool(config.get("SLACK_SHOW_REASON", True))

    history_file = str(config.get("HISTORY_FILE", "history.csv"))
    # Claude の利用量台帳は LOCAL_ROOT_DIR 直下で全トピック共有（月間予算は合算で管理）
    claude_client.configure(
        ledger_path=os.path.join(root_dir, CLAUDE_USAGE_FILE),
        monthly_budget_usd=float(config.get("CLAUDE_MONTHLY_BUDGET_USD", DEFAULT_CLAUDE_MONTHLY_BUDGET_USD) or 0),
        tag=os.path.splitext(os.path.basename(history_file))[0],
        effort=str(config.get("CLAUDE_EFFORT", claude_client.DEFAULT_EFFORT) or ""),
    )

    translation_enabled = config.get("TRANSLATION_ENABLED", False)
    translation_provider = str(config.get("TRANSLATION_PROVIDER", "ollama")).lower()
    translation_model = str(config.get("TRANSLATION_MODEL", "gemma2"))
    translation_api_key = str(config.get("TRANSLATION_API_KEY", ""))
    if claude_client.is_claude_provider(translation_provider):
        translation_api_key = resolve_claude_api_key(config, "TRANSLATION_API_KEY")

    default_prompt = "Translate the following scientific abstract into natural Japanese. Output ONLY the Japanese translation:\n\n{text}"
    translation_prompt = str(config.get("TRANSLATION_PROMPT", default_prompt))

    summarization_enabled = config.get("SUMMARIZATION_ENABLED", False)
    summarization_provider = str(config.get("SUMMARIZATION_PROVIDER", "ollama")).lower()
    summarization_model = str(config.get("SUMMARIZATION_MODEL", "gemma2"))
    summarization_api_key = str(config.get("SUMMARIZATION_API_KEY", "")) # 必要なら
    if claude_client.is_claude_provider(summarization_provider):
        summarization_api_key = resolve_claude_api_key(config, "SUMMARIZATION_API_KEY")
    summarization_prompt = str(config.get("SUMMARIZATION_PROMPT", "以下のテキストを日本語で3点の箇条書きに要約してください。:\n\n{text}"))

    filter_unedited = bool(config.get("FILTER_UNEDITED", False))

    finder = PapersFinder(
        root_dir=root_dir,
        history_file = history_file,
        spreadsheet_id=config.get("GOOGLE_SPREADSHEET_ID", ""),
        google_credentials_json=config.get("GOOGLE_CREDENTIALS_JSON", ""),
        sheet_name="Papers",
        since=since,
        query=query,
        query_biorxiv=query_biorxiv,
        query_pubmed_arxiv=query_pubmed_arxiv,
        query_authors=query_authors,
        author_common_names=author_common_names,
        author_senior_positions=author_senior_positions,
        author_common_threshold=author_common_threshold,
        interactive=interactive,
        llm_filtering=llm_filtering,
        filtering_prompt=filtering_prompt,
        llm_provider=LLM_PROVIDER,
        model=LANGUAGE_MODEL,
        llm_api_key=llm_api_key, # Renamed
        local_provider=local_provider,
        local_model=local_model,
        use_prefilter=use_prefilter,
        claude_batch_size=claude_batch_size,
        slack_show_reason=slack_show_reason,
        filter_unedited=filter_unedited,
        ncbi_api_key=str(config.get("NCBI_API_KEY", "")),
        slack_bot_token=slack_args["bot_token"],
        slack_channel_id=slack_args["channel_id"],
        telegram_bot_token=telegram_args["bot_token"],
        telegram_channel_id=telegram_args["channel_id"],
        zulip_prc=zulip_args["prc"],
        zulip_stream=zulip_args["stream"],
        zulip_topic=zulip_args["topic"],
        mattermost_url=mattermost_args["url"],
        mattermost_token=mattermost_args["token"],
        mattermost_team=mattermost_args["team"],
        mattermost_channel=mattermost_args["channel"],
        databases=databases,
        translation_enabled=translation_enabled,
        translation_provider=translation_provider,
        translation_model=translation_model,
        translation_api_key=translation_api_key,
        translation_prompt=translation_prompt,
        summarization_enabled=summarization_enabled,
        summarization_provider=summarization_provider,
        summarization_model=summarization_model,
        summarization_api_key=summarization_api_key,
        summarization_prompt=summarization_prompt,
    )
    papers, response_slack, response_telegram, response_zulip, response_mattermost = await finder.run_daily(
        post_to_slack=slack_args["is_posting_on"],
        post_to_telegram=telegram_args["is_posting_on"],
        post_to_zulip=zulip_args["is_posting_on"],
        post_to_mattermost=mattermost_args["is_posting_on"],
    )

    usage = claude_client.usage_summary()
    if usage:
        print(usage)

    return papers, response_slack, response_telegram, response_zulip, response_mattermost


def main() -> None:
    """
    CLI entry point for PaperBee, supporting subcommands like 'post'.
    """
    parser = argparse.ArgumentParser(description="PaperBee CLI")
    subparsers = parser.add_subparsers(dest="command", required=True, help="Available commands")

    # Subcommand: post
    post_parser = subparsers.add_parser("post", help="Post daily papers")
    post_parser.add_argument(
        "--config",
        type=str,
        required=True,
        help="Path to YAML configuration file.",
    )
    post_parser.add_argument(
        "--interactive",
        action="store_true",
        help="Activate interactive filtering",
    )
    post_parser.add_argument(
        "--since",
        type=int,
        help="Filter out papers if published before the specified number of days ago.",
    )
    post_parser.add_argument(
        "--databases",
        nargs="+",
        type=str,
        help="Specify any combination of databases to search among the available ones 'pubmed','arxiv', and 'biorxiv'(e.g., ['pubmed', 'arxiv']).",
    )
    args = parser.parse_args()

    # Dispatch to the appropriate subcommand
    if args.command == "post":
        config = load_config(args.config)
        papers, _, _, _, _ = asyncio.run(
            daily_papers_search(
                config,
                interactive=args.interactive,
                since=args.since,
                databases=args.databases,
            )
        )
        print("Papers found:")
        print(papers)