#!/usr/bin/env python3
"""Gmail トリアージ自動化スクリプト

未読メールを Jev (TypeSafe AI System One) で分類し、不要メールを削除・重要メールを Discord に通知する。
"""

import argparse
import base64
import html
import json
import logging
import os
import re
import sys
import threading
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

import jev_classifier
from claude_cli import summarize_important

# .env 読み込み
load_dotenv()

# 定数
SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]
BASE_DIR = Path(__file__).resolve().parent
CREDENTIALS_FILE = BASE_DIR / "credentials.json"
TOKEN_FILE = BASE_DIR / "token.json"
LOG_DIR = BASE_DIR / "logs"
MAX_BODY_CHARS = int(os.getenv("MAX_BODY_CHARS", "4000"))
GMAIL_CONCURRENCY = int(os.getenv("GMAIL_CONCURRENCY", "8"))

_thread_local = threading.local()


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
        try:
            creds.refresh(Request())
        except RefreshError as e:
            # invalid_grant は refresh token の失効・取り消しが主因。古い token.json を捨てて再認証に切り替える。
            if TOKEN_FILE.exists():
                TOKEN_FILE.unlink()
            creds = None

            if not sys.stdin.isatty() or not sys.stdout.isatty():
                raise RuntimeError(
                    "Google OAuth トークンが失効または取り消されています。"
                    f" {TOKEN_FILE.name} を削除したため、次回は手動で再認証が必要です。"
                    " ターミナルで `uv run gmail-triage --dry-run` を実行して再認証してください。"
                ) from e

    if not creds or not creds.valid:
        if not CREDENTIALS_FILE.exists():
            raise FileNotFoundError(
                f"credentials.json が見つかりません: {CREDENTIALS_FILE}\n"
                "Google Cloud Console から OAuth クライアント ID をダウンロードしてください。"
            )
        flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), SCOPES)
        creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")

    # トークンを保存
    TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
    return creds


def _gmail_service(creds: Credentials):
    """httplib2 がスレッドセーフでないため、service はスレッドごとに作る"""
    if not hasattr(_thread_local, "service"):
        _thread_local.service = build("gmail", "v1", credentials=creds)
    return _thread_local.service


def _decode_part(part: dict) -> str:
    """MIME パートの本文を base64url からデコード"""
    data = part.get("body", {}).get("data", "")
    if not data:
        return ""
    # base64url はパディング省略が許されており、省略されていると urlsafe_b64decode が例外を投げる
    padded = data + "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")


def _strip_html(markup: str) -> str:
    """HTML メールから本文テキストだけを取り出す"""
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", markup, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    return html.unescape(text)


def _extract_body(payload: dict) -> str:
    """MIME ツリーを走査して本文を抽出。text/plain を優先し、無ければ HTML を平文化する"""
    plain, markup = [], []

    def walk(part: dict) -> None:
        mime = part.get("mimeType", "")
        if mime == "text/plain":
            plain.append(_decode_part(part))
        elif mime == "text/html":
            markup.append(_decode_part(part))
        for child in part.get("parts", []):
            walk(child)

    walk(payload)
    text = "\n".join(plain) or _strip_html("\n".join(markup))
    return re.sub(r"[ \t\r\f\v]+", " ", text).strip()


def _extract_email_fields(service, msg_info: dict) -> dict:
    """メール1通の必要フィールドを抽出"""
    msg = service.users().messages().get(userId="me", id=msg_info["id"], format="full").execute()
    payload = msg.get("payload", {})
    headers = {h["name"]: h["value"] for h in payload.get("headers", [])}
    return {
        "id": msg_info["id"],
        "from": headers.get("From", ""),
        "subject": headers.get("Subject", ""),
        "date": headers.get("Date", ""),
        "snippet": msg.get("snippet", ""),
        "body": _extract_body(payload)[:MAX_BODY_CHARS],
    }


def _fetch_fields_parallel(creds: Credentials, message_ids: list[dict]) -> list[dict]:
    """本文取得は1通ずつのAPI呼び出しになるため並列化する"""
    with ThreadPoolExecutor(max_workers=GMAIL_CONCURRENCY) as pool:
        return list(pool.map(lambda m: _extract_email_fields(_gmail_service(creds), m), message_ids))


def fetch_unread_emails(creds: Credentials, service, hours_back: int, max_emails: int) -> list[dict]:
    """未読メールを取得し、必要なフィールドを抽出"""
    query = f"is:unread -is:starred newer_than:{hours_back}h"
    results = service.users().messages().list(userId="me", q=query, maxResults=max_emails).execute()

    messages = results.get("messages", [])
    if not messages:
        return []

    return _fetch_fields_parallel(creds, messages)


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


def fetch_email_batch(creds: Credentials, message_ids: list[dict], logger: logging.Logger) -> list[dict]:
    """メールID一覧からバッチ分のメールを取得"""
    return _fetch_fields_parallel(creds, message_ids)


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


def _attach_summaries(classification: dict, emails: list[dict], logger: logging.Logger) -> None:
    """important メールの要約を Claude で生成して結果に埋める（Jev は文字列を生成できない）"""
    by_id = {e["id"]: e for e in emails}
    important = [r for r in classification["results"] if r["action"] == "important"]
    if not important:
        return

    summaries = summarize_important([by_id[r["id"]] for r in important], logger)
    for item in important:
        item["summary"] = summaries.get(item["id"]) or by_id[item["id"]].get("subject", "")


def _process_batch(service, emails: list[dict], config: dict, label_ids: dict[str, str],
                    logger: logging.Logger) -> dict:
    """メールのバッチを分類・アクション実行し、結果を返す"""
    classification = jev_classifier.classify(emails, logger)
    stats = classification.get("stats", {})
    logger.info(
        f"分類完了: important={stats.get('important', 0)}, "
        f"keep={stats.get('keep', 0)}, delete={stats.get('delete', 0)} "
        f"(Claude エスカレーション {stats.get('escalated', 0)}通)"
    )
    _attach_summaries(classification, emails, logger)
    execute_actions(service, classification, config["dry_run"], label_ids, logger)
    return classification


def _merge_classifications(classifications: list[dict]) -> dict:
    """複数バッチの分類結果をマージ"""
    merged_results = []
    merged_stats = {"total": 0, "important": 0, "keep": 0, "delete": 0, "escalated": 0}

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
                logger.info(f"バッチ {batch_num}/{total_batches}: メール取得中 ({len(batch_ids)}通)")
                emails = fetch_email_batch(creds, batch_ids, logger)
                result = _process_batch(service, emails, config, label_ids, logger)
                classifications.append(result)
                # バッチごとにDiscord通知
                send_discord_notification(result, config, logger, label=f"バッチ {batch_num}/{total_batches}")
        else:
            emails = fetch_unread_emails(creds, service, config["hours_back"], config["max_emails"])
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
