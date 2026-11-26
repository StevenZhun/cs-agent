"""PDF 作者源、可定位内容块和父子检索文档；不改变短 Excel 记录的契约。"""

import re
from datetime import date, datetime
from math import isfinite
from pathlib import PurePosixPath
from typing import Literal

from pydantic import Field, field_validator, model_validator

from shared.contracts.common import ContractModel, RawContractModel
from shared.contracts.conversation import ConversationMessage


class RagSource(ContractModel):
    """一个 PDF 的业务归属和适用范围；文件身份与知识身份分别保存。"""

    source_document_id: str = Field(min_length=1, max_length=64)
    knowledge_id: str = Field(min_length=1, max_length=64)
    version: str = Field(min_length=1, max_length=32)
    title: str = Field(min_length=1)
    scene_category: str = Field(min_length=1)
    source_file: str = Field(min_length=1)
    product_code: str | None = None
    channel: str | None = None
    region: str | None = None
    effective_at: date | None = None
    expires_at: date | None = None
    policy_parameter_refs: tuple[str, ...] = ()

    @field_validator("policy_parameter_refs")
    @classmethod
    def validate_policy_parameter_refs(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        """口径引用必须带不可变版本，且同一知识文档不能重复引用。"""

        pattern = re.compile(r"^[a-z][a-z0-9_]*:parameter-v[1-9][0-9]*$")
        if len(values) != len(set(values)) or any(not pattern.fullmatch(value) for value in values):
            raise ValueError("policy parameter refs must be unique key:parameter-v* values")
        return values

    @field_validator("source_file")
    @classmethod
    def validate_path(cls, value: str) -> str:
        """清单只保存项目内相对路径，不携带机器私有绝对路径。"""
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts or path.suffix.lower() != ".pdf":
            raise ValueError("PDF 来源必须是项目内相对路径")
        return value

    @model_validator(mode="after")
    def validate_dates(self) -> "RagSource":
        """有效期必须顺序正确；无终止日期表示尚未设置截止日期。"""
        if self.effective_at and self.expires_at and self.effective_at > self.expires_at:
            raise ValueError("知识有效期倒置")
        return self


class PdfContentBlock(ContractModel):
    """PDF 中一段正文、一张表格或有文字说明的图片。"""

    block_id: str = Field(min_length=1)
    kind: Literal["TEXT", "TABLE", "IMAGE"]
    page: int = Field(ge=1)
    bbox: tuple[float, float, float, float]
    text: str = Field(min_length=1)
    asset_ref: str | None = None
    rows: tuple[tuple[str, ...], ...] = ()

    @model_validator(mode="after")
    def validate_structure(self) -> "PdfContentBlock":
        """表格保留行列，图片保留原资源引用；位置按左上右下表示。"""
        x0, top, x1, bottom = self.bbox
        if not all(isfinite(value) for value in self.bbox) or x0 > x1 or top > bottom:
            raise ValueError("PDF 块位置倒置")
        if self.kind == "TABLE" and (len(self.rows) < 2 or not self.rows[0]):
            raise ValueError("表格需要表头和数据行")
        if self.kind == "TABLE" and (
            any(not cell for cell in self.rows[0])
            or any(len(row) != len(self.rows[0]) for row in self.rows)
        ):
            raise ValueError("表头缺失或表格行列不一致")
        if self.kind == "IMAGE" and not self.asset_ref:
            raise ValueError("图片需要资源引用")
        return self


class RagChunkPolicy(ContractModel):
    """分块配置单独版本化，Token 数由模型实际 tokenizer 计算。"""

    policy_id: str = "pdf-structure-512-v1"
    tokenizer_name: str = "Qwen/Qwen3-Embedding-0.6B"
    child_max_tokens: int = Field(default=512, ge=64, le=4096)
    parent_max_tokens: int = Field(default=4096, ge=512, le=16384)


class RagParentDocument(ContractModel):
    """一个连续章节页的完整上下文，不能用整份长 PDF 作为无限父文档。"""

    parent_doc_id: str = Field(min_length=1)
    page: int = Field(ge=1)
    section_title: str = Field(min_length=1)
    page_content: str = Field(min_length=1)
    block_ids: tuple[str, ...]
    token_count: int = Field(ge=1)


class RagChildChunk(ContractModel):
    """实际参与召回的子块；来源块编号使表格和图片说明仍可定位。"""

    chunk_id: str = Field(min_length=1)
    parent_doc_id: str = Field(min_length=1)
    page_content: str = Field(min_length=1)
    block_ids: tuple[str, ...]
    token_count: int = Field(ge=1)


class RagChunkMetadata(ContractModel):
    """RAG 子块随检索结果返回的溯源与适用范围。

    Excel 知识使用 ``KnowledgeMetadata`` 的工作表和行号；PDF 知识没有这组
    字段，因此单独保存子块、父文档和页码，避免用空字符串伪造另一种来源。
    """

    knowledge_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    scene_category: str = Field(min_length=1)
    product_code: str | None = None
    channel: str | None = None
    region: str | None = None
    version: str = Field(min_length=1)
    source_document_id: str = Field(min_length=1)
    parent_doc_id: str = Field(min_length=1)
    chunk_id: str = Field(min_length=1)
    section_title: str = Field(min_length=1)
    page: int = Field(ge=1)
    block_kinds: tuple[str, ...] = ()
    effective_at: date | None = None
    expires_at: date | None = None
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    embedding_profile_id: str = Field(min_length=1)
    release_version: str = Field(min_length=1)


class RagDocumentBundle(ContractModel):
    """可验证、可重建的一个知识版本；尚无向量也不能声称索引已完成。"""

    source: RagSource
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy: RagChunkPolicy
    page_count: int = Field(ge=1)
    removed_running_lines: tuple[str, ...] = ()
    blocks: tuple[PdfContentBlock, ...]
    parents: tuple[RagParentDocument, ...]
    chunks: tuple[RagChildChunk, ...]

    @model_validator(mode="after")
    def validate_links(self) -> "RagDocumentBundle":
        """拒绝重复编号、越界页码、孤立来源、错误父引用和超预算内容。"""
        blocks = {block.block_id: block for block in self.blocks}
        parents = {parent.parent_doc_id: parent for parent in self.parents}
        if not blocks or not parents or not self.chunks:
            raise ValueError("知识版本不能缺少正文、父文档或子块")
        if len(blocks) != len(self.blocks) or len(parents) != len(self.parents):
            raise ValueError("块或父文档编号重复")
        if len({chunk.chunk_id for chunk in self.chunks}) != len(self.chunks):
            raise ValueError("子块编号重复")
        if any(block.page > self.page_count for block in self.blocks):
            raise ValueError("来源页码超出 PDF")
        for parent in self.parents:
            if not parent.block_ids or not set(parent.block_ids) <= blocks.keys():
                raise ValueError("父文档引用不存在的内容块")
            if any(blocks[key].page != parent.page for key in parent.block_ids):
                raise ValueError("父文档页码与来源不一致")
            if parent.token_count > self.policy.parent_max_tokens:
                raise ValueError("父文档超过预算，需调整章节范围")
        used = set()
        used_parents = set()
        for chunk in self.chunks:
            parent = parents.get(chunk.parent_doc_id)
            if parent is None or not chunk.block_ids:
                raise ValueError("子块缺少父文档或来源")
            if not set(chunk.block_ids) <= set(parent.block_ids):
                raise ValueError("子块来源与父文档不一致")
            if chunk.token_count > self.policy.child_max_tokens:
                raise ValueError("子块超过 Token 预算")
            used.update(chunk.block_ids)
            used_parents.add(chunk.parent_doc_id)
        if used != set(blocks):
            raise ValueError("有内容块未进入可检索子块")
        if used_parents != set(parents):
            raise ValueError("有父文档没有对应的可检索子块")
        if sum(len(parent.block_ids) for parent in self.parents) != len(blocks):
            raise ValueError("同一来源块被多个父文档重复归属")
        return self


class RagCitation(ContractModel):
    """生成草稿引用的一项已验证证据；编号只在当前草稿内有效。"""

    citation_id: str = Field(pattern=r"^\[[1-9][0-9]*\]$")
    document_id: str = Field(min_length=1)
    parent_doc_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    section_title: str = Field(min_length=1)
    page: int = Field(ge=1)
    excerpt: str = Field(min_length=1, max_length=1200)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class RagDraft(ContractModel):
    """只供坐席审核的证据约束草稿，不是客户可见的助手答复。"""

    draft_id: str = Field(min_length=1, max_length=96)
    retrieval_id: str = Field(min_length=1, max_length=96)
    answer: str = Field(min_length=1, max_length=1800)
    citations: tuple[RagCitation, ...] = Field(min_length=1, max_length=8)
    provider: str = Field(min_length=1, max_length=64)
    model: str = Field(min_length=1, max_length=128)
    prompt_version: str = Field(min_length=1, max_length=64)
    created_at: datetime

    @model_validator(mode="after")
    def validate_citations(self) -> "RagDraft":
        """正文与引用清单必须完全一致，避免悬空来源或伪造编号。"""

        ids = tuple(item.citation_id for item in self.citations)
        if len(ids) != len(set(ids)):
            raise ValueError("RAG 草稿引用编号不能重复")
        answer_ids = {f"[{number}]" for number in re.findall(r"\[([1-9][0-9]*)\]", self.answer)}
        if answer_ids != set(ids):
            raise ValueError("RAG 草稿正文与已声明引用必须完全一致")
        return self


class RagReviewRequest(RawContractModel):
    """坐席对一份未发送 RAG 草稿作出的明确处理动作。"""

    recommendation_id: str = Field(min_length=1, max_length=64)
    turn_id: str = Field(min_length=1, max_length=64)
    draft_id: str = Field(min_length=1, max_length=96)
    action: Literal["APPROVE", "EDIT", "REJECT", "REGENERATE"]
    edited_answer: str | None = Field(default=None, max_length=1800)
    note: str = Field(default="", max_length=1000)

    @model_validator(mode="after")
    def validate_action(self) -> "RagReviewRequest":
        """采用不能夹带改写；编辑和拒绝必须留下可审计原因。"""

        if self.edited_answer is not None and (
            not self.edited_answer.strip()
            or any(
                ord(character) < 32 and character not in "\n\r\t"
                for character in self.edited_answer
            )
        ):
            raise ValueError("编辑后的答复不能是空白或包含异常控制字符")
        if self.action == "EDIT" and self.edited_answer is None:
            raise ValueError("编辑草稿必须提交修改后的完整答复")
        if self.action != "EDIT" and self.edited_answer is not None:
            raise ValueError("只有编辑动作可以提交修改后的答复")
        if self.action in {"EDIT", "REJECT", "REGENERATE"} and not self.note.strip():
            raise ValueError("编辑、拒绝或重新生成草稿必须填写处理原因")
        if any(ord(character) < 32 and character not in "\n\r\t" for character in self.note):
            raise ValueError("处理原因包含异常控制字符")
        return self


class RagReviewInstruction(RagReviewRequest):
    """BFF 已授权并落库的恢复指令；Runtime 只能消费，不能自行构造。"""

    review_type: Literal["RAG"] = "RAG"
    decision_id: str = Field(min_length=1, max_length=64)
    actor_id: str = Field(min_length=1, max_length=64)
    acted_at: datetime
    request_id: str = Field(min_length=1, max_length=128)
    trace_id: str = Field(min_length=1, max_length=128)


class RagReviewOutcome(ContractModel):
    """图恢复后的处理结果，用于 BFF 核对动作与最终文本。"""

    decision_id: str = Field(min_length=1, max_length=64)
    recommendation_id: str = Field(min_length=1, max_length=64)
    draft_id: str = Field(min_length=1, max_length=96)
    action: Literal["APPROVE", "EDIT", "REJECT"]
    actor_id: str = Field(min_length=1, max_length=64)
    final_answer: str = Field(min_length=1, max_length=1800)
    reviewed_at: datetime


class RagReviewResult(ContractModel):
    """坐席审核完成后的应用事实；客户消息已经在同一事务落库。"""

    review_id: str = Field(min_length=1, max_length=64)
    recommendation_id: str = Field(min_length=1, max_length=64)
    conversation_id: str = Field(min_length=1, max_length=64)
    turn_id: str = Field(min_length=1, max_length=64)
    action: Literal["APPROVE", "EDIT", "REJECT", "REGENERATE"]
    recommendation_status: Literal["SENT", "REJECTED", "DRAFT"]
    customer_message: ConversationMessage | None = None
    regenerated_recommendation_id: str | None = None
    regenerated_draft_id: str | None = None
    reviewed_at: datetime

    @model_validator(mode="after")
    def validate_result_shape(self) -> "RagReviewResult":
        """终局审核必须有客户消息；重新生成只返回新的待审核草稿标识。"""

        if self.action == "REGENERATE":
            if (
                self.recommendation_status != "DRAFT"
                or self.customer_message is not None
                or not self.regenerated_recommendation_id
                or not self.regenerated_draft_id
            ):
                raise ValueError("重新生成结果必须指向新的待审核草稿")
        elif self.customer_message is None or self.recommendation_status == "DRAFT":
            raise ValueError("终局审核结果必须包含客户消息")
        return self
