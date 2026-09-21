"""Claude Code CLI 呼び出しの薄いラッパー

主分類は Jev が担うため、ここは低信頼メールのエスカレーションと
important メールの要約生成という 2 用途のみで使われる。
"""

import json
import logging
import os
import re
import subprocess

# Jev が文字列を生成できないため、要約だけは言語モデルに委ねる
SUMMARY_PROMPT = """あなたはメール要約アシスタントです。与えられた重要メール一覧について、各メールの要点を30文字以内で要約してください。

以下のJSON形式で応答してください。JSONのみを出力し、他のテキストは含めないでください。

```json
{
  "summaries": [
    {"id": "メールID", "summary": "内容要約（30文字以内）"}
  ]
}
```
"""


def run_json(
    system_prompt: str, payload: dict, logger: logging.Logger, *, timeout: int = 120
) -> dict | None:
    """Claude CLI を呼び出して JSON 応答をパースする。失敗時は None"""
    try:
        claude_path = os.getenv("CLAUDE_PATH", "claude")
        result = subprocess.run(
            [
                claude_path,
                "--print",
                "--model",
                "sonnet",
                "--append-system-prompt",
                system_prompt,
            ],
            input=json.dumps(payload, ensure_ascii=False),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        logger.error("Claude CLI がタイムアウトしました")
        return None
    except FileNotFoundError:
        logger.error(
            "claude コマンドが見つかりません。Claude Code CLI がインストールされているか確認してください。"
        )
        return None

    if result.returncode != 0:
        error_output = (result.stderr or result.stdout).strip()
        if "Invalid authentication credentials" in error_output:
            logger.error(
                "Claude CLI 認証エラー: Invalid authentication credentials. "
                "`claude auth login` または API キー設定を確認してください。"
            )
        logger.error(
            f"Claude CLI エラー: {error_output or f'returncode={result.returncode}'}"
        )
        return None

    output = result.stdout.strip()
    json_match = re.search(r"```json\s*(.*?)\s*```", output, re.DOTALL)
    if json_match:
        output = json_match.group(1)

    try:
        return json.loads(output)
    except json.JSONDecodeError:
        logger.error(f"Claude の応答を JSON としてパースできません: {output[:200]}")
        return None


def summarize_important(emails: list[dict], logger: logging.Logger) -> dict[str, str]:
    """important メールの要約を生成し、メールID→要約 のマッピングを返す"""
    if not emails:
        return {}

    response = run_json(SUMMARY_PROMPT, {"emails": emails}, logger)
    if not response or "summaries" not in response:
        logger.warning("要約生成に失敗しました。件名で代替します。")
        return {}

    return {s["id"]: s.get("summary", "") for s in response["summaries"] if "id" in s}
