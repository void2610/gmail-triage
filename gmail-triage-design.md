# Gmail Triage Automation 設計書

## 概要

毎朝 Claude Code CLI を使って Gmail の未読メールを分類し、不要メールを削除・重要メールを Discord に通知する自動化システム。

SKILL.md でルールを管理することで、コード変更なしに分類ロジックを調整可能にする。

## アーキテクチャ

```
cron (毎朝 7:00)
  └─ gmail_triage.py
       ├─ Gmail API: 未読メール取得
       ├─ claude --print -p SKILL.md: メール分類（JSON応答）
       ├─ Gmail API: 不要メール → ゴミ箱
       └─ Discord Webhook: 重要メールの要約を投稿
```

**実行環境**: M1 MacBook Pro（常時稼働）

## ディレクトリ構成

```
gmail-triage/
├── gmail_triage.py       # メインスクリプト
├── SKILL.md              # Claude Code 用スキル（分類ルール）
├── config.json           # 設定ファイル（Webhook URL 等）
├── credentials.json      # Google OAuth クライアント（git管理外）
├── token.json            # OAuth トークン（git管理外、自動生成）
├── logs/                 # 実行ログ
│   └── triage_YYYYMMDD_HHMM.log
├── requirements.txt
├── .gitignore
└── README.md
```

## config.json

```json
{
  "discord_webhook_url": "https://discord.com/api/webhooks/XXXX/YYYY",
  "hours_back": 24,
  "max_emails": 50,
  "dry_run": false
}
```

- `hours_back`: 何時間前までのメールを対象にするか
- `max_emails`: 1回の実行で処理する最大件数
- `dry_run`: `true` にすると削除を実行せずログのみ出力

## SKILL.md（分類ルール）

Claude Code CLI に渡すスキルファイル。メール分類の全ロジックをここで管理する。

### 分類カテゴリ

| カテゴリ | action | 説明 |
|---------|--------|------|
| 🔴 重要 | `important` | 個人宛の返信必要メール、仕事関連の緊急連絡、請求・支払い期限 |
| 🟡 確認 | `review` | 返信不要だが把握すべきもの（サービス変更通知、セキュリティ通知） |
| ⚪ 保留 | `keep` | 残しておくが今は見なくていいもの（注文確認、レシート） |
| 🗑️ 不要 | `delete` | 広告、マーケティング、ニュースレター、不要な自動通知 |

### 判定ルールの要点

**delete**:
- マーケティング・プロモーション
- 購読していないニュースレター
- SNS の通知メール（いいね、フォロー等）
- クーポン・セール情報
- 「配信停止」リンクがあるメール

**important**:
- 送信者が個人名で具体的な依頼・質問を含む
- 仕事・業務関連の連絡
- 期限・締切が明記されている
- 金銭に関わる重要通知
- セキュリティアラート（不正ログイン等）

**review**:
- GitHub の PR レビュー依頼、Issue アサイン
- サービスの利用規約変更・料金改定
- アカウント関連の重要通知

**keep**:
- 注文確認・配送通知・レシート
- GitHub CI 通知
- カレンダー招待

**原則: 迷ったら `keep`（誤削除防止）**

### Claude への出力指示

JSON 形式で応答させる:

```json
{
  "results": [
    {
      "id": "メールID",
      "action": "important|review|keep|delete",
      "reason": "分類理由（15文字以内）",
      "summary": "内容要約（important/reviewのみ、30文字以内）"
    }
  ],
  "stats": {
    "total": 10,
    "important": 1,
    "review": 2,
    "keep": 3,
    "delete": 4
  }
}
```

## gmail_triage.py 処理フロー

### 1. 初期化

```
config.json 読み込み
ログ設定（stdout + ファイル）
コマンドライン引数パース（--dry-run, --hours）
```

### 2. Gmail API 認証

```
token.json が有効 → そのまま使用
期限切れ → refresh_token でリフレッシュ
token.json なし → credentials.json で OAuth フロー（初回のみブラウザ認可）
```

- スコープ: `gmail.modify`（読み取り + ゴミ箱移動に必要）
- ライブラリ: `google-api-python-client`, `google-auth-oauthlib`

### 3. 未読メール取得

```
Gmail API: users.messages.list
  query: "is:unread newer_than:{hours_back}h"
  maxResults: config.max_emails

各メールの取得フィールド:
  - id
  - From（送信者名 + アドレス）
  - Subject
  - Date
  - snippet（本文プレビュー、API が返す先頭約200文字）
```

snippet で十分な情報量が得られる。本文全体を取得すると token 消費が大きくなるため避ける。

### 4. Claude Code CLI で分類

```bash
echo '{メール一覧JSON}' | claude --print --model sonnet --append-system-prompt "$(cat SKILL.md)"
```

**入力フォーマット**（Claude に渡すJSON）:

```json
{
  "emails": [
    {
      "id": "18f1a2b3c4d5e6f7",
      "from": "田中太郎 <tanaka@example.com>",
      "subject": "来週の打ち合わせについて",
      "date": "2026-04-17T08:30:00+09:00",
      "snippet": "お疲れ様です。来週火曜の打ち合わせですが、14時からに変更可能でしょうか..."
    }
  ]
}
```

**Claude CLI 呼び出し**:
- `--print`: 非対話モードで stdout に結果を出力
- `--model sonnet`: コスト効率重視（Haiku でも可、精度を見て調整）
- `--append-system-prompt`: SKILL.md の内容をシステムプロンプトに追加
- stdin からメール一覧 JSON をパイプ

**応答パース**:
- stdout から JSON を抽出（```json ブロックの可能性があるので strip する）
- パース失敗時はリトライ1回、それでも失敗なら全メール `keep` 扱い

### 5. アクション実行

**delete 対象**:
```
Gmail API: users.messages.trash(id=メールID)
```
- 完全削除ではなくゴミ箱移動（30日後に自動削除、誤判定時に復旧可能）

**keep / review 対象**:
- 何もしない（受信トレイに残す）

### 6. Discord 通知

**通知対象**: `important` と `review` のメール

**Discord Webhook ペイロード**:

```json
{
  "embeds": [
    {
      "title": "📬 朝のメールトリアージ",
      "color": 5814783,
      "description": "**3通**を処理しました",
      "fields": [
        {
          "name": "🔴 重要 (1)",
          "value": "**来週の打ち合わせについて**\n田中太郎 - 14時への変更依頼"
        },
        {
          "name": "🟡 確認 (1)",
          "value": "**料金プラン改定のお知らせ**\nAWSから - 6月1日に料金変更"
        },
        {
          "name": "📊 統計",
          "value": "重要: 1 / 確認: 1 / 保留: 5 / 削除: 12",
          "inline": true
        }
      ],
      "footer": {
        "text": "gmail-triage • dry_run: false"
      },
      "timestamp": "2026-04-17T07:00:00+09:00"
    }
  ]
}
```

通知は `urllib.request` で POST する（外部依存なし）。

### 7. ログ出力

`logs/triage_YYYYMMDD_HHMM.log` に記録する。
1 通ごとの判定は `log_format.verdict_table` が桁揃えした表を 1 レコードとして出し
(行ごとに時刻が重複しないようにするため)、末尾に `log_format.summary` の終了サマリを付ける。

```
2026-04-17 07:00:01 [INFO] 開始: 直近24時間の未読メール取得 (dry_run=False)
2026-04-17 07:00:02 [INFO] 取得: 19通
2026-04-17 07:00:06 [INFO]
  判定        根拠                      操作        差出人               件名
  ──────────  ────────────────────────  ──────────  ────────────────────  ────────────────────
  important   Claude再判定: 期限あり    ラベルのみ  楽天カード株式会社    ご利用明細確定のお知らせ
  delete      Jev 1.00                  ゴミ箱へ    Quora                 Quoraダイジェスト
2026-04-17 07:00:07 [INFO]
── サマリ ────────────────────────────────────────────────────
  対象    : 直近24時間の未読メール
  クエリ  : is:unread -is:starred newer_than:24h
  取得    : 19通 (1.2秒)
  分類    : important=1 keep=6 delete=12 / Claude再判定 2通 (3.4秒)
  操作    : ゴミ箱 12通 / ラベルのみ 7通
  合計    : 6.1秒
```

桁揃えは表示幅ベース (全角 2 / 結合文字 0)。`…` と罫線は Ambiguous 幅のため 1 桁と決め打つ。
色は TTY のときだけ付けるので、cron 経由のログファイルには ANSI エスケープが混ざらない。

## cron 設定

```crontab
# 毎朝7時にメールトリアージ
0 7 * * * cd /path/to/gmail-triage && /usr/bin/python3 gmail_triage.py >> logs/cron.log 2>&1
```

- Claude Code CLI にパスが通っていることを確認（`which claude`）
- 必要に応じて `PATH` を crontab 内で明示する

## セットアップ手順

### 1. Google Cloud Console

1. [Google Cloud Console](https://console.cloud.google.com/) でプロジェクト作成
2. Gmail API を有効化
3. OAuth 同意画面を設定（テストユーザーに自分を追加）
4. OAuth クライアント ID を作成（デスクトップアプリ）
5. `credentials.json` をダウンロードしてプロジェクトルートに配置

### 2. 依存パッケージ

```
google-api-python-client
google-auth-oauthlib
```

### 3. 初回認証

```bash
python gmail_triage.py --dry-run
# ブラウザが開くので Google アカウントで認可
# token.json が自動生成される
```

### 4. Discord Webhook

1. Discord サーバーの通知用チャンネルで Webhook を作成
2. Webhook URL を `config.json` の `discord_webhook_url` に設定

### 5. cron 登録

```bash
crontab -e
# 上記の cron 設定を追加
```

## コマンドライン引数

```
python gmail_triage.py              # 通常実行
python gmail_triage.py --dry-run    # 削除せずプレビュー
python gmail_triage.py --hours 48   # 直近48時間を対象
```

## 運用メモ

### 分類精度の調整

SKILL.md を編集するだけで振る舞いを変更できる:

- 特定の送信者を常に `keep` にしたい → SKILL.md にホワイトリストセクションを追加
- GitHub 通知を全部 `delete` にしたい → delete ルールに追記
- 新しいカテゴリを追加したい → カテゴリテーブルと判定ルールを追加

変更後は `--dry-run` で期待通りか確認してからデプロイする。

### token.json の有効期限

- アクセストークンは自動リフレッシュされる
- リフレッシュトークンが失効した場合は `token.json` を削除して再認証

### ログのローテーション

古いログが溜まるので、定期的に削除するか logrotate を設定する:

```bash
# 30日以上前のログを削除（cron に追加）
0 0 1 * * find /path/to/gmail-triage/logs -name "*.log" -mtime +30 -delete
```

### エラー時の安全策

- Claude CLI の応答パースに失敗 → 全メール `keep` 扱い（何も削除しない）
- Gmail API エラー → Discord にエラー通知、処理中断
- Webhook 送信失敗 → ログに記録して続行（メール処理は完了させる）
