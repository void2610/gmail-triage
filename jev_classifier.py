"""Jev (TypeSafe AI System One) によるメール分類

Jev は文字列を生成せず、型付きの設問を 1 パスで評価して構造化された値だけを返す。
そのため分類は Jev が全件担当し、Claude は低信頼メールの再判定と要約にのみ使う。
"""

import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from typesafe_sdk import Choice, TypeSafeAPIError, TypeSafeClient

from claude_cli import run_json

BASE_DIR = Path(__file__).resolve().parent
QUESTIONS_FILE = BASE_DIR / "triage_questions.json"
SKILL_FILE = BASE_DIR / "SKILL.md"

QUESTION_KEY = "action"
VALID_ACTIONS = ("important", "keep", "delete")

# 判定を採用する confidence の下限。delete はゴミ箱送りで取り消しに手間がかかるため高く設定する
DEFAULT_THRESHOLD = float(os.getenv("JEV_CONFIDENCE_THRESHOLD", "0.5"))
DELETE_THRESHOLD = float(os.getenv("JEV_DELETE_THRESHOLD", "0.9"))


def _thresholds() -> dict[str, float]:
    return {
        "important": DEFAULT_THRESHOLD,
        "keep": DEFAULT_THRESHOLD,
        "delete": DELETE_THRESHOLD,
    }


def _build_question() -> Choice:
    """triage_questions.json から Choice 設問を構築"""
    spec = json.loads(QUESTIONS_FILE.read_text(encoding="utf-8"))
    return Choice(instructions=spec["instructions"], criteria=spec["criteria"])


def _email_state(email: dict) -> dict:
    """Jev に渡す state。ID は判定材料にならないので含めない"""
    return {
        "from": email.get("from", ""),
        "subject": email.get("subject", ""),
        "date": email.get("date", ""),
        "body": email.get("body") or email.get("snippet", ""),
    }


def _classify_one(client: TypeSafeClient, question: Choice, email: dict) -> dict:
    """メール1通を Jev で判定。失敗時は needs_escalation を立てる"""
    try:
        response = client.system_one(
            state=_email_state(email), questions={QUESTION_KEY: question}
        )
    except TypeSafeAPIError as e:
        return {
            "id": email["id"],
            "error": f"{type(e).__name__}: {e}",
            "needs_escalation": True,
        }

    answer = response.choices[QUESTION_KEY]
    below_threshold = answer.confidence < _thresholds()[answer.choice]
    return {
        "id": email["id"],
        "action": answer.choice,
        "confidence": answer.confidence,
        "probabilities": dict(answer.probabilities),
        "needs_escalation": below_threshold,
    }


def _escalate(emails: list[dict], logger: logging.Logger) -> dict[str, dict]:
    """低信頼メールを Claude で再判定し、メールID→判定結果 を返す"""
    response = run_json(
        SKILL_FILE.read_text(encoding="utf-8"), {"emails": emails}, logger
    )
    if not response or "results" not in response:
        logger.warning(
            "エスカレーションに失敗しました。対象メールは keep 扱いにします。"
        )
        return {}

    return {r["id"]: r for r in response["results"] if r.get("action") in VALID_ACTIONS}


def classify(emails: list[dict], logger: logging.Logger) -> dict:
    """メール一覧を分類する。Jev で並列判定し、低信頼分のみ Claude へ回す"""
    question = _build_question()
    concurrency = int(os.getenv("JEV_CONCURRENCY", "8"))

    with TypeSafeClient(model=os.getenv("TYPESAFE_MODEL", "jev-latest")) as client:
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            verdicts = list(
                pool.map(lambda e: _classify_one(client, question, e), emails)
            )

    by_id = {e["id"]: e for e in emails}
    escalated = [by_id[v["id"]] for v in verdicts if v["needs_escalation"]]
    if escalated:
        logger.info(f"低信頼のため Claude にエスカレーション: {len(escalated)}通")
        for v in verdicts:
            if v.get("error"):
                logger.warning(f"Jev 判定失敗 ({v['id']}): {v['error']}")
    claude_verdicts = _escalate(escalated, logger) if escalated else {}

    results = []
    for v in verdicts:
        if not v["needs_escalation"]:
            results.append(
                {
                    "id": v["id"],
                    "action": v["action"],
                    "reason": f"Jev {v['confidence']:.2f}",
                    "summary": "",
                }
            )
            continue

        fallback = claude_verdicts.get(v["id"])
        if fallback:
            results.append(
                {
                    "id": v["id"],
                    "action": fallback["action"],
                    "reason": f"Claude再判定: {fallback.get('reason', '')}",
                    "summary": "",
                }
            )
        else:
            # Jev も Claude も判断できなかったメールは消さずに残す
            results.append(
                {"id": v["id"], "action": "keep", "reason": "判定不能", "summary": ""}
            )

    stats = {"total": len(results)}
    for action in VALID_ACTIONS:
        stats[action] = sum(1 for r in results if r["action"] == action)
    stats["escalated"] = len(escalated)

    return {"results": results, "stats": stats}
