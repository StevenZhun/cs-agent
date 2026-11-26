"""RAG 知识权威版本和发布准备快照，不把向量库当内容权威源。"""

from datetime import date, datetime

from sqlalchemy import Boolean, CheckConstraint, Date, DateTime, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from shared.persistence import PostgresBase


class RagDocumentModel(PostgresBase):
    """同一知识版本保存完整来源、正文块和父子结构，批准后不可原地覆盖。"""

    __tablename__ = "rag_document"
    __table_args__ = (
        CheckConstraint(
            "lifecycle_status IN ('DRAFT','APPROVED','PUBLISHED','RETIRED')",
            name="lifecycle_status",
        ),
        CheckConstraint(
            "effective_at IS NULL OR expires_at IS NULL OR effective_at <= expires_at",
            name="validity",
        ),
        CheckConstraint(
            "lifecycle_status = 'DRAFT' OR "
            "(approved_by IS NOT NULL AND approved_at IS NOT NULL AND review_note IS NOT NULL)",
            name="approval",
        ),
        {
            "schema": "content",
            "comment": "RAG 知识权威版本；JSON 正文含 PDF 坐标、表格、图片说明及父子关系。",
        },
    )

    knowledge_id: Mapped[str] = mapped_column(
        String(64), primary_key=True, comment="稳定业务知识编号。"
    )
    version: Mapped[str] = mapped_column(
        String(32), primary_key=True, comment="知识版本；批准后修改需新版本。"
    )
    source_document_id: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="PDF 来源编号，不等同于知识编号。"
    )
    source_ref: Mapped[str] = mapped_column(
        String(255), nullable=False, comment="知识作者源及其发布归属。"
    )
    bundle: Mapped[dict] = mapped_column(
        JSONB, nullable=False, comment="经 RagDocumentBundle 校验的完整可重建知识包。"
    )
    content_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="知识包规范 JSON 的 SHA-256，含来源和分块规则。"
    )
    lifecycle_status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        comment="DRAFT=草稿；APPROVED=已批准；PUBLISHED=已上线；RETIRED=已下线。",
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, comment="独立启用标记；下线后旧向量也不得作答。"
    )
    product_code: Mapped[str | None] = mapped_column(
        String(64), comment="适用产品；空值表示通用知识，不证明个人资格。"
    )
    channel: Mapped[str | None] = mapped_column(String(64), comment="适用渠道；空值表示未限制。")
    region: Mapped[str | None] = mapped_column(String(64), comment="适用业务地区；空值表示未限制。")
    effective_at: Mapped[date | None] = mapped_column(Date, comment="知识开始生效日期。")
    expires_at: Mapped[date | None] = mapped_column(Date, comment="知识失效日期。")
    approved_by: Mapped[str | None] = mapped_column(
        String(64), comment="批准主体；该记录不替代部署环境的合规审批。"
    )
    approved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), comment="批准时间，草稿为空。"
    )
    review_note: Mapped[str | None] = mapped_column(
        Text, comment="范围、重复、业务冲突和资料质量的人工核查说明。"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="本知识版本首次保存时间。"
    )


class RagReleaseModel(PostgresBase):
    """准备清单冻结完整知识版本；READY 不代表 Milvus 已构建或线上已激活。"""

    __tablename__ = "rag_release"
    __table_args__ = (
        CheckConstraint(
            "lifecycle_status IN ('READY','PUBLISHED','RETIRED')", name="lifecycle_status"
        ),
        {"schema": "content", "comment": "RAG 不可变内容发布快照；索引就绪后才允许激活。"},
    )

    release_version: Mapped[str] = mapped_column(
        String(64), primary_key=True, comment="整批发布号，同名清单不可替换。"
    )
    manifest: Mapped[dict] = mapped_column(
        JSONB, nullable=False, comment="完整知识快照、结构计数、审核和待构建索引要求。"
    )
    content_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="冻结清单的规范 JSON SHA-256。"
    )
    lifecycle_status: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="READY=待构建激活；PUBLISHED=已激活；RETIRED=已停用。"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="清单准备时间；不是索引完成时间。"
    )
