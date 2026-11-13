"""Markdownの本文、リンク、見出しを構文に沿って扱う。"""

from __future__ import annotations

import re
import string
import unicodedata
from collections import Counter
from collections.abc import Callable, Iterator

from markdown_it import MarkdownIt
from markdown_it.rules_inline import link
from markdown_it.rules_inline.state_inline import StateInline
from markdown_it.token import Token

from .store import ScanError


def _link_with_span(state: StateInline, silent: bool) -> bool:
    start, index = state.pos, len(state.tokens) + bool(state.pending)
    if not link(state, silent):
        return False
    if not silent:
        label_end = state.md.helpers.parseLinkLabel(state, start, True)
        state.tokens[index].meta.update(span=(start, state.pos), label=state.src[start:label_end + 1])
    return True


PARSER = MarkdownIt("commonmark").enable("table")
PARSER.inline.ruler.at("link", _link_with_span)


def links(tokens: list[Token]) -> Iterator[Token]:
    for token in tokens:
        if token.type in {"link_open", "image"}:
            yield token
        if token.children:
            yield from links(token.children)


def destination(token: Token) -> str:
    return str(token.attrGet("src" if token.type == "image" else "href") or "")


def heading_anchors(tokens: list[Token]) -> set[str]:
    """通常のMarkdown見出しから、重複の連番を含むアンカーを作る。"""
    anchors = set()
    duplicates = Counter()
    for block, inline in zip(tokens, tokens[1:]):
        if block.type != "heading_open" or inline.type != "inline":
            continue
        title = "".join(token.content for token in inline.children or []
                        if token.type in {"text", "code_inline", "image"})
        slug = "".join(char for char in title.strip().lower()
                       if char in " -_" or (char not in string.punctuation
                                            and unicodedata.category(char)[0] in "LNM"))
        slug = slug.replace(" ", "-")
        anchor = slug
        while anchor in anchors:
            duplicates[slug] += 1
            anchor = f"{slug}-{duplicates[slug]}"
        anchors.add(anchor)
    return anchors


def unclosed_fence(lines: list[str], token: Token) -> bool:
    first, end = token.map
    if end - first < 2:
        return True
    # 開始フェンスの記号と数を使い、引用・リスト内の閉じフェンスも確認する。
    marker = re.escape(token.markup[0]) + "{" + str(len(token.markup)) + ",}"
    return not re.fullmatch(r"[\t >]*" + marker + r"[\t ]*", lines[end - 1])


def _original_offsets(lines, starts, inline, cursors):
    """引用・リストの接頭辞と表の空白を除いたinline位置を原文へ戻す。"""
    offsets = []
    first, end = inline.map
    for index, part in enumerate(inline.content.split("\n")):
        number = first + index
        if number >= end:
            raise ScanError("markdown_link_position_unavailable")
        original = lines[number]
        # markdown-itの表処理はセル内のエスケープ済みパイプを戻す。
        positions = list(range(len(original)))
        cursor = cursors.get(number, 0)
        if part not in original[cursor:]:
            positions = [i for i in positions if not original.startswith(r"\|", i)]
            simplified = "".join(original[i] for i in positions)
            start = simplified.find(part, sum(i < cursor for i in positions))
        else:
            start = original.find(part, cursor)
        if start < 0:
            raise ScanError("markdown_link_position_unavailable")
        offsets.extend(starts[number] + i for i in positions[start:start + len(part)])
        if part:
            cursors[number] = positions[start + len(part) - 1] + 1
        offsets.append(starts[number] + len(original))
    return offsets


def rewrite_links(text: str, replacement: Callable[[Token], str | None]) -> str:
    """実際のMarkdownリンクだけを置換し、コードと他の原文を保持する。"""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    starts = [0]
    starts.extend(match.end() for match in re.finditer(r"\r\n|\r|\n", text))
    changes, cursors = [], {}
    inlines = [token for token in PARSER.parse(text) if token.type == "inline" and token.map]
    edits = [[(token, value) for token in inline.children or []
              if token.type == "link_open" and "span" in token.meta
              and (value := replacement(token)) is not None] for inline in inlines]
    touched = {inline.map[0] for inline, updates in zip(inlines, edits) if updates}
    for inline, updates in zip(inlines, edits):
        # 同じ行の表セルなどを左から対応付ける。
        if inline.map[0] not in touched:
            continue
        offsets = _original_offsets(lines, starts, inline, cursors)
        for token, value in updates:
            start, end = token.meta["span"]
            changes.append((offsets[start], offsets[end - 1] + 1, value))
    for start, end, value in sorted(changes, reverse=True):
        text = text[:start] + value + text[end:]
    return text
