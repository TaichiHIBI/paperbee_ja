import os
import re
from logging import Logger
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
from slack_sdk import WebClient
from slack_sdk.web.slack_response import SlackResponse


class SlackPaperPublisher:
    """
    A class to manage publishing academic papers to a Slack channel.

    Args:
        client (WebClient): A Slack WebClient instance for interacting with Slack's API.
        logger (Logger): A logger instance for logging information and errors.
        channel_id (Optional[str]): The ID of the Slack channel where papers will be published.
    """

    def __init__(self, client: WebClient, logger: Logger, channel_id: Optional[str] = None) -> None:
        """
        Initializes the SlackPaperPublisher with the Slack client, logger, and optional channel ID.

        Args:
            client (WebClient): The Slack WebClient instance.
            logger (Logger): The logger instance for logging messages.
            channel_id (Optional[str]): The ID of the Slack channel. Defaults to None.
        """
        self.client = client
        self.logger = logger
        self.channel_id = channel_id

    @staticmethod
    def format_papers_for_slack(
        papers_list: List[List[str]],
        reasons: Optional[Dict[str, str]] = None,
    ) -> Tuple[List[str], List[str]]:
        """
        Formats a list of papers into separate lists for regular papers and preprints with Slack message formatting.

        Args:
            papers_list (List[List[str]]): A list of papers, where each paper is a list of Args.
            reasons (Optional[Dict[str, str]]): 小文字の DOI → LLM の採用理由。あれば誌名の下に添える。

        Returns:
            Tuple[List[str], List[str]]: Two lists, one for regular papers and one for preprints, each formatted for Slack.
        """
        papers = []
        preprints = []
        for paper in papers_list:
            emoji = ":pencil:" if paper[3] == "TRUE" else ":rolled_up_newspaper:"
            # 列順: [DOI, Date, PostedDate, IsPreprint, Title, Journal, Keywords, Preprint, Abstract_JP, URL]
            title = paper[4]
            journal = paper[5] if len(paper) > 5 else ""
            abstract_jp = re.sub(r'\*\*(.+?)\*\*', r'*\1*', paper[8] if len(paper) > 8 else "")
            url = paper[-1]

            reason = (reasons or {}).get(str(paper[0]).strip().lower(), "")

            formatted_paper = f"{emoji} <{url}|{title}>"
            if journal:
                formatted_paper += f"\n> *{journal}*"
            if reason:
                formatted_paper += f"\n> :bulb: {reason}"
            if abstract_jp:
                formatted_paper += f"\n> {abstract_jp}"
            if paper[3] == "TRUE":
                preprints.append(formatted_paper)
            else:
                papers.append(formatted_paper)

        return papers, preprints

    def publish_papers_to_slack(
        self,
        papers: List[str],
        preprints: List[str],
        today: str,
        spreadsheet_id: str,
        part: Optional[Tuple[int, int]] = None,
    ) -> Optional[SlackResponse]:
        """
        Publishes the formatted papers and preprints to the specified Slack channel.

        Args:
            papers (List[str]): A list of formatted regular papers.
            preprints (List[str]): A list of formatted preprints.
            today (str): The date for the publication (used in the footer message).
            spreadsheet_id (str): The ID of the Google Spreadsheet containing the papers.
            part (Optional[Tuple[int, int]]): 複数のメッセージに分けて投稿するときの (何通目, 全通数)。
                見出しに「(1/3)」を付け、論文の無い欄（"No papers found today." など）は省く。

        Returns:
            Optional[dict]: The Slack API response on success, or None if there is an error.
        """
        try:
            header = "Good morning :coffee: Here are today's papers! Enjoy your reading! :wave:"
            header += f" ({part[0]}/{part[1]})\n" if part else "\n"
            footer = (
                f"*View all papers:* <https://docs.google.com/spreadsheets/d/{spreadsheet_id}|Google Sheet> :books:"
            )
            message_blocks: List[Dict[str, Any]] = [
                {"type": "section", "text": {"type": "mrkdwn", "text": header}},
            ]
            message_blocks += self._list_blocks("*Preprints:*:point_down:", preprints, "No preprints found today.", part)
            message_blocks += self._list_blocks("*Papers:*:point_down:", papers, "No papers found today.", part)
            message_blocks.append({"type": "divider"})
            message_blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": footer}})
            message_blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"Published on {today}"},
            })
            message_blocks.append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "Posted with `paperbee` <https://github.com/theislab/paperbee|GitHub>",
                },
            })
            if not self.channel_id:
                self.logger.error("Channel ID is not provided.")
                return None
            response = self.client.chat_postMessage(
                channel=self.channel_id,
                blocks=message_blocks,
                text=header,
                unfurl_links=False,
                unfurl_media=False,
            )
            self.logger.info(f"Published papers to Slack: {response}")
        except Exception:
            self.logger.exception("Error in publishing papers to Slack: ", exc_info=True)
            return None
        else:
            return response

    @staticmethod
    def _list_blocks(
        title: str, items: List[str], empty_text: str, part: Optional[Tuple[int, int]]
    ) -> List[Dict[str, Any]]:
        """論文の欄（区切り・見出し・各論文）のブロック。分割投稿（part あり）では空の欄を出さない。"""
        if not items and part:
            return []
        texts = items or [empty_text]
        return [
            {"type": "divider"},
            {"type": "section", "text": {"type": "mrkdwn", "text": title}},
            *({"type": "section", "text": {"type": "mrkdwn", "text": text}} for text in texts),
        ]

    def _send_csv(self, papers: pd.DataFrame, root_dir: str, user_id: str, user_query: str) -> Optional[SlackResponse]:
        """
        Sends a CSV file of papers to the specified Slack channel.

        Args:
            papers (pd.DataFrame): The DataFrame containing the papers to be sent.
            root_dir (str): The root directory where the CSV file will be temporarily saved.
            user_id (str): The Slack user ID of the person who requested the CSV.
            user_query (str): The query string used by the user to filter papers.

        Returns:
            Optional[dict]: The Slack API response on success, or None if there is an error.
        """
        csv_path = os.path.join(root_dir, f"{user_id}_requested_papers.csv")
        papers.to_csv(csv_path, index=False)

        try:
            initial_comment = (
                f"Hey <@{user_id}>, here is the CSV file of papers based on your query:\n\n '{user_query}'."
            )

            # Upload the CSV file to Slack
            response = self.client.files_upload(
                channels=self.channel_id,
                file=csv_path,
                title="Requested_Papers",
                initial_comment=initial_comment,
            )

            self.logger.info(f"Successfully uploaded papers CSV to Slack: {response}")

        except Exception:
            # Log any exceptions that occur during the upload
            self.logger.exception("Error encountered while sending CSV file to Slack.", exc_info=True)
            return None
        else:
            return response
        finally:
            # Clean up by removing the CSV file after the upload
            if os.path.exists(csv_path):
                os.remove(csv_path)
