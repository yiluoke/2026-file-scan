"""抽出器とDB登録処理が共有する、保存先に依存しないデータ形式。

ファイル形式ごとの処理をDB処理から分離するため、抽出器はこのデータを
返すだけにする。原本バイト列、原本位置、抽出文字を別々に扱うのが要点。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Block:
    """段落・表・シートなどの原本要素。IDは変換世代の中で一意とする。"""

    block_id: str
    element_type: str
    content_text: str | None
    source_locations: list[dict[str, Any]]
    parent_id: str | None = None
    section_path: list[str] = field(default_factory=list)
    content_data: dict[str, Any] = field(default_factory=dict)
    quality_flags: list[str] = field(default_factory=list)
    artifact_type: str = "source"
    extraction_method: str = "native"


@dataclass
class ExtractedAsset:
    """画像の実体と出現位置。本文のBlockを介して原本との関係を保持する。"""

    block_id: str
    content: bytes
    mime_type: str
    source_locations: list[dict[str, Any]]
    asset_role: str = "original_image"
    rendering_details: dict[str, Any] = field(default_factory=dict)


@dataclass
class Relation:
    """同じ文書の要素間の関係。推定と原本上で確認できた参照は区別する。"""

    from_block_id: str
    to_block_id: str
    relation_type: str
    evidence: dict[str, Any] = field(default_factory=dict)
    relation_status: str = "confirmed"
    origin_type: str = "source"


@dataclass
class ExtractionResult:
    """1ファイルの抽出結果。未処理画像などは成功に見せかけず記録する。"""

    blocks: list[Block] = field(default_factory=list)
    assets: list[ExtractedAsset] = field(default_factory=list)
    relations: list[Relation] = field(default_factory=list)
    quality_flags: list[str] = field(default_factory=list)
    pending_images: int = 0
    metrics: dict[str, Any] = field(default_factory=dict)

    @property
    def processing_status(self) -> str:
        # 第1版では、品質警告が残る文書は保守的に部分抽出として登録する。
        return "partial" if self.quality_flags or self.pending_images else "succeeded"


@dataclass
class ChunkBlock:
    """チャンク本文のどの文字範囲が、どの原本ブロックに由来するか。"""

    block_id: str
    chunk_char_start: int
    chunk_char_end: int
    block_char_start: int | None = None
    block_char_end: int | None = None
    source_slice: dict[str, Any] = field(default_factory=dict)


@dataclass
class Chunk:
    """検索用の小さな単位。第1版では公開せず、根拠への対応を保存する。"""

    chunk_id: str
    title: str
    content_text: str
    search_text: str
    blocks: list[ChunkBlock]
    artifact_type: str = "source"
    quality_flags: list[str] = field(default_factory=list)
    token_count: int | None = None
    identifiers: list[str] = field(default_factory=list)
