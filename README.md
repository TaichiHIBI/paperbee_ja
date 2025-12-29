# 🐝 PaperBee (日本語要約・翻訳対応版)
<img src="images/paperbee_logo.png" width="200" height="auto" alt="logo"/>

⚠️このツールは [theislab/paperbee](https://github.com/theislab/paperbee) を元に、論文要旨の日本語訳・要約機能を付け加えたものです。

PaperBeeは、新しい科学論文を自動的に検索し、お気に入りのチャットツールに投稿するためのPythonアプリケーションです。

### 現在サポートされているプラットフォーム:

🟣 Slack (※日本語要約機能に対応)

🔵 Telegram

🟢 Zulip

🟠 Mattermost

## ✨ このバージョンでの変更点：日本語要訳・翻訳機能
本フォーク版では、LLM（Ollama, OpenAI, Gemini）を使用して、論文のアブストラクトを自動的に日本語に翻訳・要約する機能を追加しています。

### ⚠️ 重要なお知らせ
日本語でのアブストラクト要約・翻訳出力は、現在「Slack (🟣)」のみに対応しています。 他のプラットフォーム（Telegram, Zulip, Mattermost）では、通常の英語タイトルとリンクのみが表示されます。

### 🚀 仕組み
PaperBeeは、findpapers ライブラリを使用して、指定されたキーワードでPubMed、arXiv、bioRxivから科学論文を検索します。

取得した論文は、コマンドラインでの手動選別、または LLMによる自動フィルタリング によって選別されます。 選別された論文は**GoogleスプレッドシートまたはローカルのCSVファイル**に記録され、Slackなどのチャンネルに通知されます。 設定はシンプルな config.yml ファイルで行います。

### 🗂️ プロジェクト構造（主な変更点）
* src/PaperBee/papers/utils.py – 翻訳機能 (translate_abstract) を追加。

* src/PaperBee/papers/slack_papers_formatter.py – 日本語要約を表示できるようにフォーマットを修正。

* src/PaperBee/daily_posting.py – 設定ファイルから翻訳オプションを読み込むように修正。

* src/PaperBee/papers/papers_finder.py – 翻訳フローおよびローカル履歴管理機能を統合。

## 📦 インストール
ソースコードを修正しているため、以下の手順でインストールしてください：

```bash
git clone https://github.com/TaichiHIBI/paperbee_ja.git
cd paperbee_ja

# 必要なら仮想環境有効化
pip install -e .
```

## 📝 セットアップガイド

### Slack通知設定：必須
1. [Slack App を作成](https://api.slack.com/apps/new)します（「From an app manifest」を選択してください）。
2. ワークスペースを選択します。
3. manifest.json の内容をコピーし、manifestの入力ボックスに貼り付けます。
4. 内容を確認し、アプリを作成（Create）します。
5. 「Install App」メニューへ移動し、ワークスペースにインストールして権限を許可します。
6. インストール前に「Bot Token Scope」の追加が必要になる場合があります。その際は、「OAuth & Permissions」 -> 「Scopes」へ移動し、任意の Bot Token Scope を追加してください。
7. 「OAuth & Permissions」にある Bot User OAuth Token をコピーし、config.yml ファイルの `bot_token` の箇所に貼り付けます。
8. 「Basic Information」 -> 「App-Level Tokens」にて、connections:write スコープを持つ App-Level Token を作成します。
9. config.yml ファイルの `SLACK_CHANNEL_ID` に、投稿先のチャンネル ID を設定します。
10. config.yml ファイル内のその他の SLACK 関連の変数（`app_token` など）も更新します。
### NCBI API Key 取得：必須
1. [このページ](https://www.ncbi.nlm.nih.gov/datasets/docs/v2/api/api-keys/)からNCBIアカウントにログインし、API keyを作成してください。

2. config.ymlの `NCBI_API_KEY` に取得したAPI Keyを貼り付けてください。
### LLM連携：いずれかを推奨
* LLMフィルタリング：OpenAI or Ollama
* アブストラクト翻訳：OpenAI or Ollama or Gemini
#### OpenAI API
1. [このページ](https://platform.openai.com/settings/organization/api-keys)からOpenAIアカウントにログインし、API Keyを作成してください。

2. config.ymlの `OEPENAI_API_KEY` に取得したAPI Keyを貼り付けてください。（LLMフィルタリング用。）

3. config.ymlの `TRANSLATION_API_KEY` に取得したAPI Keyを貼り付けてください。（翻訳用）
#### Google AI API
1. [このページ](https://ai.google.dev/aistudio?hl=ja)からGoogleアカウントにログインし、Gemini API Keyを作成してください。

2. config.ymlの `TRANSLATION_API_KEY` に取得したAPI Keyを貼り付けてください。（翻訳用）
#### Ollama 
1. [このページ](https://github.com/ollama/ollama)からOllamaをダウンロードし、お好みのローカルLLMモデルをダウンロードしてください。選択したLLMモデル名は `LANGUAGE_MODEL` `TRANSLATIONAL_MODEL` に使用します。


## ⚙️ 設定ファイル (Configuration)

PaperBee_jaはすべての設定をYAMLファイルで管理します。 以下のテンプレートを config.yml として保存・編集してください。

config.yml の例（日本語要約機能・ローカル履歴管理付き）

⚠️queryの書き方やFiltering promptの形式はフォーク元を参照してください。

config_yourforcus.ymlを複数作成してそれぞれ実行することで、異なる分野の論文をサーチすることができます。

```yaml
# -----------------------------------------------------------------------------
# Google Sheets設定 (オプション。非推奨：別途 google cloud 連携が必要)
# -----------------------------------------------------------------------------
# Google Sheetsで既読管理をする場合のみ設定してください。
# 使わない場合は空文字 "" に設定可能です。
GOOGLE_SPREADSHEET_ID: "your-google-spreadsheet-id"
GOOGLE_CREDENTIALS_JSON: "/path/to/your/google-credentials.json"

# -----------------------------------------------------------------------------
# ローカル履歴設定 (オプション。こちらを推奨)
# -----------------------------------------------------------------------------
# Google Sheetsの代わりにローカルのCSVファイルで既読管理を行います。
# ファイルは LOCAL_ROOT_DIR 直下に保存されます。
HISTORY_FILE: "history.csv"

NCBI_API_KEY: "your-ncbi-api-key"

# ローカルルートディレクトリへのパス
LOCAL_ROOT_DIR: "/path/to/local/root/dir"

# 検索クエリ設定
# bioRxivは複雑なクエリに対応していないため、単純なOR検索のみ記述します
query_biorxiv: "[machine learning for single-cell] OR [deep learning for single-cell] OR [AI for single-cell]"

# PubMed/arXiv用クエリ (AND, OR, NOTが使用可能)
query_pubmed_arxiv: "([single-cell transcriptomics]) AND ([AI] OR [machine learning] OR [deep learning])"

# -----------------------------------------------------------------------------
# LLMフィルタリング設定 (オプション)
# -----------------------------------------------------------------------------
LLM_FILTERING: false
LLM_PROVIDER: "ollama"       # "ollama" または "openai"
LANGUAGE_MODEL: "gemma2"     # 使用するモデル名 (例: gemma2, llama3, gpt-4o-mini)
OPENAI_API_KEY: "your-key"

# フィルタリング用プロンプト
# 興味のある分野や、除外したい論文の条件を記述します。
FILTERING_PROMPT: |
  You are a researcher in a computational biology lab. Your goal is to identify papers that propose **novel algorithms**.
  Criteria for Relevance (YES):
  - Proposes a new algorithm or method.
  - Applicable to human data.
  Criteria for Exclusion (NO):
  - Purely clinical studies.
  - Review papers.
  Please answer 'yes' or 'no' to the following question: Is the following research paper relevant?

# -----------------------------------------------------------------------------
# 🇯🇵 日本語翻訳・要約設定 (本フォーク版の機能)
# -----------------------------------------------------------------------------
TRANSLATION_ENABLED: false
TRANSLATION_PROVIDER: "ollama"      # "ollama", "openai", "gemini"
TRANSLATION_MODEL: "gemma2"         # モデル名は環境に合わせてください
TRANSLATION_API_KEY: ""             # OpenAI/Geminiの場合のみ必要

# 翻訳・要約用プロンプト
TRANSLATION_PROMPT: |
  以下の科学論文のアブストラクトを、日本語で3点の箇条書きに要約してください。
  出力は日本語の要約のみを行ってください。

# -----------------------------------------------------------------------------
# Slack設定 (日本語要約対応)
# -----------------------------------------------------------------------------
SLACK:
  is_posting_on: true
  bot_token: "your_slack_bot_token"
  channel_id: "your_slack_channel_id"
  app_token: "your_slack_app_token"

# その他のプラットフォーム設定...
TELEGRAM:
  is_posting_on: false
  # ...

```

## ▶️ Botの実行

設定が完了したら、以下のコマンドで実行します。

### コマンドラインからの実行

```bash
# 過去1日分の論文を検索し、自動でフィルタリング・要約してSlackに投稿
paperbee post --config /path/to/config.yml --since 1 --databases pubmed biorxiv

--config: 設定ファイルのパス。
--since: 何日前まで遡って検索するか（デフォルト: 1日）。
--interactive: オプション。これを付けると、手動で採択するかを選択できます。自動化する場合は付けないでください。
--databases: オプション。検索対象データベースを指定します（例: pubmed biorxiv arxiv）。
```

### 自動実行（cron）

毎日午前9時に実行する場合のcron設定例:

```bash
0 9 * * * /path/to/your/venv/bin/paperbee post --config /path/to/config.yml --since 1 --databases pubmed biorxiv

```

## 📚 Reference

Original PaperBee:

```
@misc{shitov_patpy_2024,
  author = {Lucarelli, Daniele and Shitov, Vladimir A. and Saur, Dieter and Zappia, Luke and Theis, Fabian J.},
  title = {PaperBee: An Automated Daily Digest Bot for Scientific Literature Monitoring},
  year = {2025},
  url = {[https://github.com/theislab/paperbee](https://github.com/theislab/paperbee)},
  note = {Version 1.2.0}
}
```