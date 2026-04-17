# Gmail Triage

Claude Code CLI を使って Gmail の未読メールを自動分類し、不要メールをゴミ箱に移動、重要メールを Discord に通知する。

## 仕組み

```
cron (毎朝 7:00)
  └─ gmail_triage.py
       ├─ Gmail API: 未読メール取得
       ├─ Claude CLI: SKILL.md のルールでメールを分類
       ├─ Gmail API: 不要メール → ゴミ箱
       └─ Discord Webhook: 重要メールの要約を投稿
```

### 分類カテゴリ

| カテゴリ | action | 処理 |
|---------|--------|------|
| 🔴 重要 | `important` | Discord に通知 |
| 🟡 確認 | `review` | Discord に通知 |
| ⚪ 保留 | `keep` | 何もしない |
| 🗑️ 不要 | `delete` | ゴミ箱に移動 |

分類ルールは `SKILL.md` を編集するだけで調整できる。

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

### 5. cron 登録

```bash
crontab -e
```

```crontab
0 7 * * * cd /path/to/gmail-triage && /path/to/uv run gmail-triage >> logs/cron.log 2>&1
```

## 使い方

```bash
# 通常実行
uv run gmail-triage

# ドライラン（削除せずプレビュー）
uv run gmail-triage --dry-run

# 直近48時間を対象
uv run gmail-triage --hours 48

# 全メール対象（未読以外も含む、手動実行用）
uv run gmail-triage --all --dry-run

# バッチサイズ指定（デフォルト50）
uv run gmail-triage --all --batch 20
```

## 分類ルールのカスタマイズ

`SKILL.md` を編集する。変更後は `--dry-run` で確認してからデプロイ。

例:
- 特定の送信者を常に `keep` にしたい → ホワイトリストセクションを追加
- GitHub 通知を全部 `delete` にしたい → delete ルールに追記
