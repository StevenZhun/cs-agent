"""增加 FAQ 发布记录和已放行推荐正文。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0013_faq_runtime"
down_revision: str | None = "0012_content_domain_rule"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """保存 FAQ 发布清单，并让坐席能够读取真实推荐内容。"""

    op.create_table(
        "faq_release",
        sa.Column(
            "release_version",
            sa.String(64),
            primary_key=True,
            comment="不可变的 FAQ 发布版本号。",
        ),
        sa.Column(
            "source_ref",
            sa.String(255),
            nullable=False,
            comment="本次发布使用的 FAQ 内容来源。",
        ),
        sa.Column(
            "manifest",
            JSONB(),
            nullable=False,
            comment="审核人、内容指纹、条目和 Redis 副本清单。",
        ),
        sa.Column(
            "published_at",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="本次 FAQ 发布完成时间。",
        ),
        schema="content",
        comment="FAQ 发布清单；Redis 丢失后可按该清单从 PostgreSQL 重建。",
    )
    op.add_column(
        "recommendation_record",
        sa.Column(
            "payload",
            JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
            comment="通过契约校验的完整 RecommendationResult；旧演示记录可能为空对象。",
        ),
        schema="app",
    )
    op.alter_column("recommendation_record", "payload", schema="app", server_default=None)
    op.execute(
        "COMMENT ON TABLE app.recommendation_record IS "
        "'已放行推荐、所属轮次及前端读取的结构化内容。'"
    )


def downgrade() -> None:
    """仅供本地未发布数据回退；先删除新增列再删除 FAQ 发布表。"""

    op.drop_column("recommendation_record", "payload", schema="app")
    op.drop_table("faq_release", schema="content")
