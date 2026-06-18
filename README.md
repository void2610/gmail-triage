# Gmail Triage

Claude Code CLI を使って Gmail のメールを自動分類し、不要メールをゴミ箱に移動、重要メールを Discord に通知する。

## 仕組み

```
cron (毎朝 7:00)
  └─ gmail_triage.py
       ├─ Gmail API: メール取得
       ├─ Claude CLI: SKILL.md のルールでメールを分類
       ├─ Gmail API: ラベル付与 + 不要メール → ゴミ箱
       └─ Discord Webhook: 重要メールの要約を投稿
```

### 分類カテゴリ

| カテゴリ | action | 処理 |
|---------|--------|------|
| 🔴 重要 | `important` | `triage/important` ラベル + Discord 通知 |
| 🟡 確認 | `review` | `triage/review` ラベル + Discord 通知 |
| ⚪ 保留 | `keep` | `triage/keep` ラベル |
| 🗑️ 不要 | `delete` | `triage/delete` ラベル + ゴミ箱に移動 |

分類ルールは `SKILL.md` を編集するだけで調整できる。Gmail 上でラベルによるフィルタで分類結果を確認可能。

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

### 3. 環境変数

```bash
cp .env.example .env
```

`.env` を編集:

```
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/XXXX/YYYY
HOURS_BACK=24
MAX_EMAILS=50
DRY_RUN=false
```

### 4. 初回認証

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

#### Claude CLI 認証エラー時

`Claude CLI 認証エラー: Invalid authentication credentials.` が出た場合は、Claude Code CLI 側の認証が切れているか、`ANTHROPIC_API_KEY` が無効。

```bash
claude auth login
claude auth status
```

API キー運用の場合は `ANTHROPIC_API_KEY` の値を確認する。再認証後に `uv run gmail-triage --dry-run` で再実行する。

### 5. cron 登録

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

## 分類ルールのカスタマイズ

`SKILL.md` を編集する。変更後は `--dry-run` で確認してからデプロイ。

例:
- 特定の送信者を常に `keep` にしたい → ホワイトリストセクションを追加
- 特定のニュースレターを残したい → keep ルールに追記
