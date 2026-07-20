#!/usr/bin/env python3
"""Audit a Chinese market-book TOC for common structural and title risks."""

from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
import unicodedata
import zipfile
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from xml.etree import ElementTree


LINE_RE = re.compile(
    r"^(?P<number>\d+\.\d+(?:\.\d+)?)\s*[、.．]?\s*(?P<title>\S.*)$"
)
CHAPTER_RE = re.compile(r"^第\s*(?P<number>[0-9一二三四五六七八九十百零〇]+)\s*章\s*(?P<title>.*)$")
STRONG_WORDS = (
    "重磅",
    "炸裂",
    "杀疯了",
    "震惊",
    "颠覆",
    "封神",
    "必看",
    "终极",
    "秘籍",
    "史上最强",
)


@dataclass
class Entry:
    line: int
    number: str
    title: str
    level: str


@dataclass
class Issue:
    severity: str
    code: str
    line: int | None
    message: str


def clean_line(raw: str) -> str:
    line = raw.strip()
    line = re.sub(r"^#{1,6}\s*", "", line)
    line = re.sub(r"^[-*+]\s+", "", line)
    return line.strip()


def normalized_title(title: str) -> str:
    return "".join(
        ch.casefold()
        for ch in unicodedata.normalize("NFKC", title)
        if not ch.isspace() and not unicodedata.category(ch).startswith("P")
    )


def visible_length(title: str) -> int:
    return sum(
        1
        for ch in unicodedata.normalize("NFKC", title)
        if not ch.isspace() and not unicodedata.category(ch).startswith("P")
    )


def parse_entries(text: str) -> list[Entry]:
    entries: list[Entry] = []
    for line_no, raw in enumerate(text.splitlines(), start=1):
        line = clean_line(raw)
        if not line:
            continue
        chapter = CHAPTER_RE.match(line)
        if chapter:
            entries.append(
                Entry(line_no, f"第{chapter.group('number')}章", chapter.group("title").strip(), "chapter")
            )
            continue
        item = LINE_RE.match(line)
        if not item:
            continue
        number = item.group("number")
        level = "subsection" if number.count(".") == 2 else "section"
        entries.append(Entry(line_no, number, item.group("title").strip(), level))
    return entries


def add_title_checks(entries: list[Entry], issues: list[Issue]) -> None:
    for entry in entries:
        if entry.level == "chapter":
            continue
        if not entry.title:
            issues.append(Issue("error", "empty-title", entry.line, f"{entry.number}缺少标题。"))
            continue
        length = visible_length(entry.title)
        low, high = (8, 34) if entry.level == "section" else (6, 26)
        if length < low:
            issues.append(Issue("warning", "title-too-short", entry.line, f"{entry.number}标题仅{length}个有效字符，可能过于抽象。"))
        if length > high:
            issues.append(Issue("warning", "title-too-long", entry.line, f"{entry.number}标题有{length}个有效字符，建议检查是否包含多个承诺。"))
        marks = sum(entry.title.count(mark) for mark in "！？!?")
        if marks > 1:
            issues.append(Issue("warning", "punctuation-overuse", entry.line, f"{entry.number}包含多个问号或感叹号。"))
        if entry.title.count("：") + entry.title.count(":") > 1:
            issues.append(Issue("warning", "colon-overuse", entry.line, f"{entry.number}包含多个冒号。"))
        found = [word for word in STRONG_WORDS if word in entry.title]
        if found:
            issues.append(Issue("warning", "hype-word", entry.line, f"{entry.number}包含刺激词：{'、'.join(found)}。"))


def add_duplicate_checks(entries: list[Entry], issues: list[Issue]) -> None:
    groups: dict[str, list[Entry]] = defaultdict(list)
    for entry in entries:
        if entry.level != "chapter" and entry.title:
            groups[normalized_title(entry.title)].append(entry)
    for group in groups.values():
        if len(group) > 1:
            locations = "、".join(item.number for item in group)
            issues.append(Issue("error", "duplicate-title", group[0].line, f"标题重复：{locations}。"))


def add_source_similarity_checks(
    entries: list[Entry], source_entries: list[Entry], issues: list[Issue]
) -> None:
    source_by_number = {
        entry.number: entry for entry in source_entries if entry.level != "chapter"
    }
    for entry in entries:
        if entry.level == "chapter" or entry.number not in source_by_number:
            continue
        source = source_by_number[entry.number]
        current_text = normalized_title(entry.title)
        source_text = normalized_title(source.title)
        if not current_text or not source_text:
            continue
        ratio = difflib.SequenceMatcher(None, source_text, current_text).ratio()
        if ratio == 1:
            issues.append(
                Issue("error", "unchanged-title", entry.line, f"{entry.number}与原题相同，尚未完成改写。")
            )
        elif ratio >= 0.74:
            issues.append(
                Issue(
                    "warning",
                    "source-style-overlap",
                    entry.line,
                    f"{entry.number}与原题文本相似度为{ratio:.0%}，建议检查是否仍沿用原句骨架。",
                )
            )


def add_number_checks(entries: list[Entry], issues: list[Issue]) -> None:
    section_numbers: dict[str, list[tuple[int, Entry]]] = defaultdict(list)
    subsection_numbers: dict[tuple[str, str], list[tuple[int, Entry]]] = defaultdict(list)
    known_sections: set[tuple[str, str]] = set()

    for entry in entries:
        if entry.level == "section":
            chapter, section = entry.number.split(".")
            section_numbers[chapter].append((int(section), entry))
            known_sections.add((chapter, section))
        elif entry.level == "subsection":
            chapter, section, subsection = entry.number.split(".")
            subsection_numbers[(chapter, section)].append((int(subsection), entry))
            if (chapter, section) not in known_sections:
                issues.append(Issue("error", "orphan-subsection", entry.line, f"{entry.number}前没有对应的{chapter}.{section}节。"))

    for chapter, values in section_numbers.items():
        ordered = sorted(number for number, _ in values)
        expected = list(range(1, max(ordered) + 1)) if ordered else []
        if ordered != expected:
            issues.append(Issue("warning", "section-gap", values[0][1].line, f"第{chapter}章的节编号为{ordered}，存在跳号或重复。"))
        count = len(values)
        if count < 2 or count > 7:
            issues.append(Issue("warning", "section-count", values[0][1].line, f"第{chapter}章共有{count}节，建议检查拆分颗粒度。"))

    for (chapter, section), values in subsection_numbers.items():
        ordered = sorted(number for number, _ in values)
        expected = list(range(1, max(ordered) + 1)) if ordered else []
        if ordered != expected:
            issues.append(Issue("warning", "subsection-gap", values[0][1].line, f"{chapter}.{section}的小节编号为{ordered}，存在跳号或重复。"))
        count = len(values)
        if count < 2 or count > 6:
            issues.append(Issue("warning", "subsection-count", values[0][1].line, f"{chapter}.{section}共有{count}个小节，建议检查拆分颗粒度。"))


def audit(text: str, source_text: str | None = None) -> tuple[list[Entry], list[Issue]]:
    entries = parse_entries(text)
    issues: list[Issue] = []
    if not entries:
        issues.append(Issue("error", "no-toc-entry", None, "未识别到章、节或小节编号。"))
        return entries, issues
    add_title_checks(entries, issues)
    add_duplicate_checks(entries, issues)
    add_number_checks(entries, issues)
    if source_text is not None:
        add_source_similarity_checks(entries, parse_entries(source_text), issues)
    return entries, issues


def read_input(path: str) -> str:
    if path == "-":
        return sys.stdin.read()
    input_path = Path(path)
    if input_path.suffix.casefold() == ".docx":
        with zipfile.ZipFile(input_path) as archive:
            root = ElementTree.fromstring(archive.read("word/document.xml"))
        namespace = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
        paragraphs: list[str] = []
        for paragraph in root.iter(namespace + "p"):
            text = "".join((node.text or "") for node in paragraph.iter(namespace + "t")).strip()
            if text:
                paragraphs.append(text)
        return "\n".join(paragraphs)
    return input_path.read_text(encoding="utf-8")


def print_text(entries: list[Entry], issues: list[Issue]) -> None:
    counts = Counter(entry.level for entry in entries)
    print(
        f"识别：章{counts['chapter']}，节{counts['section']}，小节{counts['subsection']}；"
        f"问题{len(issues)}。"
    )
    if not issues:
        print("未发现可自动识别的形式风险。仍需人工检查内容逻辑与标题可兑现性。")
        return
    for issue in issues:
        location = f"第{issue.line}行" if issue.line else "全局"
        print(f"[{issue.severity.upper()}] {location} {issue.code}：{issue.message}")


def main() -> int:
    parser = argparse.ArgumentParser(description="检查中文市场书目录的常见形式风险。")
    parser.add_argument("path", nargs="?", default="-", help="UTF-8 Markdown或文本文件；省略或使用-时读取标准输入。")
    parser.add_argument("--source", help="原目录的UTF-8文本、Markdown或DOCX文件，用于检查无效改写和原句骨架残留。")
    parser.add_argument("--json", action="store_true", help="输出JSON。")
    args = parser.parse_args()

    try:
        text = read_input(args.path)
        source_text = read_input(args.source) if args.source else None
    except (OSError, UnicodeError) as exc:
        print(f"读取失败：{exc}", file=sys.stderr)
        return 2

    entries, issues = audit(text, source_text)
    if args.json:
        print(json.dumps({"entries": [asdict(item) for item in entries], "issues": [asdict(item) for item in issues]}, ensure_ascii=False, indent=2))
    else:
        print_text(entries, issues)
    return 1 if any(issue.severity == "error" for issue in issues) else 0


if __name__ == "__main__":
    raise SystemExit(main())
