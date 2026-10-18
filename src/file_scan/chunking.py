"""原本への対応を壊さず、構造を優先して検索用の単位を組み立てる。

Embeddingモデルは未選定なので、第1版の上限は明示的に「文字数」で管理する。
トークン数を推測して保存しない。モデル採用時に、そのトークナイザーによる
分割へ変更すると変換設定ハッシュも変わり、別の変換世代として登録される。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator

from file_scan.domain import Block, Chunk, ChunkBlock


def _ranges(text: str, limit: int) -> Iterator[tuple[int, int]]:
    """改行か空白で区切る。返す座標は原本テキストのコードポイント位置。"""
    start = 0
    while start < len(text):
        end = min(start + limit, len(text))
        if end < len(text):
            # 行・手順の途中を避ける。ただし空白がない日本語も必ず先へ進める。
            boundary = max(text.rfind("\n", start, end), text.rfind(" ", start, end))
            if boundary > start + limit // 2:
                end = boundary + 1
        if text[start:end].strip():
            yield start, end
        start = end


def build_chunks(blocks: list[Block], title: str, max_characters: int = 1800) -> list[Chunk]:
    """同じ親・節・由来の要素だけをまとめ、各文字範囲の抜粋元を保持する。"""
    if max_characters < 100:
        raise ValueError("chunk_max_characters must be at least 100")
    chunks: list[Chunk] = []
    texts: list[str] = []
    mappings: list[ChunkBlock] = []
    flags: set[str] = set()
    identifiers: set[str] = set()
    headers: list[str] = []
    key: tuple | None = None

    def flush() -> None:
        """根拠本文と検索用の文脈を分離して確定する。本文へ説明を混入させない。"""
        if not texts or key is None:
            return
        content = "\n".join(texts)
        section = list(key[1])
        context = " / ".join([title, *section])
        search_text = context + "\n" + "\n".join(dict.fromkeys(headers)) + "\n" + content
        identity = json.dumps(
            {"order": len(chunks), "content": content, "context": context},
            sort_keys=True, ensure_ascii=False,
        )
        chunks.append(Chunk(
            chunk_id="sha256:" + hashlib.sha256(identity.encode()).hexdigest(),
            title=context,
            content_text=content,
            search_text=search_text,
            blocks=list(mappings),
            artifact_type=key[2],
            quality_flags=sorted(flags),
            identifiers=sorted(identifiers),
        ))
        texts.clear()
        mappings.clear()
        flags.clear()
        identifiers.clear()
        headers.clear()

    for block in blocks:
        # 目次・シート名はナビゲーション。本文の回答根拠へ重複登録しない。
        if block.element_type in {"document", "sheet", "section", "heading", "navigation"}:
            flush()
            key = None
            continue
        if not block.content_text or not block.content_text.strip():
            continue
        block_key = (block.parent_id, tuple(block.section_path), block.artifact_type)
        if key != block_key:
            flush()
            key = block_key
        for start, end in _ranges(block.content_text, max_characters):
            part = block.content_text[start:end]
            length = sum(map(len, texts)) + max(0, len(texts) - 1)
            if texts and length + 1 + len(part) > max_characters:
                flush()
                length = 0
            chunk_start = length + (1 if texts else 0)
            mappings.append(ChunkBlock(
                block_id=block.block_id,
                chunk_char_start=chunk_start,
                chunk_char_end=chunk_start + len(part),
                block_char_start=start,
                block_char_end=end,
            ))
            texts.append(part)
            flags.update(block.quality_flags)
            symbol = block.content_data.get("symbol_name")
            if isinstance(symbol, str):
                identifiers.add(symbol)
            column_headers = block.content_data.get("headers", [])
            if column_headers:
                headers.append(json.dumps(column_headers, ensure_ascii=False))
    flush()
    return chunks
