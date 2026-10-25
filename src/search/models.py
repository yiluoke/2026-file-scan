"""Dify互換入力。未知の項目と暗黙の数値・真偽値変換を拒否する。"""

import math
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

from file_scan.tokens import count_tokens


class InputModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class RetrievalSetting(InputModel):
    top_k: StrictInt = Field(ge=1, le=20)
    score_threshold: float = Field(ge=0, le=1)

    @field_validator("score_threshold", mode="before")
    @classmethod
    def finite_threshold(cls, value):
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
        ):
            raise ValueError("invalid threshold")
        return value


class Condition(InputModel):
    name: Literal["target_release", "document_id", "feature_id", "artifact_type"]
    comparison_operator: Literal["is", "in"]
    value: StrictStr | list[StrictStr]

    @model_validator(mode="after")
    def allowed_condition(self):
        values = self.value if isinstance(self.value, list) else [self.value]
        if not 1 <= len(values) <= 20 or len(values) != len(set(values)):
            raise ValueError("invalid values")
        if any(not v.strip() or len(v) > 256 or "\x00" in v for v in values):
            raise ValueError("invalid value")
        if (self.comparison_operator == "is") != isinstance(self.value, str):
            raise ValueError("invalid value type")
        if self.name == "target_release" and self.comparison_operator != "is":
            raise ValueError("invalid release")
        if self.name == "artifact_type" and any(v not in {"source", "derived"} for v in values):
            raise ValueError("invalid origin")
        return self


class MetadataCondition(InputModel):
    logical_operator: Literal["and"] = "and"
    conditions: list[Condition] = Field(max_length=8)

    @model_validator(mode="after")
    def unique_names(self):
        names = [c.name for c in self.conditions]
        if len(names) != len(set(names)):
            raise ValueError("duplicate condition")
        return self


class SearchRequest(InputModel):
    knowledge_id: StrictStr = Field(min_length=1, max_length=128)
    query: StrictStr = Field(min_length=1, max_length=4000)
    retrieval_setting: RetrievalSetting
    metadata_condition: MetadataCondition | None = None

    @field_validator("knowledge_id", "query")
    @classmethod
    def nonempty(cls, value):
        if not value.strip() or "\x00" in value:
            raise ValueError("invalid text")
        return value

    @field_validator("query")
    @classmethod
    def token_budget(cls, value):
        if count_tokens(value) > 2048:
            raise ValueError("query token limit")
        return value

    def filters(self):
        return (
            {c.name: c.value for c in self.metadata_condition.conditions}
            if self.metadata_condition
            else {}
        )


class EvidenceRecord(BaseModel):
    content: str
    score: float = Field(ge=0, le=1)
    title: str
    metadata: dict


class RetrievalStatus(BaseModel):
    snapshot_id: str
    search_mode: Literal["current", "history"]
    resolved_release_id: str
    catalog_revision: int
    publication_revision: int
    policy_revision: int | None
    access_control: Literal["not_enforced"]
    execution_mode: Literal["local_prototype"]
    embedding_profile_id: str
    search_config_version: str
    coverage_scope: Literal["configured_knowledge_release"]
    coverage_state: Literal["complete", "incomplete", "unknown"]
    pending_updates: int | None
    failed_documents: int | None
    partial_documents: int | None
    uncollected_documents: int | None
    index_pending_documents: int | None
    unknown_documents: int | None
    result_state: Literal["matched", "no_match", "budget_exhausted"]
    returned_records: int
    limit_reasons: list[str]
    message: str


class SearchResponse(BaseModel):
    api_version: Literal["1"]
    request_id: str
    records: list[EvidenceRecord]
    retrieval_status: RetrievalStatus


class RetrievalResponse(BaseModel):
    records: list[EvidenceRecord]
