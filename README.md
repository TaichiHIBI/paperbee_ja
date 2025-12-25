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

取得した論文は、コマンドラインでの手動選別、または LLMによる自動フィルタリング によって選別されます。 選別された論文はGoogleスプレッドシートに記録され、Slackなどのチャンネルに通知されます。 設定はシンプルな config.yml ファイルで行います。

### 🗂️ プロジェクト構造（主な変更点）
* src/PaperBee/papers/utils.py – 翻訳機能 (translate_abstract) を追加。

* src/PaperBee/papers/slack_papers_formatter.py – 日本語要約を表示できるようにフォーマットを修正。

* src/PaperBee/daily_posting.py – 設定ファイルから翻訳オプションを読み込むように修正。

* src/PaperBee/papers/papers_finder.py – 翻訳フローを統合。

## 📦 インストール
ソースコードを修正しているため、以下の手順でインストールしてください：

```bash
git clone https://github.com/TaichiHIBI/paperbee_ja.git
cd paperbee_ja
# 必要なら仮想環境有効化
pip install -e .
```

## 📝 セットアップガイド
[フォーク元のセットアップガイド](https://github.com/theislab/paperbee)に従ってセットアップしてください。

## ⚙️ 設定ファイル (Configuration)
PaperBee_jaはすべての設定をYAMLファイルで管理します。 以下のテンプレートを config.yml として保存・編集してください。

config.yml の例（日本語要約機能付き）

⚠️queryの書き方やFiltering promptの形式はもとの手法を参照してください。
```YAML
GOOGLE_SPREADSHEET_ID: "your-google-spreadsheet-id"
GOOGLE_CREDENTIALS_JSON: "/path/to/your/google-credentials.json"
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
LLM_FILTERING: true
LLM_PROVIDER: "ollama"       # "ollama" または "openai"
LANGUAGE_MODEL: "gemma2"     # 使用するモデル名 (例: gemma2, llama3, gpt-4o-mini)
# OPENAI_API_KEY: "your-key"

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
TRANSLATION_ENABLED: true
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
```Bash
# 過去1日分の論文を検索し、自動でフィルタリング・要約してSlackに投稿
paperbee post --config /path/to/config.yml --since 1 --databases pubmed biorxiv

--config : 設定ファイルのパス。
--since : 何日前まで遡って検索するか（デフォルト: 1日）。
--interactive : (オプション) これを付けると、LLMフィルタリングの後に手動で Yes/No を選択できます。自動化する場合は付けないでください。
--databases: (オプション) 検索対象データベースを指定します（例: pubmed biorxiv arxiv）。
```

### 自動実行（cron）
毎日午前9時に実行する場合のcron設定例:
```Bash
0 9 * * * /path/to/your/venv/bin/paperbee post --config /path/to/config.yml --since 1 --databases pubmed biorxiv
```

## 📚 Reference
Original PaperBee:
```
@misc{shitov_patpy_2024,
  author = {Lucarelli, Daniele and Shitov, Vladimir A. and Saur, Dieter and Zappia, Luke and Theis, Fabian J.},
  title = {PaperBee: An Automated Daily Digest Bot for Scientific Literature Monitoring},
  year = {2025},
  url = {https://github.com/theislab/paperbee},
  note = {Version 1.2.0}
}
```