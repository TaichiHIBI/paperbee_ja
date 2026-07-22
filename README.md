# 🐝 PaperBee (日本語要約・翻訳対応版)
<img src="images/paperbee_logo.png" width="200" height="auto" alt="logo"/>

⚠️このツールは [theislab/paperbee](https://github.com/theislab/paperbee) を元に、論文要旨の日本語訳・要約機能を付け加えたものです。

PaperBeeは、新しい科学論文を自動的に検索し、お気に入りのチャットツールに投稿するためのPythonアプリケーションです。

### 現在サポートされているプラットフォーム:

🟣 Slack (※日本語要約機能に対応)

🔵 Telegram

🟢 Zulip

🟠 Mattermost

## ✨ 本フォーク版の主な機能

元の [theislab/paperbee](https://github.com/theislab/paperbee) に対して、以下を追加・強化しています。

- **日本語要約・翻訳** — LLM（Ollama / OpenAI / Gemini）でアブストラクトを日本語に要約・翻訳（「要約→翻訳」の2段階。片方のみの利用も可）
- **ジャーナル名表示** — Slack投稿に掲載誌名を表示
- **著者名検索** — `query_authors` で PubMed / arXiv の著者フィールドを直接検索（多数の著者も自動分割で対応）
- **LLMフィルタリング** — OpenAI / Ollama に加え Gemini に対応
- **検索の堅牢化** — bioRxiv の間欠的な取りこぼしに対するリトライ、`FILTER_UNEDITED` オプション

> ⚠️ 日本語の要約・翻訳とジャーナル名表示は **Slack のみ** 対応です。Telegram / Zulip / Mattermost では英語タイトルとリンクのみ表示されます。

### 🚀 仕組み
findpapers ライブラリで PubMed・arXiv・bioRxiv を検索し、手動選別または LLM による自動フィルタリングで論文を選別します。選別結果は GoogleスプレッドシートまたはローカルCSVに記録され、Slack等へ通知されます。設定は `config.yml` 1ファイルで完結します。

詳しい変更履歴は末尾の [更新履歴](#-更新履歴) を参照してください。

<details>
<summary>🗂️ フォーク元からの主な変更ファイル</summary>

- `papers/utils.py` – 翻訳機能 (`translate_abstract`)
- `papers/slack_papers_formatter.py` – 日本語要約・ジャーナル名の表示
- `daily_posting.py` – 設定からの翻訳／フィルタオプション読み込み
- `papers/papers_finder.py` – 翻訳フロー、著者検索（多人数分割）、ジャーナル名抽出、ローカル履歴管理、bioRxiv リトライ、Unedited version フィルタ
- `papers/llm_filtering.py` – Gemini フィルタリング対応

</details>

## 📦 インストール
ソースコードを修正しているため、以下の手順でインストールしてください：

```bash
git clone https://github.com/TaichiHIBI/paperbee_ja.git
cd paperbee_ja

# 必要なら仮想環境有効化 python=3.12を推奨
pip install .
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
* LLMフィルタリング：OpenAI or Ollama or Gemini
* アブストラクト翻訳：OpenAI or Ollama or Gemini
#### OpenAI API
1. [このページ](https://platform.openai.com/settings/organization/api-keys)からOpenAIアカウントにログインし、API Keyを作成してください。

2. config.ymlの `LLM_API_KEY` に取得したAPI Keyを貼り付けてください。（LLMフィルタリング用）

3. config.ymlの `TRANSLATION_API_KEY` に取得したAPI Keyを貼り付けてください。（翻訳用）
#### Google AI API
1. [このページ](https://ai.google.dev/aistudio?hl=ja)からGoogleアカウントにログインし、Gemini API Keyを作成してください。

2. config.ymlの `LLM_API_KEY` に取得したAPI Keyを貼り付けてください。（LLMフィルタリング用）

3. config.ymlの `TRANSLATION_API_KEY` に取得したAPI Keyを貼り付けてください。（翻訳用）
#### Ollama 
* [このページ](https://github.com/ollama/ollama)からOllamaをダウンロードし、お使いのデバイスに合ったローカルLLMモデルをダウンロードしてください。選択したLLMモデル名は `LANGUAGE_MODEL` `TRANSLATIONAL_MODEL` に使用します。
  ```bash
  # インストール例
  # 翻訳用モデル（PLaMo-2 翻訳特化版）
  ollama pull mitmul/plamo-2-translate:Q8_0

  # 要約・フィルタリング用モデル（gpt-oss:20b）
  ollama pull gpt-oss:20b
  ```

## ⚙️ 設定ファイル (Configuration)

PaperBee_jaはすべての設定をYAMLファイルで管理します。 以下のテンプレートを config.yml として保存・編集してください。

config.yml の例（日本語要約機能・ローカル履歴管理付き）

⚠️queryの書き方やFiltering promptの形式はフォーク元を参照してください。

config_yourforcus.ymlを複数作成してそれぞれ実行することで、異なる分野の論文をサーチすることができます。その場合は、history.csvの名前も合わせて変更するように `HISTORY_FILE` のファイル名を設定してください。

<details>
<summary>📄 config.yml テンプレート全文（クリックで展開）</summary>

```yaml
# -----------------------------------------------------------------------------
# Google Sheets / ローカル履歴設定
# -----------------------------------------------------------------------------
# Google Sheets ID (使用しない場合は空文字)
GOOGLE_SPREADSHEET_ID: ""
GOOGLE_CREDENTIALS_JSON: ""

# ローカル履歴ファイル (推奨)
HISTORY_FILE: "history.csv"
LOCAL_ROOT_DIR: "../paperbee_ja/files"

NCBI_API_KEY: "your-ncbi-api-key"

# -----------------------------------------------------------------------------
# 検索クエリ設定（いずれか1つ以上を設定。組み合わせ可）
# -----------------------------------------------------------------------------
# キーワード検索
query_biorxiv: "[machine learning for single-cell] OR [deep learning for single-cell]"
query_pubmed_arxiv: "([single-cell transcriptomics]) AND ([AI] OR [machine learning])"

# 著者名直接検索（PubMed/arXiv のみ対応。カンマ区切りで複数指定可）
# query_authors: "Jane Doe, John Smith"

# -----------------------------------------------------------------------------
# LLMフィルタリング設定 (オプション)
# -----------------------------------------------------------------------------
LLM_FILTERING: false
LLM_PROVIDER: "ollama"            # "ollama", "openai", "gemini"
LANGUAGE_MODEL: "gpt-oss:20b"     # フィルタリングに使用するモデル
LLM_API_KEY: ""                   # API使用時のみ

FILTERING_PROMPT: |
  "You are a lab manager at a research lab focusing on machine learning methods development for single-cell RNA sequencing. Lab members are interested in developing methods to model cell dynamics. You are reviewing a list of research papers to determine if they are relevant to your lab. Please answer 'yes' or 'no' to the following question: Is the following research paper relevant?"

# -----------------------------------------------------------------------------
# 🇯🇵 日本語要約・翻訳パイプライン設定
# -----------------------------------------------------------------------------
# 【Step 1: 要約】 (翻訳の前段階として、または要約のみに使用)
SUMMARIZATION_ENABLED: false
SUMMARIZATION_PROVIDER: "ollama" # "ollama", "openai", "gemini"
SUMMARIZATION_MODEL: "gemma2"    # 要約に使用するモデル
SUMMARIZATION_API_KEY: ""        # API使用時のみ

SUMMARIZATION_PROMPT: |
  以下の日本語の論文アブストラクトを、専門家向けに重要なポイントを3点の箇条書きで要約してください。
  出力は要約のみを行ってください。

# 【Step 2: 翻訳】 (Step 1の結果、または原文を翻訳)
TRANSLATION_ENABLED: false
TRANSLATION_PROVIDER: "ollama"                 # "ollama", "openai", "gemini"
TRANSLATION_MODEL: "mitmul/plamo-2-translate"  # 翻訳に使用するモデル
TRANSLATION_API_KEY: ""                        # API使用時のみ

# 入力されたテキスト（英語要約 or 英語原文）を日本語翻訳
TRANSLATION_PROMPT: |
  Translate the following scientific abstract into Japanese. Output ONLY the translation.

# -----------------------------------------------------------------------------
# Slack設定
# -----------------------------------------------------------------------------
SLACK:
  is_posting_on: true
  bot_token: "your_slack_bot_token"
  channel_id: "your_slack_channel_id"
  app_token: "your_slack_app_token"

# その他のプラットフォーム設定...(使用非推奨。is_posting_on: false)
# Telegram configuration
TELEGRAM:
  is_posting_on: false
  bot_token: "your-telegram-bot-token"
  channel_id: "your-telegram-channel-id"

# Zulip configuration
ZULIP:
  is_posting_on: false
  prc: "path-to-your-zulip-prc"
  stream: "your-zulip-stream"
  topic: "your-zulip-topic"

# Mattermost configuration
MATTERMOST:
  is_posting_on: false              # Set to true to enable Mattermost posting
  url: "your-mattermost-url"        # e.g. mattermost.example.com (do NOT include https://)
  token: "your-mattermost-access-token" # Your Mattermost personal access token (do NOT commit real tokens)
  team: "your-mattermost-team-name" # The team name (not display name)
  channel: "your-mattermost-channel-name" # The channel name (not display name)
```

</details>

## ▶️ Botの実行

設定が完了したら、以下のコマンドで実行します。

### コマンドラインからの実行

```bash
# 過去1日分の論文を検索し、自動でフィルタリング・要約してSlackに投稿
paperbee post --config /path/to/config.yml --since 1 --databases pubmed biorxiv

--config: 設定ファイルのパス。
--since: 何日前まで遡って検索するか（デフォルト: 1日）。
--interactive: オプション。これを付けると、手動で採択するかを選択できます。自動化する場合は付けないでください。
--databases: オプション。検索対象データベースを任意の数指定します（選択肢: pubmed biorxiv arxiv）。
```

### 自動実行（cron）

毎日午前9時に実行する場合のcron設定例:

```bash
0 9 * * * /path/to/your/venv/bin/paperbee post --config /path/to/config.yml --since 1 --databases pubmed biorxiv

```

### TIPS
* `config.yml`, `history.csv`, `~.json` などが格納される`files` フォルダを、ローカルにマウント済みのクラウドストレージなどに設定しておくと、同一の設定・検索履歴を保持したまま複数のデバイスで使用することができます。
* 定期実行する場合は、常に起動しているサーバマシンなどで実行するか、お使いのデバイスをスリープしない設定にすることをお勧めします。
* フィルタリングプロンプトの形式や、検索クエリの書き方は非常に重要です。
* 翻訳部分は翻訳特化モデルにすると、より良い結果が得られます。

## 📋 更新履歴

<details>
<summary><b>2026/07/22</b> — 検索の堅牢化</summary>

- **bioRxiv検索のリトライ**: 0件時に自動で再試行（最大3回・2秒間隔）し、最も多く取得できた結果を採用。レート制限等による間欠的な取りこぼしを軽減。
- **著者検索の分割**: `query_authors` に多数の著者を指定するとURLが長くなり PubMed（HTTP 414）・arXiv（HTTP 400）が**エラーも出さず0件**になっていた問題を、25名ずつのバッチ検索＋重複除去で修正。
- **`FILTER_UNEDITED`（オプション）**: CrossRef の参照数が0の論文（編集前の early access 版に相当）を除外。
- **履歴CSVの堅牢化**: 過去のスキーマ変更や列数の揺れに耐えるよう、履歴行を正準スキーマへ正規化してから読み込み。

</details>

<details>
<summary><b>2026/04/27</b> — ジャーナル名表示</summary>

Slack投稿に掲載誌名を表示。

```
📰 論文タイトル
> *Nature Neuroscience*
> 日本語要約...
```

</details>

<details>
<summary><b>2026/02/24</b> — 著者名検索</summary>

`query_authors` にカンマ区切りで著者名を指定すると PubMed（`[Author]`）・arXiv（`au:`）を直接検索。キーワード検索（`query_pubmed_arxiv`）と併用可。

⚠️ bioRxiv は非対応。bioRxiv で著者検索する場合は `query_biorxiv` に著者名を直接記述してください。

</details>

<details>
<summary><b>2026/01/29</b> — 要約と翻訳の分離</summary>

処理順序は **Step1（要約）→ Step2（翻訳）** の固定順。

| SUMMARIZATION_ENABLED | TRANSLATION_ENABLED | 動作 |
|---|---|---|
| true | true | 英語で要約 → 日本語に翻訳（推奨） |
| false | true | アブストラクト全文を日本語に翻訳 |
| true | false | 英語のまま要約 |

</details>

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