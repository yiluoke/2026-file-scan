"""構造境界と引用文字範囲が、分割後も一致することを検証する。"""

from file_scan.chunking import build_chunks
from file_scan.domain import Block


def test_long_japanese_text_keeps_exact_character_provenance():
    original = "入力条件を確認してください。\n" * 120
    block = Block("one", "paragraph", original, [{"start_line": 1}])
    chunks = build_chunks([block], "手順", 100)
    assert len(chunks) > 1
    assert "".join(chunk.content_text for chunk in chunks) == original
    for chunk in chunks:
        assert len(chunk.content_text) <= 100
        mapping = chunk.blocks[0]
        assert chunk.content_text == original[mapping.block_char_start:mapping.block_char_end]
        assert 0 < chunk.token_count <= 8191


def test_sections_and_origins_are_not_mixed():
    blocks = [Block("a", "paragraph", "原文A", [{"line": 1}], section_path=["A"]),
              Block("b", "paragraph", "原文B", [{"line": 2}], section_path=["B"]),
              Block("c", "paragraph", "生成説明", [{"line": 3}], section_path=["B"], artifact_type="derived")]
    chunks = build_chunks(blocks, "資料")
    assert len(chunks) == 3
    assert [chunk.artifact_type for chunk in chunks] == ["source", "source", "derived"]


def test_multiple_blocks_keep_non_overlapping_source_ranges():
    blocks = [Block("a", "paragraph", "先頭の段落。", [{"line": 1}]),
              Block("b", "paragraph", "次の段落。", [{"line": 2}])]
    chunks = build_chunks(blocks, "資料")
    assert len(chunks) == 1
    for mapping, block in zip(chunks[0].blocks, blocks, strict=True):
        assert chunks[0].content_text[mapping.chunk_char_start:mapping.chunk_char_end] == block.content_text
