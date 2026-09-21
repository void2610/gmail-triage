"""MIME ツリーからの本文抽出と、環境変数の読み取りタイミングを検証する"""

import base64

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


def test_本文長の上限は実行時に環境変数から読まれる(monkeypatch):
    # import 時に読むと gmail_triage の load_dotenv() より先になり .env が無視される
    monkeypatch.setenv("MAX_BODY_CHARS", "3")
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


def test_並列数も実行時に読まれる(monkeypatch):
    monkeypatch.setenv("GMAIL_CONCURRENCY", "3")

    assert gmail_fetch._concurrency() == 3
