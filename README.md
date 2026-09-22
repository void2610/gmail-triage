# Gmail Triage

TypeSafe AI の Jev (System One モデル) で Gmail のメールを自動分類し、不要メールをゴミ箱に移動、重要メールを Discord に通知する。

## 仕組み

```
cron (毎朝 7:00)
  └─ gmail_triage.py
       ├─ gmail_fetch: メール取得（本文込み、並列8）
       ├─ Jev: triage_questions.json の criteria で 1通ずつ並列分類
       │    └─ confidence が閾値未満のメールのみ Claude CLI (SKILL.md) で再判定
       ├─ Claude CLI: important メールの要約だけ生成
       ├─ Gmail API: ラベル付与 + 不要メール → ゴミ箱
       └─ Discord Webhook: 重要メールの要約を投稿
```

### モジュール構成

| モジュール | 責務 |
|---|---|
| `gmail_triage.py` | 認証、ラベル操作、Discord 通知、全体のオーケストレーション |
| `gmail_fetch.py` | Gmail からのメール取得と本文の平文化 |
| `jev_classifier.py` | Jev による分類と confidence ゲーティング |
| `claude_cli.py` | Claude CLI 呼び出し（再判定・要約） |
| `log_format.py` | 実行ログの桁揃え・色付け |

環境変数は**どのモジュールも import 時ではなく実行時に読む**。
`gmail_triage.py` が `load_dotenv()` を呼ぶより先に各モジュールが import されるため、
モジュール定数として `os.getenv` を書くと `.env` の設定が黙って無視される。

### なぜ Jev か

Jev は文字列を生成せず、型付きの設問を 1 パスで並列評価して構造化された値だけを返すモデル。
分類は本来この形に収まるため、LLM にテキストで JSON を書かせるより速く、スキーマ違反が構造的に起こらない。
代わりに文字列を作れないので、要約だけは Claude に残している。

| | 旧 (Claude バッチ) | 新 (Jev) |
|---|---|---|
| 判定単位 | 20通を1プロンプトに同梱 | 1通ずつ独立 (並列) |
| 他メールの文脈による判定揺れ | あり | なし |
| 出力形式エラー | パース失敗時に全件 keep へ退避 | 構造上発生しない |
| 曖昧なメール | 区別できない | confidence で検出し Claude へ回す |

実測 (未読40通、2026-09-21):

| | 実測値 |
|---|---|
| Gmail 取得 (本文込み、並列8) | 2.0 秒 |
| Jev の分類 | 1.6 秒 (40ms/通、並列8) |
| Claude 再判定に回った割合 | 10.0% (4/40通) |
| 全体 (再判定 + important の要約含む) | 約18 秒 |

再判定は該当メールをまとめて 1 回の Claude 呼び出しに送るため、エスカレーション率が上がっても呼び出し回数は増えない。

判定には件名・差出人に加えて**本文全文**を渡す (`MAX_BODY_CHARS` で上限、既定 4000 文字)。
Jev の入力は 100 万トークンあたり $0.042 と安いので、本文を渡すコストより誤判定を減らす利得が大きい。
実際、Cloudflare の「請求書がご利用いただけます」は件名だけでは `important` 寄り (0.52) だったが、
本文に請求額 $0 が含まれることで `delete` (0.80) に動いた。

本文取得は 1 通ずつの API 呼び出しになるため、`GMAIL_CONCURRENCY` (既定 8) で並列化している。

### confidence ゲーティング

Jev は選択結果と併せて確信度 (0〜1) と全ラベルの確率分布を返す。
閾値未満のメールだけ Claude に再判定させるため、判断に迷うメールにのみコストを払う。

| action | 既定の閾値 | 理由 |
|---|---|---|
| `delete` | 0.9 | ゴミ箱送りは取り消しに手間がかかるので厳しく |
| `important` / `keep` | 0.5 | ラベル付与のみで実害が小さい |

Jev と Claude の双方が判断できなかったメールは削除せず `keep` に倒す。

### 分類カテゴリ

| カテゴリ | action | 処理 |
|---------|--------|------|
| 🔴 重要 | `important` | `triage/important` ラベル + Discord 通知 |
| 🟡 確認 | `review` | `triage/review` ラベル + Discord 通知 |
| ⚪ 保留 | `keep` | `triage/keep` ラベル |
| 🗑️ 不要 | `delete` | `triage/delete` ラベル + ゴミ箱に移動 |

分類ルールは `triage_questions.json` の `criteria` を編集するだけで調整できる (Claude 再判定側のルールは `SKILL.md`)。Gmail 上でラベルによるフィルタで分類結果を確認可能。

## セットアップ

### 1. 依存インストール

```bash
uv sync
```

### 2. Google Cloud Console

1. プロジェクトを作成し Gmail API を有効化
2. OAuth 同意画面を設定（テストユーザーに自分を追加）
3. OAuth クライアント ID を作成（デスクトップアプリ）
4. `credentials.json` をプロジェクトルートに配置

### 3. TypeSafe AI

[console.typesafe.ai](https://console.typesafe.ai/) で API キーを発行する。

### 4. 環境変数

```bash
cp .env.example .env
```

`.env` を編集:

```
TYPESAFE_API_KEY=ts-XXXXXXXX
TYPESAFE_MODEL=jev-latest
JEV_CONCURRENCY=8
JEV_CONFIDENCE_THRESHOLD=0.5
JEV_DELETE_THRESHOLD=0.9
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/XXXX/YYYY
HOURS_BACK=24
MAX_EMAILS=50
DRY_RUN=false
```

### 5. 初回認証

```bash
uv run gmail-triage --dry-run
```

ブラウザが開くので Google アカウントで認可する。`token.json` が自動生成される。

#### トークン失効時の再認証

`invalid_grant: Token has been expired or revoked.` が出た場合は、保存済みの OAuth トークンが無効になっている。

```bash
uv run gmail-triage --dry-run
```

最新版では失効した `token.json` を自動で破棄する。ターミナルで上のコマンドを実行するとブラウザ認証が再度走る。`cron` 実行中はブラウザを開けないため、一度手動で再認証してから定期実行に戻す。

#### TypeSafe 認証エラー時

`TypeSafeAuthenticationError` が出た場合は `TYPESAFE_API_KEY` が無効。[console.typesafe.ai](https://console.typesafe.ai/) でキーを再発行して `.env` を更新する。Jev の判定が全件失敗すると全メールが Claude 再判定に回るため、Discord 通知の統計で `escalated` が総数と一致していたらこれを疑う。

#### Claude CLI 認証エラー時

`Claude CLI 認証エラー: Invalid authentication credentials.` が出た場合は、Claude Code CLI 側の認証が切れているか、`ANTHROPIC_API_KEY` が無効。Claude は重要メールの要約と低信頼メールの再判定にしか使われないため、分類自体は継続する。

```bash
claude auth login
claude auth status
```

API キー運用の場合は `ANTHROPIC_API_KEY` の値を確認する。再認証後に `uv run gmail-triage --dry-run` で再実行する。

### 6. cron 登録

```bash
crontab -e
```

```crontab
0 7 * * * cd /path/to/gmail-triage && /path/to/uv run gmail-triage >> logs/cron.log 2>&1
```

## 使い方

```bash
# 通常実行（直近24時間の未読メール）
uv run gmail-triage

# ドライラン（削除せずプレビュー）
uv run gmail-triage --dry-run

# 直近48時間を対象
uv run gmail-triage --hours 48

# 全メール対象（未読以外も含む、手動実行用）
uv run gmail-triage --all

# 全メール + ドライラン
uv run gmail-triage --all --dry-run

# バッチサイズ指定（デフォルト20）
uv run gmail-triage --all --batch 10
```

### --all モード

受信トレイの全メールを対象にする。バッチ単位（デフォルト20件）でメタデータ取得→分類→アクション実行を繰り返す。`--hours` を指定しなければ時間制限なし。バッチごとに Discord 通知が送信され、全バッチ完了後に合計サマリーも通知される。

## ログの読み方

1 通ごとの判定は `判定 / 根拠 / 操作 / 差出人 / 件名` の表で出る。
末尾に対象・クエリ・件数・所要時間をまとめたサマリが付く。

```
  判定        根拠                      操作        差出人               件名
  ──────────  ────────────────────────  ──────────  ────────────────────  ────────────────────────────────────────
  important   Claude再判定: 期限あり    ラベルのみ  楽天カード株式会社    ご利用明細確定のお知らせ
  delete      Jev 1.00                  ゴミ箱へ    Quora                 Quoraダイジェスト
```

桁揃えは文字数ではなく表示幅で計算する (全角 2 桁 / 結合文字 0 桁)。
`…` や罫線は East Asian Width が Ambiguous で端末により幅が変わるため、1 桁として扱うと決め打ちしている。

色は標準出力が TTY のときだけ付く。cron のリダイレクト先や `NO_COLOR` 設定時はプレーンな文字列になる。

## 分類ルールのカスタマイズ

`triage_questions.json` の `criteria` を編集する。各カテゴリは以下の 3 フィールドで説明する。

| フィールド | 役割 |
|---|---|
| `what` | そのカテゴリが何を指すか |
| `not_for` | 紛らわしいが別カテゴリに属するもの |
| `examples` | 代表的な入力例 |

`not_for` と `examples` は取り違えやすいカテゴリの分離に効くので、誤分類を見つけたら該当カテゴリの `examples` に実例を足すのが最も手軽な調整方法。

変更後は `--dry-run` で確認してからデプロイ。Claude 再判定側の基準は `SKILL.md` にあるので、大きくルールを変えたら両方を揃える。

`examples` を 2 行足しただけでエスカレーション率が 22.5% → 12.5% に下がり、判定ラベルは 1 件も変わらなかった実績がある。閾値を動かすより先に `examples` を試すとよい。

閾値は `.env` の `JEV_CONFIDENCE_THRESHOLD` / `JEV_DELETE_THRESHOLD` で調整する。誤削除が気になるなら `JEV_DELETE_THRESHOLD` を上げる (Claude 再判定に回る件数が増える)。

## テスト

```bash
uv run pytest
```
