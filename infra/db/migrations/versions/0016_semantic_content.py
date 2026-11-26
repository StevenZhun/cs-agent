"""增加相似意图语料和离线语义索引构建记录。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0016_semantic_content"
down_revision: str | None = "0015_conversation_pin"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LIFECYCLE = (
    "DRAFT=草稿；IN_REVIEW=审核中；APPROVED=已批准；"
    "PUBLISHED=已建索引；RETIRED=已下线。"
)


def upgrade() -> None:
    """建立语义检索权威语料表，并保存每次可复现的索引构建信息。"""

    op.create_table(
        "intent_similarity",
        sa.Column("similarity_id", sa.String(64), primary_key=True, comment="相似意图语料编号。"),
        sa.Column("version", sa.String(32), primary_key=True, comment="语料版本号。"),
        sa.Column("intent_id", sa.String(64), nullable=False, comment="所属动作意图编号。"),
        sa.Column("intent_version", sa.String(32), nullable=False, comment="所属动作意图版本。"),
        sa.Column("text", sa.Text(), nullable=False, comment="审核时阅读的语料原文。"),
        sa.Column(
            "normalized_text",
            sa.Text(),
            nullable=False,
            comment="进入 Embedding 前使用的规范化文本。",
        ),
        sa.Column("locale", sa.String(16), nullable=False, comment="语料语言，如 zh-CN。"),
        sa.Column("lifecycle_status", sa.String(16), nullable=False, comment=LIFECYCLE),
        sa.Column("source_ref", sa.String(255), nullable=False, comment="语料来源。"),
        sa.Column("approved_by", sa.String(64), comment="批准该语料的主体编号。"),
        sa.Column("approved_at", sa.DateTime(timezone=True), comment="语料批准时间。"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, comment="语料创建时间。"
        ),
        sa.ForeignKeyConstraint(
            ["intent_id", "intent_version"],
            ["content.intent_definition.intent_id", "content.intent_definition.version"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("intent_id", "intent_version", "normalized_text", "locale"),
        sa.CheckConstraint(
            "lifecycle_status IN ('DRAFT', 'IN_REVIEW', 'APPROVED', 'PUBLISHED', 'RETIRED')",
            name="lifecycle_status",
        ),
        schema="content",
        comment="动作意图的相似检索语料；PostgreSQL 是权威源，Milvus 只保存向量副本。",
    )
    op.create_table(
        "semantic_index_build",
        sa.Column("index_version", sa.String(64), primary_key=True, comment="本次索引构建版本。"),
        sa.Column(
            "embedding_profile_id",
            sa.String(64),
            nullable=False,
            comment="Embedding 模型及预处理配置编号。",
        ),
        sa.Column(
            "intent_collection",
            sa.String(128),
            nullable=False,
            comment="相似意图语料所在的 Milvus Collection。",
        ),
        sa.Column(
            "faq_collection",
            sa.String(128),
            nullable=False,
            comment="FAQ 问题变体所在的 Milvus Collection。",
        ),
        sa.Column(
            "manifest",
            JSONB(),
            nullable=False,
            comment="内容指纹、数量、任务指令和索引参数。",
        ),
        sa.Column(
            "built_at",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="两个 Collection 均完成写入的时间。",
        ),
        schema="content",
        comment="离线语义索引构建记录；用于追溯内容、模型配置和 Milvus Collection。",
    )


def downgrade() -> None:
    """删除语义索引记录和权威语料；Milvus Collection 需单独清理。"""

    op.drop_table("semantic_index_build", schema="content")
    op.drop_table("intent_similarity", schema="content")
