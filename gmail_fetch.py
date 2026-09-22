"""Gmail API からのメール取得

本文は MIME ツリーを走査して平文化する。本文取得は1通ずつのAPI呼び出しになるため並列化する。
"""

import base64
import html
import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

_thread_local = threading.local()

# Jev に渡す本文の長さ。冒頭だけで判定できるうえ、長文をそのまま送ると分類が遅くなる
MAX_BODY_CHARS = 4000
CONCURRENCY = 8
# Gmail の list API が 1 ページで返せる上限
PAGE_SIZE = 500


def _hours_filter(hours_back: int | None) -> str:
    return f" newer_than:{hours_back}h" if hours_back else ""


def triage_query(hours_back: int | None) -> str:
    """トリアージ対象のクエリ。ログのサマリにも出すため公開する"""
    # in:inbox が無いと送信済み・アーカイブ済み・迷惑メール・ゴミ箱まで対象に入る
    return "in:inbox -is:starred" + _hours_filter(hours_back)


# 期間で絞らない: 古いメッセージにスターが付いたスレッドも守る必要がある
STARRED_QUERY = "in:inbox is:starred"


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
        "body": _extract_body(payload)[:MAX_BODY_CHARS],
    }


def _fetch_fields_parallel(creds: Credentials, message_ids: list[dict]) -> list[dict]:
    """本文取得は1通ずつのAPI呼び出しになるため並列化する"""
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        return list(
            pool.map(
                lambda m: _extract_email_fields(_gmail_service(creds), m), message_ids
            )
        )


def _list_messages(creds: Credentials, query: str) -> list[dict]:
    """list API をページネーションしながら辿る。返る要素は id と threadId だけ"""
    all_messages = []
    page_token = None

    while True:
        results = (
            _gmail_service(creds)
            .users()
            .messages()
            .list(userId="me", q=query, maxResults=PAGE_SIZE, pageToken=page_token)
            .execute()
        )
        all_messages.extend(results.get("messages", []))
        page_token = results.get("nextPageToken")
        if not page_token:
            break

    return all_messages


def starred_thread_ids(creds: Credentials) -> set[str]:
    """スター付きメッセージが属するスレッドの ID 集合"""
    return {m["threadId"] for m in _list_messages(creds, STARRED_QUERY)}


def fetch_message_ids(
    creds: Credentials, hours_back: int | None, logger: logging.Logger
) -> list[dict]:
    """対象メールのID一覧を取得（本文は取得しない）"""
    messages = _list_messages(creds, triage_query(hours_back))
    # スターはメッセージ単位に付くため、返信で増えた別の1通は -is:starred をすり抜ける。
    # ユーザーはスレッド単位で保護したつもりなので、スレッドごと対象から外す
    starred = starred_thread_ids(creds)
    kept = [m for m in messages if m["threadId"] not in starred]

    logger.info(f"対象ID取得: {len(kept)}通")
    return kept


def fetch_email_batch(creds: Credentials, message_ids: list[dict]) -> list[dict]:
    """メールID一覧からバッチ分のメールを取得"""
    return _fetch_fields_parallel(creds, message_ids)
