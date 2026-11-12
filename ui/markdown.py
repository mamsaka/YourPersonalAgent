"""Мини-разбор Markdown для окна чата. Чистая логика без Tkinter — легко тестируется.

parse(text) -> список (tag, text). tag — пустая строка или одно/несколько имён тегов через пробел:
"b", "i", "code", "codeblock", "h1", "h2", "h3", "li", "li2", "quote", "url" (например "li b").
"""
from __future__ import annotations

import re

FENCE_RE = re.compile(r"```[^\n`]*\n(.*?)(?:```|\Z)", re.S)
INLINE_RE = re.compile(
    r"(`[^`\n]+`"
    r"|\*\*[^*\n]+?\*\*"
    r"|(?<![*\w])\*[^*\s][^*\n]*?(?<!\s)\*(?![*\w])"
    r"|https?://[^\s<>\"')\]]+)"
)
HEADING_RE = re.compile(r"^(#{1,3})\s+(.*)$")
BULLET_RE = re.compile(r"^(\s*)[-*+]\s+(.*)$")
NUMBER_RE = re.compile(r"^(\s*)(\d{1,3})[.)]\s+(.*)$")
QUOTE_RE = re.compile(r"^>\s?(.*)$")
TABLE_SEP_RE = re.compile(r"^:?-{2,}:?$")


def _tag(base: str, tag: str) -> str:
    return f"{base} {tag}" if base else tag


def _inline(segments: list[tuple[str, str]], line: str, base: str = "") -> None:
    for index, part in enumerate(INLINE_RE.split(line)):
        if not part:
            continue
        if index % 2 == 0:
            segments.append((base, part))
        elif part.startswith("`"):
            segments.append((_tag(base, "code"), part[1:-1]))
        elif part.startswith("**"):
            segments.append((_tag(base, "b"), part[2:-2]))
        elif part.startswith("*"):
            segments.append((_tag(base, "i"), part[1:-1]))
        else:
            body = part.rstrip(".,;:!?")
            segments.append((_tag(base, "url"), body))
            if len(body) < len(part):
                segments.append((base, part[len(body):]))


def _is_table_line(line: str) -> bool:
    stripped = line.strip()
    return stripped.startswith("|") and stripped.count("|") >= 2


def _table(lines: list[str]) -> str:
    rows = []
    for line in lines:
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if all(TABLE_SEP_RE.match(cell) for cell in cells if cell) and any(cells):
            continue
        rows.append(cells)
    columns = max(len(row) for row in rows)
    rows = [row + [""] * (columns - len(row)) for row in rows]
    widths = [max(len(row[i]) for row in rows) for i in range(columns)]
    out = []
    for number, row in enumerate(rows):
        out.append(" | ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip())
        if number == 0 and len(rows) > 1:
            out.append("-+-".join("-" * w for w in widths))
    return "\n".join(out) + "\n"


def _plain(segments: list[tuple[str, str]], chunk: str) -> None:
    if not chunk:
        return
    lines = chunk.split("\n")
    index = 0
    while index < len(lines):
        line = lines[index]
        newline = "\n" if index < len(lines) - 1 else ""
        if _is_table_line(line):
            end = index
            while end < len(lines) and _is_table_line(lines[end]):
                end += 1
            if end - index >= 2:
                table = _table(lines[index:end])
                if end >= len(lines):
                    table = table.rstrip("\n")
                segments.append(("codeblock", table))
                if end < len(lines):
                    pass
                index = end
                continue
        index += 1
        heading = HEADING_RE.match(line)
        if heading:
            segments.append((f"h{len(heading.group(1))}", heading.group(2) + newline))
            continue
        bullet = BULLET_RE.match(line)
        if bullet:
            base = "li li2" if len(bullet.group(1).expandtabs(4)) >= 2 else "li"
            segments.append((base, "\u2022 "))
            _inline(segments, bullet.group(2) + newline, base)
            continue
        number = NUMBER_RE.match(line)
        if number:
            base = "li li2" if len(number.group(1).expandtabs(4)) >= 2 else "li"
            segments.append((base, f"{number.group(2)}. "))
            _inline(segments, number.group(3) + newline, base)
            continue
        quote = QUOTE_RE.match(line)
        if quote:
            _inline(segments, quote.group(1) + newline, "quote")
            continue
        _inline(segments, line + newline)


def parse(text: str) -> list[tuple[str, str]]:
    text = (text or "").replace("\r\n", "\n").strip("\n")
    segments: list[tuple[str, str]] = []
    position = 0
    for match in FENCE_RE.finditer(text):
        _plain(segments, text[position:match.start()])
        segments.append(("codeblock", match.group(1).rstrip("\n") + "\n"))
        position = match.end()
        if text[position:position + 1] == "\n":
            position += 1
    _plain(segments, text[position:])
    # хвостовой перевод строки не нужен: у Text он и так есть
    if segments and segments[-1][1].endswith("\n"):
        tag, last = segments[-1]
        segments[-1] = (tag, last.rstrip("\n"))
        if not segments[-1][1]:
            segments.pop()
    return segments