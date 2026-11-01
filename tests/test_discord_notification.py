"""Discord 通知が embed の上限に収まることを検証する"""

import json
import logging

import gmail_triage

logger = logging.getLogger("test")


def _send(monkeypatch, summaries: list[str]) -> dict:
    """important メールを並べて通知し、送られた payload を返す"""
    sent = {}

    def fake_urlopen(req):
        sent.update(json.loads(req.data))

    monkeypatch.setattr(gmail_triage.urllib.request, "urlopen", fake_urlopen)
    results = [
        {"id": str(i), "action": "important", "summary": s}
        for i, s in enumerate(summaries)
    ]
    gmail_triage.send_discord_notification(
        {"results": results, "stats": {"total": len(results)}},
        {"discord_webhook_url": "https://example.com/hook", "dry_run": True},
        logger,
    )
    return sent["embeds"][0]["fields"][0]["value"]


def test_重要メールが多くてもフィールドは上限に収まり溢れた件数が出る(monkeypatch):
    # 54 通 × 30 文字は 1024 文字を超え、Discord が 400 を返していた
    value = _send(monkeypatch, [f"{i:02d}" + "あ" * 28 for i in range(54)])

    assert len(value) <= gmail_triage.DISCORD_FIELD_VALUE_LIMIT
    shown = value.count("• ")
    assert value.endswith(f"…ほか {54 - shown} 件")


def test_上限に収まるなら全件をそのまま出す(monkeypatch):
    value = _send(monkeypatch, ["請求の期限", "面談の日程調整"])

    assert value == "• 請求の期限\n• 面談の日程調整"
