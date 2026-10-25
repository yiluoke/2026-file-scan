"""表の行・検索文脈・トークン予算を考慮し、原本の文字範囲を保持して分割する。"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import replace

from file_scan.domain import Block, Chunk, ChunkBlock
from file_scan.tokens import count_tokens, fit_prefix


def table_headers(block: Block) -> str:
    headers = block.content_data.get("headers", [])
    if headers:
        return json.dumps(headers, ensure_ascii=False)
    table = block.content_data.get("table", {})
    cells = table.get("table_cells", []) if isinstance(table, dict) else []
    names = [str(cell.get("text", "")) for cell in cells if cell.get("column_header")]
    if names:
        return " | ".join(dict.fromkeys(names))
    lines = (block.content_text or "").splitlines()
    for index, line in enumerate(lines[1:], 1):
        if re.fullmatch(r"[\s|:\-]+", line) and "-" in line:
            return lines[index - 1]
    return ""


def build_chunks(blocks: list[Block], title: str, max_characters: int = 1800,
                 max_tokens: int = 8191, context_max_tokens: int = 1024) -> list[Chunk]:
    """本文を欠落させず、検索用文脈だけを制限する。座標はUnicode文字位置。"""
    if max_characters < 100 or max_tokens < 32 or not 1 <= context_max_tokens < max_tokens:
        raise ValueError("invalid chunk limits")
    chunks: list[Chunk] = []
    texts, mappings = [], []
    flags, identifiers = set(), set()
    key, prefix, context = None, "", ""

    def flush():
        if not texts:
            return
        content = "\n".join(texts)
        search = prefix + content
        identity = json.dumps({"order": len(chunks), "search_text": search},
                              ensure_ascii=False, sort_keys=True)
        chunks.append(Chunk(
            chunk_id="sha256:" + hashlib.sha256(identity.encode()).hexdigest(),
            title=context, content_text=content, search_text=search, blocks=list(mappings),
            artifact_type=key[2], quality_flags=sorted(flags), identifiers=sorted(identifiers),
            token_count=count_tokens(search),
        ))
        texts.clear()
        mappings.clear()
        flags.clear()
        identifiers.clear()

    for block in blocks:
        if block.element_type in {"document", "sheet", "section", "heading", "navigation"}:
            flush()
            key = None
            continue
        text = block.content_text
        if not text or not text.strip():
            continue
        headers = table_headers(block)
        block_key = (block.parent_id, tuple(block.section_path), block.artifact_type,
                     headers, block.block_id if block.element_type == "table" else None)
        if key != block_key:
            flush()
            key = block_key
            context = " / ".join([title, *block.section_path])
            context_budget = min(context_max_tokens, max_tokens // 2)
            budget = max(0, context_budget - 2)
            if headers:
                heading = fit_prefix(context, budget // 2)
                prefix = heading + "\n" + fit_prefix(headers, max(0, budget - count_tokens(heading)))
            else:
                prefix = fit_prefix(context, budget)
            while prefix and count_tokens(prefix + "\n\n") > context_budget:
                prefix = prefix[:-1]
            prefix += "\n\n"
        start = 0
        while start < len(text):
            existing = "\n".join(texts)
            separator = "\n" if texts else ""
            capacity = max_characters - len(existing) - len(separator)
            if capacity <= 0:
                flush()
                continue
            end = min(len(text), start + capacity)
            candidate_prefix = prefix + existing + separator
            if count_tokens(candidate_prefix + text[start:end]) > max_tokens:
                low, high = start, end
                while low < high:
                    middle = (low + high + 1) // 2
                    if count_tokens(candidate_prefix + text[start:middle]) <= max_tokens:
                        low = middle
                    else:
                        high = middle - 1
                end = low
            if end == start:
                if texts:
                    flush()
                    continue
                raise ValueError("chunk_token_budget_too_small")
            if end < len(text):
                boundary = text.rfind("\n", start, end)
                if boundary < start and block.element_type != "table":
                    boundary = text.rfind(" ", start, end)
                if boundary >= start and (block.element_type == "table"
                                          or boundary > start + (end - start) // 2):
                    end = boundary + 1
                elif block.element_type == "table":
                    flags.add("table_row_split")
            # BPEは文字接頭辞に対して厳密には単調ではない。行境界へ戻した後も検査する。
            while end > start and count_tokens(candidate_prefix + text[start:end]) > max_tokens:
                end -= 1
            if end == start:
                if texts:
                    flush()
                    continue
                raise ValueError("chunk_token_budget_too_small")
            part = text[start:end]
            if part.strip():
                if block.element_type == "table" and (
                    (start > 0 and text[start - 1] != "\n")
                    or (end < len(text) and text[end - 1] != "\n")
                ):
                    flags.add("table_row_split")
                chunk_start = len(existing) + len(separator)
                mappings.append(ChunkBlock(block.block_id, chunk_start, chunk_start + len(part),
                                           start, end))
                texts.append(part)
                flags.update(block.quality_flags)
                full_prefix = context + "\n" + headers + "\n\n" if headers else context + "\n\n"
                if prefix != full_prefix:
                    flags.add("search_context_truncated")
                symbol = block.content_data.get("symbol_name")
                if isinstance(symbol, str):
                    identifiers.add(symbol)
            start = end
            if start < len(text):
                flush()
    flush()
    if not chunks:
        headings = [replace(block, element_type="paragraph") for block in blocks
                    if block.element_type == "heading" and block.content_text]
        if headings:
            return build_chunks(headings, title, max_characters, max_tokens, context_max_tokens)
    return chunks
