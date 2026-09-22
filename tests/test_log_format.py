"""全角混在時の桁揃えと、TTY 判定を検証する"""

import log_format


def test_全角は2桁_半角は1桁として数える():
    assert log_format.display_width("abc") == 3
    assert log_format.display_width("あいう") == 6
    assert log_format.display_width("a あ") == 4


def test_結合文字は桁に数えない():
    # 濁点が独立した結合文字として来ても見た目の幅は変わらない
    assert log_format.display_width("が") == 2


def test_短い文字列は右を空白で埋める():
    assert log_format.fit("ab", 5) == "ab   "
    assert log_format.display_width(log_format.fit("あ", 5)) == 5


def test_長い文字列は省略記号付きで切り詰める():
    fitted = log_format.fit("あいうえおかきくけこ", 6)

    assert "…" in fitted
    assert log_format.display_width(fitted) == 6


def test_曖昧幅の文字は1桁として扱う():
    # … と罫線は East Asian Width が Ambiguous。端末設定次第で2桁になりうるため前提を固定する
    assert log_format.display_width("…") == 1
    assert log_format.display_width("─") == 1


def test_全角と半角が混ざっても表示幅が揃う():
    widths = {
        log_format.display_width(log_format.fit(text, 12))
        for text in ["Quoraダイジェスト", "Naoki Shirahama", "楽天カード株式会社", ""]
    }

    assert widths == {12}


def test_差出人は表示名を優先しアドレスへ退避する():
    assert (
        log_format.sender_name('"楽天カード株式会社" <info@example.com>')
        == "楽天カード株式会社"
    )
    assert log_format.sender_name("info@example.com") == "info@example.com"
    assert log_format.sender_name("") == "(不明)"


def test_長い件名でも罫線の幅を超えない():
    long_subject = "あ" * 60
    rows = [
        (
            {"action": "delete", "reason": "Jev 1.00"},
            {"from": "Quora <a@b.c>", "subject": long_subject},
            "ゴミ箱へ",
        ),
        (
            {"action": "important", "reason": "Claude再判定: 期限" * 5},
            {"from": "", "subject": long_subject},
            "ラベルのみ",
        ),
    ]

    header, rule, *body = log_format.verdict_table(rows, colorize=False).split("\n")[1:]
    limit = log_format.display_width(rule)

    assert log_format.display_width(header) <= limit
    assert all(log_format.display_width(line) <= limit for line in body)


def test_最終列が短くても手前の列は桁が揃う():
    rows = [
        (
            {"action": "delete", "reason": "Jev 1.00"},
            {"from": "Quora <a@b.c>", "subject": "短い"},
            "ゴミ箱へ",
        ),
        (
            {"action": "keep", "reason": "Jev 0.69"},
            {"from": "楽天カード株式会社", "subject": "短い"},
            "ラベルのみ",
        ),
    ]

    lines = log_format.verdict_table(rows, colorize=False).split("\n")[3:]
    starts = {log_format.display_width(line[: line.index("短い")]) for line in lines}

    assert len(starts) == 1


def test_色付けは_TTY_以外では行わない():
    class NotATty:
        def isatty(self):
            return False

    assert log_format.supports_color(NotATty()) is False


def test_NO_COLOR_が設定されていれば色を付けない(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")

    class Tty:
        def isatty(self):
            return True

    assert log_format.supports_color(Tty()) is False


def test_色付けすると判定にエスケープが入る():
    row = log_format.verdict_table(
        [({"action": "delete", "reason": ""}, {"from": "", "subject": ""}, "")],
        colorize=True,
    )

    assert "\033[0;90m" in row and "\033[0m" in row
