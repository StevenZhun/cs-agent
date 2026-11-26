"""建立 RAG 权威知识版本与内容准备清单，保留中文表字段说明。"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0023_rag_content"
down_revision = "0022_conversation_hidden"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """只新增内容表，不修改客户会话、FAQ、SOP 或已有向量索引。"""
    op.create_table(
        "rag_document",
        sa.Column("knowledge_id", sa.String(64), primary_key=True, comment="稳定业务知识编号。"),
        sa.Column(
            "version", sa.String(32), primary_key=True, comment="知识版本；批准后修改需新版本。"
        ),
        sa.Column(
            "source_document_id",
            sa.String(64),
            nullable=False,
            comment="PDF 来源编号，不等同于知识编号。",
        ),
        sa.Column(
            "source_ref",
            sa.String(255),
            nullable=False,
            comment="知识作者源及其发布归属。",
        ),
        sa.Column(
            "bundle", JSONB, nullable=False, comment="经 RagDocumentBundle 校验的完整可重建知识包。"
        ),
        sa.Column(
            "content_hash",
            sa.String(64),
            nullable=False,
            comment="知识包规范 JSON 的 SHA-256，含来源和分块规则。",
        ),
        sa.Column(
            "lifecycle_status",
            sa.String(16),
            nullable=False,
            comment="DRAFT=草稿；APPROVED=已批准；PUBLISHED=已上线；RETIRED=已下线。",
        ),
        sa.Column(
            "enabled", sa.Boolean, nullable=False, comment="独立启用标记；下线后旧向量也不得作答。"
        ),
        sa.Column(
            "product_code", sa.String(64), comment="适用产品；空值表示通用知识，不证明个人资格。"
        ),
        sa.Column("channel", sa.String(64), comment="适用渠道；空值表示未限制。"),
        sa.Column("region", sa.String(64), comment="适用业务地区；空值表示未限制。"),
        sa.Column("effective_at", sa.Date, comment="知识开始生效日期。"),
        sa.Column("expires_at", sa.Date, comment="知识失效日期。"),
        sa.Column(
            "approved_by",
            sa.String(64),
            comment="批准主体；该记录不替代部署环境的合规审批。",
        ),
        sa.Column("approved_at", sa.DateTime(timezone=True), comment="批准时间，草稿为空。"),
        sa.Column("review_note", sa.Text, comment="范围、重复、业务冲突和资料质量的人工核查说明。"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="本知识版本首次保存时间。",
        ),
        sa.CheckConstraint(
            "lifecycle_status IN ('DRAFT','APPROVED','PUBLISHED','RETIRED')",
            name="lifecycle_status",
        ),
        sa.CheckConstraint(
            "effective_at IS NULL OR expires_at IS NULL OR effective_at <= expires_at",
            name="validity",
        ),
        sa.CheckConstraint(
            "lifecycle_status = 'DRAFT' OR "
            "(approved_by IS NOT NULL AND approved_at IS NOT NULL AND review_note IS NOT NULL)",
            name="approval",
        ),
        schema="content",
        comment="RAG 知识权威版本；JSON 正文含 PDF 坐标、表格、图片说明及父子关系。",
    )
    op.create_table(
        "rag_release",
        sa.Column(
            "release_version",
            sa.String(64),
            primary_key=True,
            comment="整批发布号，同名清单不可替换。",
        ),
        sa.Column(
            "manifest",
            JSONB,
            nullable=False,
            comment="完整知识快照、结构计数、审核和待构建索引要求。",
        ),
        sa.Column(
            "content_hash", sa.String(64), nullable=False, comment="冻结清单的规范 JSON SHA-256。"
        ),
        sa.Column(
            "lifecycle_status",
            sa.String(16),
            nullable=False,
            comment="READY=待构建激活；PUBLISHED=已激活；RETIRED=已停用。",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="清单准备时间；不是索引完成时间。",
        ),
        sa.CheckConstraint(
            "lifecycle_status IN ('READY','PUBLISHED','RETIRED')", name="lifecycle_status"
        ),
        schema="content",
        comment="RAG 不可变内容发布快照；索引就绪后才允许激活。",
    )


def downgrade() -> None:
    """仅回退新增内容表；生产回退前必须确认内容及发布历史可恢复。"""
    op.drop_table("rag_release", schema="content")
    op.drop_table("rag_document", schema="content")
