#!/usr/bin/env python3
"""Gmail トリアージ自動化スクリプト

未読メールを Jev (TypeSafe AI System One) で分類し、不要メールを削除・重要メールを Discord に通知する。
"""

import argparse
import json
import logging
import time
import os
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

import gmail_fetch
import jev_classifier
import log_format
from claude_cli import summarize_important

# .env 読み込み
load_dotenv()

# 定数
SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]
BASE_DIR = Path(__file__).resolve().parent
CREDENTIALS_FILE = BASE_DIR / "credentials.json"
TOKEN_FILE = BASE_DIR / "token.json"
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
    parser.add_argument("--hours", type=int, default=None, help="対象とする直近の時間数（省略時は期間で絞らない）")
    parser.add_argument("--batch", type=int, default=20, help="1バッチあたりの処理件数（デフォルト20）")
    return parser.parse_args()


def get_config(args: argparse.Namespace) -> dict:
    """コマンドライン引数から設定を構築。環境変数は認証情報のみに使う"""
    return {
        "discord_webhook_url": os.getenv("DISCORD_WEBHOOK_URL", ""),
        "hours_back": args.hours,
        "dry_run": args.dry_run,
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
                    logger: logging.Logger) -> dict[str, str]:
    """分類結果に基づきアクションを実行し、メールID→操作内容 を返す"""
    operations = {}
    for item in classification["results"]:
        action = item["action"]
        msg_id = item["id"]
        done = []

        # ラベル付与
        if action in label_ids:
            if dry_run:
                done.append(TRIAGE_LABELS[action])
            else:
                try:
                    service.users().messages().modify(userId="me", id=msg_id, body={
                        "addLabelIds": [label_ids[action]],
                    }).execute()
                    done.append(TRIAGE_LABELS[action])
                except Exception as e:
                    logger.error(f"ラベル付与失敗 ({msg_id}): {e}")
                    done.append("ラベル失敗")

        # delete はゴミ箱に移動
        if action == "delete":
            if dry_run:
                done.append("ゴミ箱")
            else:
                try:
                    service.users().messages().trash(userId="me", id=msg_id).execute()
                    done.append("ゴミ箱")
                except Exception as e:
                    logger.error(f"ゴミ箱移動失敗 ({msg_id}): {e}")
                    done.append("削除失敗")

        operations[msg_id] = "ゴミ箱へ" if "ゴミ箱" in done else ("ラベルのみ" if done else "なし")

    return operations


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


def _log_verdict_table(classification: dict, emails: list[dict], operations: dict[str, str],
                       logger: logging.Logger) -> None:
    """1通ごとの判定を桁揃えした表で出す"""
    by_id = {e["id"]: e for e in emails}
    colorize = log_format.supports_color()

    # 重要なものから順に並べ、同じ判定の中では確信度が低い順にする（要確認のものが上に来る）
    order = {"important": 0, "keep": 1, "delete": 2}
    rows = sorted(classification["results"], key=lambda r: (order.get(r["action"], 9), r.get("reason", "")))

    logger.info(
        log_format.verdict_table(
            [(item, by_id.get(item["id"], {}), operations.get(item["id"], "なし")) for item in rows],
            colorize=colorize,
        )
    )


def _process_batch(service, emails: list[dict], config: dict, label_ids: dict[str, str],
                    logger: logging.Logger, timings: dict) -> dict:
    """メールのバッチを分類・アクション実行し、結果を返す"""
    t0 = time.monotonic()
    classification = jev_classifier.classify(emails, logger)
    _attach_summaries(classification, emails, logger)
    timings["classify"] = timings.get("classify", 0) + time.monotonic() - t0

    operations = execute_actions(service, classification, config["dry_run"], label_ids, logger)
    _log_verdict_table(classification, emails, operations, logger)
    classification["operations"] = operations
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

    target = f"直近{args.hours}時間のメール" if args.hours else "全期間のメール"
    logger.info(f"開始: {target}取得 (dry_run={config['dry_run']})")

    started = time.monotonic()
    timings: dict[str, float] = {}
    query = ""

    try:
        # Gmail API 認証
        creds = authenticate_gmail()
        service = build("gmail", "v1", credentials=creds)

        # トリアージ用ラベルを準備
        label_ids = ensure_triage_labels(service, logger)

        # ID一覧を先に取得し、バッチごとにメタデータ取得→分類→アクション
        query = gmail_fetch.triage_query(args.hours)
        t0 = time.monotonic()
        message_ids = gmail_fetch.fetch_message_ids(creds, args.hours, logger)

        if not message_ids:
            logger.info("対象メールなし。終了します。")
            return

        batch_size = args.batch
        total_batches = (len(message_ids) + batch_size - 1) // batch_size
        classifications = []
        for i in range(0, len(message_ids), batch_size):
            batch_ids = message_ids[i : i + batch_size]
            batch_num = i // batch_size + 1
            logger.info(f"バッチ {batch_num}/{total_batches}: メール取得中 ({len(batch_ids)}通)")
            emails = gmail_fetch.fetch_email_batch(creds, batch_ids)
            timings["fetch"] = time.monotonic() - t0 - timings.get("classify", 0)
            result = _process_batch(service, emails, config, label_ids, logger, timings)
            classifications.append(result)
            send_discord_notification(result, config, logger, label=f"バッチ {batch_num}/{total_batches}")

        # バッチごとに送信済みなので、複数バッチのときだけ合計を送る
        if len(classifications) > 1:
            send_discord_notification(_merge_classifications(classifications), config, logger, label="合計")

        merged = _merge_classifications(classifications)
        operations = {}
        for c in classifications:
            operations.update(c.get("operations", {}))
        timings["total"] = time.monotonic() - started
        logger.info(
            "\n"
            + log_format.summary(
                target=target,
                query=query,
                stats=merged["stats"],
                operations={
                    "trashed": sum(1 for v in operations.values() if v == "ゴミ箱へ"),
                    "labeled": sum(1 for v in operations.values() if v == "ラベルのみ"),
                },
                dry_run=config["dry_run"],
                timings=timings,
            )
        )

    except Exception as e:
        logger.error(f"致命的エラー: {e}")
        send_error_notification(str(e), config)
        sys.exit(1)


if __name__ == "__main__":
    main()
