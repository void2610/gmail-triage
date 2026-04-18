#!/usr/bin/env python3
"""Gmail トリアージ自動化スクリプト

未読メールを Claude Code CLI で分類し、不要メールを削除・重要メールを Discord に通知する。
"""

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# .env 読み込み
load_dotenv()

# 定数
SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]
BASE_DIR = Path(__file__).resolve().parent
CREDENTIALS_FILE = BASE_DIR / "credentials.json"
TOKEN_FILE = BASE_DIR / "token.json"
SKILL_FILE = BASE_DIR / "SKILL.md"
LOG_DIR = BASE_DIR / "logs"


def setup_logging() -> logging.Logger:
    """ログ設定（stdout + ファイル出力）"""
    LOG_DIR.mkdir(exist_ok=True)
    log_file = LOG_DIR / f"triage_{datetime.now().strftime('%Y%m%d_%H%M')}.log"

    logger = logging.getLogger("gmail_triage")
    logger.setLevel(logging.INFO)

    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

    # ファイルハンドラ
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setFormatter(formatter)
    logger.addHandler(fh)

    # コンソールハンドラ
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(formatter)
    logger.addHandler(ch)

    return logger


def parse_args() -> argparse.Namespace:
    """コマンドライン引数をパース"""
    parser = argparse.ArgumentParser(description="Gmail トリアージ自動化")
    parser.add_argument("--dry-run", action="store_true", help="削除せずプレビューのみ")
    parser.add_argument("--hours", type=int, default=None, help="対象とする直近の時間数")
    parser.add_argument("--all", action="store_true", help="未読に限らず全メールを対象にする（手動実行用）")
    parser.add_argument("--batch", type=int, default=20, help="1バッチあたりの処理件数（--all 時に有効、デフォルト20）")
    return parser.parse_args()


def get_config(args: argparse.Namespace) -> dict:
    """環境変数とコマンドライン引数から設定を構築"""
    return {
        "discord_webhook_url": os.getenv("DISCORD_WEBHOOK_URL", ""),
        "hours_back": args.hours or int(os.getenv("HOURS_BACK", "24")),
        "max_emails": int(os.getenv("MAX_EMAILS", "50")),
        "dry_run": args.dry_run or os.getenv("DRY_RUN", "false").lower() == "true",
    }


def authenticate_gmail() -> Credentials:
    """Gmail API 認証（token.json の自動リフレッシュ対応）"""
    creds = None

    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    elif not creds or not creds.valid:
        if not CREDENTIALS_FILE.exists():
            raise FileNotFoundError(
                f"credentials.json が見つかりません: {CREDENTIALS_FILE}\n"
                "Google Cloud Console から OAuth クライアント ID をダウンロードしてください。"
            )
        flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), SCOPES)
        creds = flow.run_local_server(port=0)

    # トークンを保存
    TOKEN_FILE.write_text(creds.to_json())
    return creds


def _extract_email_fields(service, msg_info: dict) -> dict:
    """メール1通の必要フィールドを抽出"""
    msg = service.users().messages().get(userId="me", id=msg_info["id"], format="metadata",
                                          metadataHeaders=["From", "Subject", "Date"]).execute()
    headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
    return {
        "id": msg_info["id"],
        "from": headers.get("From", ""),
        "subject": headers.get("Subject", ""),
        "date": headers.get("Date", ""),
        "snippet": msg.get("snippet", ""),
    }


def fetch_unread_emails(service, hours_back: int, max_emails: int) -> list[dict]:
    """未読メールを取得し、必要なフィールドを抽出"""
    query = f"is:unread -is:starred newer_than:{hours_back}h"
    results = service.users().messages().list(userId="me", q=query, maxResults=max_emails).execute()

    messages = results.get("messages", [])
    if not messages:
        return []

    return [_extract_email_fields(service, m) for m in messages]


def fetch_all_message_ids(service, hours_back: int | None, logger: logging.Logger) -> list[dict]:
    """未読に限らず全メールのID一覧を取得（ページネーション対応、メタデータは取得しない）"""
    query = f"-is:starred newer_than:{hours_back}h" if hours_back else "-is:starred"
    all_messages = []
    page_token = None

    while True:
        results = service.users().messages().list(
            userId="me", q=query, maxResults=500, pageToken=page_token,
        ).execute()
        messages = results.get("messages", [])
        if messages:
            all_messages.extend(messages)
        page_token = results.get("nextPageToken")
        if not page_token:
            break

    logger.info(f"全メールID取得: {len(all_messages)}通")
    return all_messages


def fetch_email_batch(service, message_ids: list[dict], logger: logging.Logger) -> list[dict]:
    """メールID一覧からバッチ分のメタデータを取得"""
    return [_extract_email_fields(service, m) for m in message_ids]


def classify_emails(emails: list[dict], logger: logging.Logger) -> dict | None:
    """Claude Code CLI でメールを分類"""
    skill_content = SKILL_FILE.read_text(encoding="utf-8")
    input_json = json.dumps({"emails": emails}, ensure_ascii=False)

    try:
        claude_path = os.getenv("CLAUDE_PATH", "claude")
        result = subprocess.run(
            [claude_path, "--print", "--model", "sonnet", "--append-system-prompt", skill_content],
            input=input_json,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        logger.error("Claude CLI がタイムアウトしました")
        return None
    except FileNotFoundError:
        logger.error("claude コマンドが見つかりません。Claude Code CLI がインストールされているか確認してください。")
        return None

    if result.returncode != 0:
        logger.error(f"Claude CLI エラー: {result.stderr}")
        return None

    # JSON を抽出（```json ブロックの可能性を考慮）
    output = result.stdout.strip()
    json_match = re.search(r"```json\s*(.*?)\s*```", output, re.DOTALL)
    if json_match:
        output = json_match.group(1)

    try:
        return json.loads(output)
    except json.JSONDecodeError:
        logger.error(f"Claude の応答を JSON としてパースできません: {output[:200]}")
        return None


def classify_with_retry(emails: list[dict], logger: logging.Logger) -> dict:
    """分類をリトライ付きで実行。失敗時は全メール keep 扱い"""
    for attempt in range(2):
        result = classify_emails(emails, logger)
        if result and "results" in result:
            return result
        if attempt == 0:
            logger.warning("分類リトライ中...")

    # 全メール keep 扱い
    logger.warning("分類に失敗しました。全メールを keep 扱いにします。")
    return {
        "results": [{"id": e["id"], "action": "keep", "reason": "分類失敗", "summary": ""} for e in emails],
        "stats": {"total": len(emails), "important": 0, "review": 0, "keep": len(emails), "delete": 0},
    }


TRIAGE_LABELS = {
    "important": "triage/important",
    "keep": "triage/keep",
    "delete": "triage/delete",
}


def ensure_triage_labels(service, logger: logging.Logger) -> dict[str, str]:
    """トリアージ用ラベルを作成（既存なら再利用）し、action→ラベルIDのマッピングを返す"""
    existing = service.users().labels().list(userId="me").execute().get("labels", [])
    name_to_id = {l["name"]: l["id"] for l in existing}

    action_to_label_id = {}
    for action, label_name in TRIAGE_LABELS.items():
        if label_name in name_to_id:
            action_to_label_id[action] = name_to_id[label_name]
        else:
            created = service.users().labels().create(userId="me", body={
                "name": label_name,
                "labelListVisibility": "labelShow",
                "messageListVisibility": "show",
            }).execute()
            action_to_label_id[action] = created["id"]
            logger.info(f"ラベル作成: {label_name}")

    return action_to_label_id


def execute_actions(service, classification: dict, dry_run: bool, label_ids: dict[str, str],
                    logger: logging.Logger) -> None:
    """分類結果に基づきアクションを実行（ラベル付与 + delete → ゴミ箱移動）"""
    delete_count = 0
    for item in classification["results"]:
        action = item["action"]
        msg_id = item["id"]

        # ラベル付与
        if action in label_ids:
            if dry_run:
                logger.info(f"[DRY RUN] ラベル付与スキップ: {msg_id} → {TRIAGE_LABELS[action]} ({item.get('reason', '')})")
            else:
                try:
                    service.users().messages().modify(userId="me", id=msg_id, body={
                        "addLabelIds": [label_ids[action]],
                    }).execute()
                except Exception as e:
                    logger.error(f"ラベル付与失敗 ({msg_id}): {e}")

        # delete はゴミ箱に移動
        if action == "delete":
            if dry_run:
                logger.info(f"[DRY RUN] ゴミ箱移動スキップ: {msg_id} ({item.get('reason', '')})")
            else:
                try:
                    service.users().messages().trash(userId="me", id=msg_id).execute()
                    delete_count += 1
                except Exception as e:
                    logger.error(f"ゴミ箱移動失敗 ({msg_id}): {e}")

    if not dry_run and delete_count > 0:
        logger.info(f"削除: {delete_count}通をゴミ箱に移動")


def send_discord_notification(classification: dict, config: dict, logger: logging.Logger, *, label: str = "") -> None:
    """Discord Webhook で重要・確認メールの要約を通知"""
    webhook_url = config["discord_webhook_url"]
    if not webhook_url:
        logger.warning("DISCORD_WEBHOOK_URL が未設定のため通知をスキップ")
        return

    stats = classification.get("stats", {})
    results = classification.get("results", [])

    # 重要メールを抽出
    important_items = [r for r in results if r["action"] == "important"]

    fields = []

    if important_items:
        value = "\n".join(f"• {item.get('summary', item.get('reason', ''))}" for item in important_items)
        fields.append({"name": f"🔴 重要 ({len(important_items)})", "value": value})

    stats_text = (
        f"重要: {stats.get('important', 0)} / "
        f"保留: {stats.get('keep', 0)} / "
        f"削除: {stats.get('delete', 0)}"
    )
    fields.append({"name": "📊 統計", "value": stats_text, "inline": True})

    payload = {
        "embeds": [{
            "title": f"📬 メールトリアージ{f' ({label})' if label else ''}",
            "color": 5814783,
            "description": f"**{stats.get('total', 0)}通**を処理しました",
            "fields": fields,
            "footer": {"text": f"gmail-triage • dry_run: {config['dry_run']}"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }]
    }

    try:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(webhook_url, data=data, headers={
            "Content-Type": "application/json",
            "User-Agent": "gmail-triage",
        })
        urllib.request.urlopen(req)
        logger.info("Discord通知: 送信完了")
    except Exception as e:
        logger.error(f"Discord通知失敗: {e}")


def send_error_notification(error_msg: str, config: dict) -> None:
    """エラー発生時に Discord へ通知"""
    webhook_url = config.get("discord_webhook_url", "")
    if not webhook_url:
        return

    payload = {
        "embeds": [{
            "title": "⚠️ Gmail トリアージ エラー",
            "color": 15158332,
            "description": f"```\n{error_msg}\n```",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }]
    }

    try:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(webhook_url, data=data, headers={
            "Content-Type": "application/json",
            "User-Agent": "gmail-triage",
        })
        urllib.request.urlopen(req)
    except Exception:
        pass  # エラー通知の失敗はログに記録済みなので無視


def _process_batch(service, emails: list[dict], config: dict, label_ids: dict[str, str],
                    logger: logging.Logger) -> dict:
    """メールのバッチを分類・アクション実行し、結果を返す"""
    classification = classify_with_retry(emails, logger)
    stats = classification.get("stats", {})
    logger.info(
        f"Claude 分類完了: important={stats.get('important', 0)}, "
        f"review={stats.get('review', 0)}, keep={stats.get('keep', 0)}, "
        f"delete={stats.get('delete', 0)}"
    )
    execute_actions(service, classification, config["dry_run"], label_ids, logger)
    return classification


def _merge_classifications(classifications: list[dict]) -> dict:
    """複数バッチの分類結果をマージ"""
    merged_results = []
    merged_stats = {"total": 0, "important": 0, "review": 0, "keep": 0, "delete": 0}

    for c in classifications:
        merged_results.extend(c.get("results", []))
        for key in merged_stats:
            merged_stats[key] += c.get("stats", {}).get(key, 0)

    return {"results": merged_results, "stats": merged_stats}


def main() -> None:
    args = parse_args()
    config = get_config(args)
    logger = setup_logging()

    mode = "全メール" if args.all else "未読メール"
    hours_label = f"直近{config['hours_back']}時間の" if not args.all or args.hours else ""
    logger.info(f"開始: {hours_label}{mode}取得 (dry_run={config['dry_run']})")

    try:
        # Gmail API 認証
        creds = authenticate_gmail()
        service = build("gmail", "v1", credentials=creds)

        # トリアージ用ラベルを準備
        label_ids = ensure_triage_labels(service, logger)

        # メール取得・処理
        if args.all:
            # --all: ID一覧を先に取得し、バッチごとにメタデータ取得→分類→アクション
            hours = args.hours if args.hours else None
            message_ids = fetch_all_message_ids(service, hours, logger)

            if not message_ids:
                logger.info("対象メールなし。終了します。")
                return

            batch_size = args.batch
            total_batches = (len(message_ids) + batch_size - 1) // batch_size
            classifications = []
            for i in range(0, len(message_ids), batch_size):
                batch_ids = message_ids[i:i + batch_size]
                batch_num = i // batch_size + 1
                logger.info(f"バッチ {batch_num}/{total_batches}: メタデータ取得中 ({len(batch_ids)}通)")
                emails = fetch_email_batch(service, batch_ids, logger)
                result = _process_batch(service, emails, config, label_ids, logger)
                classifications.append(result)
                # バッチごとにDiscord通知
                send_discord_notification(result, config, logger, label=f"バッチ {batch_num}/{total_batches}")
        else:
            emails = fetch_unread_emails(service, config["hours_back"], config["max_emails"])
            logger.info(f"取得: {len(emails)}通")

            if not emails:
                logger.info("未読メールなし。終了します。")
                return

            classifications = [_process_batch(service, emails, config, label_ids, logger)]

        # Discord 通知（通常モードは1回、--all はバッチごとに送信済みなので合計のみ）
        if args.all and len(classifications) > 1:
            merged = _merge_classifications(classifications)
            send_discord_notification(merged, config, logger, label="合計")
        elif not args.all:
            send_discord_notification(classifications[0], config, logger)

        logger.info("完了")

    except Exception as e:
        logger.error(f"致命的エラー: {e}")
        send_error_notification(str(e), config)
        sys.exit(1)


if __name__ == "__main__":
    main()
