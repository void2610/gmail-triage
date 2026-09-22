"""MIME ツリーからの本文抽出と、対象メールの絞り込み条件を検証する"""

import base64
import logging

import gmail_fetch


def _part(mime: str, text: str, *, strip_padding: bool = False) -> dict:
    data = base64.urlsafe_b64encode(text.encode()).decode()
    if strip_padding:
        data = data.rstrip("=")
    return {"mimeType": mime, "body": {"data": data}}


def test_パディングを省略した_base64url_でもデコードできる():
    # RFC 4648 は base64url のパディング省略を許しており、Gmail が省いても落ちてはいけない
    assert (
        gmail_fetch._decode_part(_part("text/plain", "hi", strip_padding=True)) == "hi"
    )


def test_データを持たないパートは空文字になる():
    assert gmail_fetch._decode_part({"body": {}}) == ""


def test_text_plain_が_html_より優先される():
    payload = {
        "mimeType": "multipart/alternative",
        "parts": [
            _part("text/html", "<p>HTML 版</p>"),
            _part("text/plain", "プレーン版"),
        ],
    }

    assert gmail_fetch._extract_body(payload) == "プレーン版"


def test_text_plain_が無ければ_html_を平文化する():
    payload = {
        "mimeType": "multipart/alternative",
        "parts": [
            _part("text/html", "<style>p{color:red}</style><p>本文&amp;続き</p>")
        ],
    }

    assert gmail_fetch._extract_body(payload) == "本文&続き"


def test_入れ子の_multipart_からも本文を拾う():
    payload = {
        "mimeType": "multipart/mixed",
        "parts": [
            {
                "mimeType": "multipart/alternative",
                "parts": [_part("text/plain", "入れ子の本文")],
            }
        ],
    }

    assert gmail_fetch._extract_body(payload) == "入れ子の本文"


def test_本文は上限文字数で打ち切られる(monkeypatch):
    monkeypatch.setattr(gmail_fetch, "MAX_BODY_CHARS", 3)
    captured = {}

    class FakeService:
        def users(self):
            return self

        def messages(self):
            return self

        def get(self, **kw):
            captured.update(kw)
            return self

        def execute(self):
            return {
                "snippet": "s",
                "payload": {"mimeType": "text/plain", **_part("text/plain", "abcdefg")},
            }

    email = gmail_fetch._extract_email_fields(FakeService(), {"id": "1"})

    assert email["body"] == "abc"
    assert captured["format"] == "full"


def test_対象は受信トレイのスターなしに限られる():
    assert gmail_fetch.triage_query(None) == "in:inbox -is:starred"


def test_期間を指定するとクエリに期間条件が入る():
    assert gmail_fetch.triage_query(48) == "in:inbox -is:starred newer_than:48h"


class _FakeList:
    """q ごとに返すメッセージを差し替える messages().list のスタブ"""

    def __init__(self, by_query: dict[str, list[dict]]):
        self.by_query = by_query
        self.queries = []

    def users(self):
        return self

    def messages(self):
        return self

    def list(self, **kw):
        self.queries.append(kw["q"])
        self._result = {"messages": self.by_query.get(kw["q"], [])}
        return self

    def execute(self):
        return self._result


def test_スター付きスレッドの別メッセージも対象から外れる(monkeypatch, caplog):
    fake = _FakeList(
        {
            "in:inbox -is:starred": [
                {"id": "m1", "threadId": "t1"},
                {"id": "m2", "threadId": "t2"},
            ],
            # t1 の別の1通にスターが付いている
            "in:inbox is:starred": [{"id": "m9", "threadId": "t1"}],
        }
    )
    monkeypatch.setattr(gmail_fetch, "_gmail_service", lambda creds: fake)

    kept = gmail_fetch.fetch_message_ids(None, None, logging.getLogger())

    assert [m["id"] for m in kept] == ["m2"]


def test_スター付きスレッドを探すクエリは期間で絞らない(monkeypatch):
    fake = _FakeList({})
    monkeypatch.setattr(gmail_fetch, "_gmail_service", lambda creds: fake)

    gmail_fetch.fetch_message_ids(None, 24, logging.getLogger())

    assert fake.queries == [
        "in:inbox -is:starred newer_than:24h",
        "in:inbox is:starred",
    ]
