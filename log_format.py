"""トリアージ結果のログ整形

件名や差出人に全角文字が混ざるため、桁揃えは文字数ではなく表示幅で計算する。
色は TTY のときだけ付け、ログファイルには常にプレーンな文字列を残す。
"""

import os
import sys
import unicodedata
from email.utils import parseaddr

RESET = "\033[0m"
ACTION_COLORS = {
    "important": "\033[1;31m",
    "keep": "\033[0;33m",
    "delete": "\033[0;90m",
}

COLUMNS = (("判定", 10), ("根拠", 24), ("操作", 10), ("差出人", 20), ("件名", 40))
INDENT = "  "


def supports_color(stream=None) -> bool:
    """cron のリダイレクト先に ANSI エスケープを混ぜないよう TTY のときだけ色を使う"""
    stream = stream or sys.stdout
    if os.getenv("NO_COLOR"):
        return False
    return hasattr(stream, "isatty") and stream.isatty()


def display_width(text: str) -> int:
    """全角を2、結合文字を0として数えた表示幅"""
    width = 0
    for ch in text:
        if unicodedata.combining(ch):
            continue
        width += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return width


def fit(text: str, width: int) -> str:
    """表示幅が width になるよう切り詰め、足りなければ右を空白で埋める"""
    text = text.replace("\n", " ").strip()
    if display_width(text) <= width:
        return text + " " * (width - display_width(text))

    out = ""
    for ch in text:
        if display_width(out + ch) > width - 1:
            break
        out += ch
    return out + "…" + " " * (width - display_width(out) - 1)


def sender_name(raw: str) -> str:
    """差出人ヘッダから表示名を取り出す。表示名が無ければアドレスのまま"""
    name, addr = parseaddr(raw)
    return name or addr or "(不明)"


def _action_label(action: str, colorize: bool) -> str:
    label = fit(action, COLUMNS[0][1])
    if colorize and action in ACTION_COLORS:
        return f"{ACTION_COLORS[action]}{label}{RESET}"
    return label


def _row(item: dict, email: dict, operation: str, *, colorize: bool) -> str:
    """1通分の判定を桁揃えして1行にする"""
    cells = [
        _action_label(item["action"], colorize),
        fit(item.get("reason", ""), COLUMNS[1][1]),
        fit(operation, COLUMNS[2][1]),
        fit(sender_name(email.get("from", "")), COLUMNS[3][1]),
        fit(email.get("subject", "") or "(件名なし)", COLUMNS[4][1]),
    ]
    return INDENT + "  ".join(cells).rstrip()


def verdict_table(rows: list[tuple[dict, dict, str]], *, colorize: bool) -> str:
    """判定一覧を1つのログレコードにまとめる（行ごとに時刻が重複しないようにするため）"""
    titles = INDENT + "  ".join(fit(name, width) for name, width in COLUMNS).rstrip()
    rule = INDENT + "  ".join("─" * width for _, width in COLUMNS)
    body = [
        _row(item, email, operation, colorize=colorize)
        for item, email, operation in rows
    ]
    return "\n".join(["", titles, rule, *body])


def summary(
    *,
    target: str,
    query: str,
    stats: dict,
    operations: dict,
    dry_run: bool,
    timings: dict,
) -> str:
    """実行内容と所要時間をまとめた終了サマリ"""
    lines = [
        "── サマリ " + "─" * 60,
        f"  対象    : {target}",
        f"  クエリ  : {query}",
        f"  取得    : {stats.get('total', 0)}通 ({timings.get('fetch', 0):.1f}秒)",
        f"  分類    : important={stats.get('important', 0)} keep={stats.get('keep', 0)} "
        f"delete={stats.get('delete', 0)} / Claude再判定 {stats.get('escalated', 0)}通 "
        f"({timings.get('classify', 0):.1f}秒)",
        f"  操作    : ゴミ箱 {operations.get('trashed', 0)}通 / ラベルのみ {operations.get('labeled', 0)}通"
        + ("  ※ dry-run のため未実行" if dry_run else ""),
        f"  合計    : {timings.get('total', 0):.1f}秒",
        "─" * 70,
    ]
    return "\n".join(lines)
