"""Gmail API からのメール取得

本文は MIME ツリーを走査して平文化する。本文取得は1通ずつのAPI呼び出しになるため並列化する。
"""

import base64
import html
import logging
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

_thread_local = threading.local()


def _max_body_chars() -> int:
    """import 時に読むと呼び出し側の load_dotenv() より先になるため実行時に読む"""
    return int(os.getenv("MAX_BODY_CHARS", "4000"))


def _concurrency() -> int:
    return int(os.getenv("GMAIL_CONCURRENCY", "8"))


def unread_query(hours_back: int) -> str:
    """未読メール取得のクエリ。ログのサマリにも出すため公開する"""
    return f"is:unread -is:starred newer_than:{hours_back}h"


def all_query(hours_back: int | None) -> str:
    """全メール取得のクエリ。ログのサマリにも出すため公開する"""
    return f"-is:starred newer_than:{hours_back}h" if hours_back else "-is:starred"


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
    text = re.sub(
        r"<(script|style)[^>]*>.*?</\1>", " ", markup, flags=re.DOTALL | re.IGNORECASE
    )
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
    msg = (
        service.users()
        .messages()
        .get(userId="me", id=msg_info["id"], format="full")
        .execute()
    )
    payload = msg.get("payload", {})
    headers = {h["name"]: h["value"] for h in payload.get("headers", [])}
    return {
        "id": msg_info["id"],
        "from": headers.get("From", ""),
        "subject": headers.get("Subject", ""),
        "date": headers.get("Date", ""),
        "snippet": msg.get("snippet", ""),
        "body": _extract_body(payload)[: _max_body_chars()],
    }


def _fetch_fields_parallel(creds: Credentials, message_ids: list[dict]) -> list[dict]:
    """本文取得は1通ずつのAPI呼び出しになるため並列化する"""
    with ThreadPoolExecutor(max_workers=_concurrency()) as pool:
        return list(
            pool.map(
                lambda m: _extract_email_fields(_gmail_service(creds), m), message_ids
            )
        )


def fetch_unread_emails(
    creds: Credentials, hours_back: int, max_emails: int
) -> list[dict]:
    """未読メールを取得し、必要なフィールドを抽出"""
    query = unread_query(hours_back)
    results = (
        _gmail_service(creds)
        .users()
        .messages()
        .list(userId="me", q=query, maxResults=max_emails)
        .execute()
    )

    messages = results.get("messages", [])
    if not messages:
        return []

    return _fetch_fields_parallel(creds, messages)


def fetch_all_message_ids(
    creds: Credentials, hours_back: int | None, logger: logging.Logger
) -> list[dict]:
    """未読に限らず全メールのID一覧を取得（ページネーション対応、本文は取得しない）"""
    query = all_query(hours_back)
    all_messages = []
    page_token = None

    while True:
        results = (
            _gmail_service(creds)
            .users()
            .messages()
            .list(userId="me", q=query, maxResults=500, pageToken=page_token)
            .execute()
        )
        messages = results.get("messages", [])
        if messages:
            all_messages.extend(messages)
        page_token = results.get("nextPageToken")
        if not page_token:
            break

    logger.info(f"全メールID取得: {len(all_messages)}通")
    return all_messages


def fetch_email_batch(creds: Credentials, message_ids: list[dict]) -> list[dict]:
    """メールID一覧からバッチ分のメールを取得"""
    return _fetch_fields_parallel(creds, message_ids)
