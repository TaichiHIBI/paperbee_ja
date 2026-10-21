# 🐝 PaperBee (日本語要約・翻訳対応版)
<img src="images/paperbee_logo.png" width="200" height="auto" alt="logo"/>

⚠️このツールは [theislab/paperbee](https://github.com/theislab/paperbee) を元に、論文要旨の日本語訳・要約機能を付け加えたものです。

PaperBeeは、新しい科学論文を自動的に検索し、お気に入りのチャットツールに投稿するためのPythonアプリケーションです。

> **最終更新: 2026/10/21** — Claude API に対応し（フィルタリングの判定・日本語要約。推奨は Sonnet 5.5）、ローカルLLMは一次ふるい・代行役として組み合わせられます。bioRxiv・arXiv の検索と bioRxiv 新着の取りこぼしを修正し、採用が多い日の Slack 分割投稿にも対応しました。（[更新履歴](#-更新履歴)）

### 現在サポートされているプラットフォーム:

🟣 Slack (※日本語要約機能に対応)

🔵 Telegram

🟢 Zulip

🟠 Mattermost

## ✨ 本フォーク版の主な機能

元の [theislab/paperbee](https://github.com/theislab/paperbee) に対して、以下を追加・強化しています。

- **日本語要約・翻訳** — LLM（Claude / Ollama / OpenAI / Gemini）でアブストラクトを日本語に要約・翻訳（「要約→翻訳」の2段階。片方のみの利用も可）
- **ジャーナル名表示** — Slack投稿に掲載誌名を表示
- **著者名検索** — `query_authors` で PubMed / arXiv / bioRxiv の著者を直接検索（多数の著者も自動分割で対応）
- **LLMフィルタリング** — Claude / OpenAI / Ollama / Gemini に対応。アブストラクトまで読んで判定し、判定理由を記録
- **Claude + ローカルLLM の分担** — Claude が最終判定、ローカルLLMが一次ふるいと障害時の代行（判定・要約・翻訳）。月間予算ガード付き
- **検索の堅牢化** — PubMed・arXiv・bioRxiv の API を直接検索（収載の遅れ・公開の遅れで取りこぼさない期間指定）、`FILTER_UNEDITED` オプション

> ⚠️ 日本語の要約・翻訳とジャーナル名表示は **Slack のみ** 対応です。Telegram / Zulip / Mattermost では英語タイトルとリンクのみ表示されます。

### 🚀 仕組み
PubMed（E-utilities）・arXiv API・bioRxiv API を直接検索し、手動選別または LLM による自動フィルタリングで論文を選別します。選別結果は GoogleスプレッドシートまたはローカルCSVに記録され、Slack等へ通知されます。設定は `config.yml` 1ファイルで完結します。

詳しい変更履歴は末尾の [更新履歴](#-更新履歴) を参照してください。

<details>
<summary>🗂️ フォーク元からの主な変更ファイル</summary>

- `papers/utils.py` – 翻訳機能 (`translate_abstract`)
- `papers/slack_papers_formatter.py` – 日本語要約・ジャーナル名の表示
- `daily_posting.py` – 設定からの翻訳／フィルタオプション読み込み
- `papers/papers_finder.py` – 翻訳フロー、著者検索（多人数分割）、ジャーナル名抽出、ローカル履歴管理、各DBの直接検索、Unedited version フィルタ
- `papers/search_query.py` – 検索クエリの解釈（PubMed / arXiv の検索式への変換、bioRxiv のローカル照合）
- `papers/llm_filtering.py` – Claude / Gemini フィルタリング、ローカルLLMの一次ふるい・代行判定
- `papers/claude_client.py` – Claude API 呼び出し、利用量台帳と月間予算ガード

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
* LLMフィルタリング：Claude or OpenAI or Ollama or Gemini
* アブストラクト翻訳：Claude or OpenAI or Ollama or Gemini
#### Claude API（推奨構成）
1. [Claude Console](https://platform.claude.com/) で API Key を作成し、config.yml の `LLM_API_KEY` に貼り付けてください。要約・翻訳も Claude にする場合、それらのキー欄は空欄のままで `LLM_API_KEY` が使われます（環境変数 `ANTHROPIC_API_KEY` でも可）。
2. 件数は少ないが精度が効く工程を Claude に、件数の多い工程をローカルLLMに任せる構成を推奨します。モデルは `claude-sonnet-5-5`（`CLAUDE_EFFORT: "high"`）を推奨します。180件で比べたところ、Opus 5.5 と同等の判定を約半分の費用で出しました。Sonnet の high は low と比べて費用が約5%増えるだけで、判定が安定しました（2回の判定が完全に一致し、見逃し0件）。
   * `LLM_PROVIDER: "claude"` — フィルタリングの最終判定（20件ずつまとめて判定し、理由も記録）
   * `SUMMARIZATION_PROVIDER: "claude"` + `TRANSLATION_ENABLED: false` — 日本語の箇条書き要約を1回で生成（ローカルの要約・翻訳モデルの読み込みが不要になる）
   * `LOCAL_LLM_PROVIDER: "ollama"` — Claude が使えないとき（予算超過・障害）に、判定と要約・翻訳を同じ指示で代行。`LOCAL_PREFILTER: true` で Claude の前の一次ふるいにも使う
3. 支出は `LOCAL_ROOT_DIR/claude_usage.csv` に記録され、全トピック合算で `CLAUDE_MONTHLY_BUDGET_USD`（既定 $100）に達すると、その月は Claude を呼ばずローカルLLMで処理します。
4. フィルタリングの判定は履歴ファイルの隣の `{履歴ファイル名}_decisions.csv` に理由つきで残ります。プロンプト調整に使ってください。同じ基準で不採用になった論文は、翌日以降に再判定しません。

#### API を使わない構成（ローカルLLMのみ）
Claude API はなくても動きます。`LLM_PROVIDER` / `SUMMARIZATION_PROVIDER` / `TRANSLATION_PROVIDER` を `"ollama"` にし、API キーや `LOCAL_LLM_*` は設定不要です。

| 機能 | Claude | ローカルLLM（Ollama） |
|---|---|---|
| フィルタリング（アブストラクトまで読む） | 20件ずつまとめて判定 | 1件ずつ判定 |
| 採用理由の Slack 表示・判定ログ | ✓ | ✓（JSON スキーマで yes/no と理由を返させる） |
| 判定キャッシュ・同姓同名ルール・検索 | ✓ | ✓（LLM に依存しない） |
| 月間予算ガード | ✓ | 不要 |
| 障害時の代行（判定・要約・翻訳） | ローカルLLMが代行 | ― |

gemma4 などの思考するモデルは、回答が yes/no の1語でも内部で数百トークン推論するため、1件あたり20秒前後かかります（M4 Pro / 24GB の実測）。
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

# 著者名直接検索（PubMed/arXiv/bioRxiv。カンマ区切りで複数指定可）
# query_authors: "Jane Doe, John Smith"
# 同姓同名の多い名前は、著者リストの末尾2人にいる論文だけを候補にする（"auto" / 名前のリスト / "off"）
# AUTHOR_COMMON_NAMES: "auto"

# -----------------------------------------------------------------------------
# LLMフィルタリング設定 (オプション)
# -----------------------------------------------------------------------------
LLM_FILTERING: false
LLM_PROVIDER: "claude"            # "claude", "ollama", "openai", "gemini"
LANGUAGE_MODEL: "claude-sonnet-5-5" # フィルタリングに使用するモデル（推奨。effort は CLAUDE_EFFORT: "high"）
LLM_API_KEY: ""                   # LLM_PROVIDER の API キー（ollama は不要。空なら環境変数 ANTHROPIC_API_KEY 等）

# Claude API（LLM_PROVIDER / SUMMARIZATION_PROVIDER / TRANSLATION_PROVIDER が "claude" のとき）
CLAUDE_EFFORT: "high"             # 思考の深さ: low / medium / high（推奨 high）
CLAUDE_BATCH_SIZE: 20             # 1リクエストで判定する論文数
CLAUDE_MONTHLY_BUDGET_USD: 100    # 全トピック合算の月間上限（超えたらローカルLLMで処理）

# ローカルLLMのサブエージェント（Claude と併用）
LOCAL_LLM_PROVIDER: "ollama"      # Claude が使えないときの代行（判定・要約・翻訳）
LOCAL_LLM_MODEL: "gemma4:26b"
LOCAL_PREFILTER: false            # true で Claude の前に一次ふるい（明らかに無関係なものだけ落とす）

FILTERING_PROMPT: |
  "You are a lab manager at a research lab focusing on machine learning methods development for single-cell RNA sequencing. Lab members are interested in developing methods to model cell dynamics. You are reviewing a list of research papers to determine if they are relevant to your lab. Please answer 'yes' or 'no' to the following question: Is the following research paper relevant?"

# -----------------------------------------------------------------------------
# 🇯🇵 日本語要約・翻訳パイプライン設定
# -----------------------------------------------------------------------------
# 【Step 1: 要約】 (翻訳の前段階として、または要約のみに使用)
SUMMARIZATION_ENABLED: false
SUMMARIZATION_PROVIDER: "ollama" # "claude", "ollama", "openai", "gemini"
SUMMARIZATION_MODEL: "gemma2"    # 要約に使用するモデル
SUMMARIZATION_API_KEY: ""        # 空でプロバイダーが LLM_PROVIDER と同じなら LLM_API_KEY を使う

SUMMARIZATION_PROMPT: |
  以下の日本語の論文アブストラクトを、専門家向けに重要なポイントを3点の箇条書きで要約してください。
  出力は要約のみを行ってください。

# 【Step 2: 翻訳】 (Step 1の結果、または原文を翻訳)
TRANSLATION_ENABLED: false
TRANSLATION_PROVIDER: "ollama"                 # "claude", "ollama", "openai", "gemini"
TRANSLATION_MODEL: "mitmul/plamo-2-translate"  # 翻訳に使用するモデル
TRANSLATION_API_KEY: ""                        # 空でプロバイダーが LLM_PROVIDER と同じなら LLM_API_KEY を使う

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
<summary><b>2026/10/21</b> — 大量投稿時の取りこぼし対策</summary>

- **Slack の分割投稿**: 採用が多い日（35件超）は複数のメッセージに分けて投稿する（見出しに「(1/3)」）。Slack は1メッセージ50ブロックまでで、超えると投稿全体が拒否されていた。
- **履歴は投稿後に記録**: Slack に投稿できた論文だけを履歴に書く。以前は投稿前に書いていたため、投稿に失敗した論文が「投稿済み」になり二度と流れなかった。
- **arXiv のアクセス制限への対応**: 通常の間隔を5秒に広げ、429・503 では `Retry-After`（無ければ30・60・120秒）待って再試行する。
- **推奨モデルを Sonnet 5.5（effort high）に変更**: 180件の比較で、Opus 5.5 の最上位設定（max・xhigh）の判定に対し、Sonnet 5.5 は Opus 5.5 low と同等（見逃し0〜1件・混入2件）で費用は約半分。Sonnet の high は low より費用が約5%多いだけで、2回の判定が完全に一致した。ローカルの gemma4:26b は見逃し0件だが混入が15件、1件あたり約28秒だった。
- **要約・翻訳にもローカルLLMの代行**: API での要約・翻訳に失敗したら、`LOCAL_LLM_PROVIDER` / `LOCAL_LLM_MODEL` のローカルLLMが同じ指示で作り直す（未設定なら従来どおり原文のまま）。Gemini のキーが無いときに「(Error: API Key missing)」付きの原文が投稿されていた問題も修正。
- **API キーを工程別の欄に一本化**: Claude のキーも他のプロバイダーと同じく `LLM_API_KEY` などに書く。要約・翻訳のキー欄が空で、プロバイダーが判定と同じなら `LLM_API_KEY` を使う。2026/10/20 版の `ANTHROPIC_API_KEY` 欄は非推奨（当面は読み込み、警告を出す）。

</details>

<details>
<summary><b>2026/10/20</b> — Claude API 対応・キーワード検索と bioRxiv 著者検索の修正</summary>

- **Claude API 対応**: `LLM_PROVIDER` / `SUMMARIZATION_PROVIDER` / `TRANSLATION_PROVIDER` に `"claude"` を指定可能。フィルタリングは20件ずつ1リクエストにまとめ、構造化出力で判定と理由を受け取る。安全分類器による誤拒否はサーバー側フォールバックで別モデルが引き継ぐ。
- **ローカルLLMとの分担**: `LOCAL_LLM_PROVIDER` のローカルLLMが、Claude の障害・予算超過時に判定を代行。`LOCAL_PREFILTER: true` で Claude の前の一次ふるいにも使える。
- **月間予算ガード**: 利用量を `claude_usage.csv` に記録し、全トピック合算で `CLAUDE_MONTHLY_BUDGET_USD` に達したらその月は Claude を呼ばない。
- **判定キャッシュと判定ログ**: 判定を `{履歴ファイル名}_decisions.csv` に理由つきで記録。同じ基準で不採用になった論文は、`--since` の期間が重なっても再判定しない。
- **フィルタリング精度**: ローカルLLMの判定にもアブストラクトを渡すよう変更（従来はタイトル・著者・キーワードのみ）。
- **採用理由の表示（`SLACK_SHOW_REASON`、既定 true）**: LLM が判定時に返す日本語の採用理由を、Slack 投稿の誌名の下に `:bulb:` で添える。Claude だけでなくローカルLLM（Ollama は JSON スキーマで出力を縛る）でも動く。
- **bioRxiv 著者検索の修正**: findpapers の bioRxiv 検索はタイトル・アブストラクトしか対象にしないため、`query_biorxiv` に書いた著者名は著者として一致していなかった。`query_authors` の著者を bioRxiv API の新着プレプリントと照合する方式に変更。
- **ハング対策**: Ollama 呼び出しにタイムアウト（600秒）を設定。応答が止まっても後続トピックの実行が止まらない。ローカルLLMのエラーで実行全体が落ちないようにした。
- `NCBI_API_KEY` が PubMed 著者検索に渡っていなかった問題を修正。
- **`FILTER_UNEDITED` が bioRxiv の新着をすべて除外していた問題を修正**: bioRxiv の DOI が `10.64898/` に移行したため、プレプリント除外の対象外判定（`10.1101/` のみ）をすり抜け、CrossRef の参照数0で落とされていた（2026/07/22 以降）。取得元DBと新しい接頭辞で判定するよう変更。
- 著者名の照合が PubMed 形式（`Theis FJ`）にも対応。
- **同姓同名ノイズの削減（`AUTHOR_COMMON_NAMES`）**: `"auto"` で、PubMed の過去1年の論文数が多い名前（`Wang B` など）を自動判定し、その名前にしか一致しない論文は著者リストの末尾2人（責任著者側）にいる場合だけ候補にする。100人超の著者を追う設定の過去30日分では、候補が1,297件→459件に減り、過去に採用された29件はすべて残った。名前のリストで直接指定、`"off"` で無効化も可能。
- **キーワード検索の修正（findpapers を廃止）**: bioRxiv と arXiv のキーワード検索が常に0件だった。bioRxiv は findpapers が使う検索画面（medrxiv.org/search）が Cloudflare のボット判定で 403 を返し、arXiv は API の応答形式の変更で findpapers が件数を読めず、例外が握りつぶされていた。3つのDBとも API を直接検索する方式に置き換えた。
  - **bioRxiv**: 期間内の新着（version 1）を bioRxiv API で全件取得し、`query_biorxiv` をタイトル・アブストラクトにローカルで照合（語の境界で一致、ハイフンは空白扱い、最後の語の複数形も一致）。`([a] OR [b]) AND ([c] OR [d])` の形も使える。取得結果は `LOCAL_ROOT_DIR/biorxiv_cache.json` に6時間保存し、続けて走る他トピックと共有する。
  - **arXiv**: arXiv API をタイトル・アブストラクト（`ti:` / `abs:`）で検索。投稿から公開まで数日空くため、期間の始まりを4日前に広げる（既読分は履歴と判定キャッシュで除外）。
  - **PubMed**: E-utilities を直接検索し、期間を出版日ではなく収載日（`[Date - Entrez]`）で切る。出版日より遅れて収載された論文を取りこぼさない。著者検索も同じ。

</details>

<details>
<summary><b>2026/07/23</b> — findpapersクラッシュ回避</summary>

- **不正レコードによる検索全体クラッシュを回避**: findpapers の `merge_duplications` が、一部の不正なPubMedレコード（`publication_date` が None 等）を含む結果で `AttributeError` を送出し、**検索結果が丸ごと0件**になる問題があった（1件の不正レコードで全論文を取りこぼす）。import時に None 耐性のあるラッパーへ差し替えて回避。重複統合に失敗しても後段のDOI除外・history照合で吸収されるため実害なし。site-packagesを直接書き換えないため再インストールにも強い。

</details>

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

※ 2026/10/20 から bioRxiv にも対応（`query_biorxiv` に著者名を書く方法は、著者としては一致しないため不要）。

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