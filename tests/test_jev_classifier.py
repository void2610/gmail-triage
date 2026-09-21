"""confidence ゲーティングとエスカレーションの挙動を検証する"""

import logging

import httpx2
import pytest
from typesafe_sdk import TypeSafeAPIError

import jev_classifier

logger = logging.getLogger("test")


class FakeChoiceAnswer:
    def __init__(self, choice: str, confidence: float):
        self.choice = choice
        self.confidence = confidence
        self.probabilities = {choice: confidence}


class FakeResponse:
    def __init__(self, answer: FakeChoiceAnswer):
        self.choices = {jev_classifier.QUESTION_KEY: answer}


class FakeClient:
    """state["subject"] をキーに verdicts から応答を引く"""

    def __init__(self, verdicts: dict):
        self.verdicts = verdicts

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def system_one(self, state, questions):
        verdict = self.verdicts[state["subject"]]
        if isinstance(verdict, Exception):
            raise verdict
        return FakeResponse(FakeChoiceAnswer(*verdict))


def _email(msg_id: str, subject: str) -> dict:
    return {
        "id": msg_id,
        "from": "a@example.com",
        "subject": subject,
        "date": "",
        "snippet": "",
    }


@pytest.fixture
def patched(monkeypatch):
    """Jev クライアントと Claude エスカレーションを差し替えるためのファクトリ"""

    def _apply(verdicts: dict, escalation: dict | None):
        monkeypatch.setattr(
            jev_classifier, "TypeSafeClient", lambda **kw: FakeClient(verdicts)
        )
        calls = []

        def fake_run_json(system_prompt, payload, log, **kw):
            calls.append(payload["emails"])
            return escalation

        monkeypatch.setattr(jev_classifier, "run_json", fake_run_json)
        return calls

    return _apply


def test_高信頼な判定はそのまま採用される(patched):
    patched({"広告": ("delete", 0.99), "依頼": ("important", 0.8)}, None)

    result = jev_classifier.classify([_email("1", "広告"), _email("2", "依頼")], logger)

    actions = {r["id"]: r["action"] for r in result["results"]}
    assert actions == {"1": "delete", "2": "important"}
    assert result["results"][0]["reason"] == "Jev 0.99"
    assert result["stats"] == {
        "total": 2,
        "important": 1,
        "keep": 0,
        "delete": 1,
        "escalated": 0,
    }


def test_delete_は閾値が高く中程度の確信ではエスカレーションされる(patched):
    # important なら 0.8 で採用されるが、delete は 0.9 未満なので再判定に回る
    calls = patched(
        {"微妙": ("delete", 0.8)},
        {"results": [{"id": "1", "action": "keep", "reason": "人間から"}]},
    )

    result = jev_classifier.classify([_email("1", "微妙")], logger)

    assert [e["id"] for e in calls[0]] == ["1"]
    assert result["results"][0]["action"] == "keep"
    assert result["results"][0]["reason"] == "Claude再判定: 人間から"
    assert result["stats"]["escalated"] == 1


def test_Jev_の_API_エラーはエスカレーションに回る(patched):
    calls = patched(
        {"壊れた": TypeSafeAPIError(500, None, httpx2.Headers(), message="boom")},
        {"results": [{"id": "1", "action": "delete", "reason": "広告"}]},
    )

    result = jev_classifier.classify([_email("1", "壊れた")], logger)

    assert [e["id"] for e in calls[0]] == ["1"]
    assert result["results"][0]["action"] == "delete"


def test_エスカレーションも失敗したメールは消さずに残す(patched):
    patched({"微妙": ("delete", 0.5)}, None)

    result = jev_classifier.classify([_email("1", "微妙")], logger)

    assert result["results"][0] == {
        "id": "1",
        "action": "keep",
        "reason": "判定不能",
        "summary": "",
    }


def test_エスカレーション対象外のメールは_Claude_に送られない(patched):
    calls = patched(
        {"広告": ("delete", 0.99), "微妙": ("delete", 0.6)},
        {"results": [{"id": "2", "action": "keep", "reason": "不明"}]},
    )

    jev_classifier.classify([_email("1", "広告"), _email("2", "微妙")], logger)

    assert [e["id"] for e in calls[0]] == ["2"]
