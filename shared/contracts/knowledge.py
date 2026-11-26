"""知识源记录、检索文档及其元数据模型。"""

from __future__ import annotations

from datetime import date

from pydantic import Field, model_validator

from shared.contracts.common import ContractModel
from shared.contracts.enums import KnowledgeProjectionType


class SourceKnowledgeRecord(ContractModel):
    """Excel 等作者源经过解析、等待分类发布的标准记录。"""

    knowledge_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    scene_category: str = Field(min_length=1)
    customer_questions: tuple[str, ...] = ()
    handling_steps: tuple[str, ...] = ()
    response_script: str = Field(min_length=1)
    notices: tuple[str, ...] = ()
    related_systems: tuple[str, ...] = ()
    escalation_condition: str | None = None
    product_code: str | None = None
    version: str = Field(default="v1", min_length=1)
    enabled: bool = True
    effective_at: date | None = None
    expires_at: date | None = None
    source_file: str = Field(min_length=1)
    source_sheet: str = Field(min_length=1)
    source_row: int = Field(ge=1)
    publish_target: KnowledgeProjectionType

    @model_validator(mode="after")
    def validate_validity_window(self) -> SourceKnowledgeRecord:
        """拒绝生效时间晚于失效时间的知识记录。"""

        if self.effective_at and self.expires_at and self.effective_at > self.expires_at:
            raise ValueError("effective_at must be before or equal to expires_at")
        return self


class KnowledgeMetadata(ContractModel):
    """随正文写入 Milvus、用于过滤和溯源的知识元数据。"""

    knowledge_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    scene_category: str = Field(min_length=1)
    product_code: str | None = None
    version: str = Field(min_length=1)
    enabled: bool
    effective_at: date | None = None
    expires_at: date | None = None
    source_file: str = Field(min_length=1)
    source_sheet: str = Field(min_length=1)
    source_row: int = Field(ge=1)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    projection_type: KnowledgeProjectionType = KnowledgeProjectionType.RAG


class KnowledgeDocument(ContractModel):
    """与存储无关的检索文档，MVP 中一对一映射 LangChain Document。"""

    id: str = Field(min_length=1)
    page_content: str = Field(min_length=1)
    metadata: KnowledgeMetadata

    @model_validator(mode="after")
    def enforce_one_record_one_document(self) -> KnowledgeDocument:
        """保证 MVP 中知识记录和检索文档共享同一个稳定 ID。"""

        if self.id != self.metadata.knowledge_id:
            raise ValueError("document id must equal metadata.knowledge_id in the MVP")
        if self.metadata.projection_type is not KnowledgeProjectionType.RAG:
            raise ValueError("KnowledgeDocument only accepts RAG projections")
        return self
