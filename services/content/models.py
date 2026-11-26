"""PostgreSQL content schema 中意图目录和规则的权威版本。"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKeyConstraint,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from shared.persistence import PostgresBase


class IntentDefinitionModel(PostgresBase):
    """保存全局意图的一个版本；同一编号的新版本不会覆盖旧版本。"""

    __tablename__ = "intent_definition"
    __table_args__ = (
        CheckConstraint("intent_class IN ('ACTION', 'STRATEGY')", name="intent_class"),
        CheckConstraint(
            "intent_class <> 'STRATEGY' OR route_target = 'FAQ'",
            name="strategy_route_target",
        ),
        CheckConstraint(
            "request_kind <> 'HIGH_RISK' OR intent_class = 'STRATEGY'",
            name="high_risk_intent_class",
        ),
        CheckConstraint("route_target IN ('SCENE', 'FAQ')", name="route_target"),
        CheckConstraint(
            "request_kind IN ('KNOWLEDGE', 'ACTION', 'CONTROL', 'SOCIAL', "
            "'EXTERNAL_CAPABILITY', 'HIGH_RISK', 'UNSURE')",
            name="request_kind",
        ),
        CheckConstraint(
            "lifecycle_status IN ('DRAFT', 'IN_REVIEW', 'APPROVED', 'PUBLISHED', 'RETIRED')",
            name="lifecycle_status",
        ),
        {"schema": "content", "comment": "全局意图权威版本；历史版本不可原地覆盖。"},
    )

    intent_id: Mapped[str] = mapped_column(String(64), primary_key=True, comment="全局意图编号。")
    version: Mapped[str] = mapped_column(String(32), primary_key=True, comment="意图版本号。")
    name: Mapped[str] = mapped_column(
        String(128), nullable=False, comment="供管理和展示的意图名称。"
    )
    description: Mapped[str] = mapped_column(Text, nullable=False, comment="全局意图的业务定义。")
    intent_class: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        comment="ACTION=业务咨询或办理动作；STRATEGY=系统策略、安全或交互意图。",
    )
    route_target: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="SCENE=场景 SOP；FAQ=固定问答分组。"
    )
    request_kind: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="KNOWLEDGE=知识咨询；ACTION=办理动作；CONTROL=会话控制；SOCIAL=寒暄；"
        "EXTERNAL_CAPABILITY=外部能力；HIGH_RISK=高风险策略；UNSURE=无法确定。",
    )
    lifecycle_status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        comment="DRAFT=草稿；IN_REVIEW=审核中；APPROVED=已批准；PUBLISHED=已发布；RETIRED=已下线。",
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, comment="当前版本是否允许命中。")
    source_ref: Mapped[str] = mapped_column(
        String(255), nullable=False, comment="业务定义来源和发布归属。"
    )
    release_version: Mapped[str | None] = mapped_column(
        String(64), comment="发布后所属的不可变内容发布版本；未发布时为空。"
    )
    approved_by: Mapped[str | None] = mapped_column(String(64), comment="批准该版本的主体编号。")
    approved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), comment="该版本批准时间。"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="版本创建时间。"
    )


class IntentRuleModel(PostgresBase):
    """保存一条带作用域和否定条件的高精度规则版本。"""

    __tablename__ = "intent_rule"
    __table_args__ = (
        ForeignKeyConstraint(
            ["intent_id", "intent_version"],
            ["content.intent_definition.intent_id", "content.intent_definition.version"],
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "rule_kind IN ('INTENT', 'CONTROL', 'CAPABILITY', 'DOMAIN')", name="rule_kind"
        ),
        CheckConstraint("match_mode IN ('EXACT', 'CONTAINS')", name="match_mode"),
        CheckConstraint("priority >= 0", name="priority_non_negative"),
        CheckConstraint(
            "lifecycle_status IN ('DRAFT', 'IN_REVIEW', 'APPROVED', 'PUBLISHED', 'RETIRED')",
            name="lifecycle_status",
        ),
        CheckConstraint(
            "(rule_kind = 'INTENT' AND intent_id IS NOT NULL AND intent_version IS NOT NULL) "
            "OR (rule_kind <> 'INTENT' AND intent_id IS NULL AND intent_version IS NULL)",
            name="intent_binding",
        ),
        CheckConstraint(
            "(rule_kind = 'CONTROL' AND control_action IN ('CANCEL', 'HUMAN')) "
            "OR (rule_kind <> 'CONTROL' AND control_action IS NULL)",
            name="control_action",
        ),
        {"schema": "content", "comment": "高精度关键词、控制命令和能力边界规则的权威版本。"},
    )

    rule_id: Mapped[str] = mapped_column(String(64), primary_key=True, comment="规则编号。")
    version: Mapped[str] = mapped_column(String(32), primary_key=True, comment="规则版本号。")
    rule_kind: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        comment="INTENT=全局意图；CONTROL=会话控制；CAPABILITY=未接能力；DOMAIN=域内知识范围。",
    )
    intent_id: Mapped[str | None] = mapped_column(String(64), comment="INTENT 规则指向的全局意图。")
    intent_version: Mapped[str | None] = mapped_column(
        String(32), comment="INTENT 规则指向的意图版本。"
    )
    pattern: Mapped[str] = mapped_column(Text, nullable=False, comment="待精确匹配的规范化短语。")
    match_mode: Mapped[str] = mapped_column(
        String(16), nullable=False, default="EXACT", comment="EXACT=整句匹配；CONTAINS=包含短语。"
    )
    negative_patterns: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, comment="命中后用于排除误判的否定短语数组。"
    )
    priority: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="规则优先级；越大越先判定。"
    )
    product_codes: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, comment="允许的产品代码数组；空数组表示不限制产品。"
    )
    channels: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, comment="允许的渠道代码数组；空数组表示不限制渠道。"
    )
    regions: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, comment="允许的政策地区数组；空数组表示不限制地区。"
    )
    required_capability: Mapped[str | None] = mapped_column(
        String(64), comment="CAPABILITY 规则标出的当前未接入能力，如 WEATHER。"
    )
    control_action: Mapped[str | None] = mapped_column(
        String(32), comment="CONTROL 规则对应的命令：CANCEL=取消；HUMAN=转人工。"
    )
    lifecycle_status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        comment="DRAFT=草稿；IN_REVIEW=审核中；APPROVED=已批准；PUBLISHED=已发布；RETIRED=已下线。",
    )
    source_ref: Mapped[str] = mapped_column(
        String(255), nullable=False, comment="规则来源和审核依据。"
    )
    release_version: Mapped[str | None] = mapped_column(
        String(64), comment="发布后所属的不可变内容发布版本；未发布时为空。"
    )
    approved_by: Mapped[str | None] = mapped_column(String(64), comment="批准该规则的主体编号。")
    approved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), comment="该规则批准时间。"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="规则版本创建时间。"
    )


class IntentSimilarityModel(PostgresBase):
    """保存一条已审核、可用于全局意图语义检索的语料版本。"""

    __tablename__ = "intent_similarity"
    __table_args__ = (
        ForeignKeyConstraint(
            ["intent_id", "intent_version"],
            ["content.intent_definition.intent_id", "content.intent_definition.version"],
            ondelete="RESTRICT",
        ),
        UniqueConstraint("intent_id", "intent_version", "normalized_text", "locale"),
        CheckConstraint(
            "lifecycle_status IN ('DRAFT', 'IN_REVIEW', 'APPROVED', 'PUBLISHED', 'RETIRED')",
            name="lifecycle_status",
        ),
        {
            "schema": "content",
            "comment": "全局意图的相似检索语料；PostgreSQL 是权威源，Milvus 只保存向量副本。",
        },
    )

    similarity_id: Mapped[str] = mapped_column(
        String(64), primary_key=True, comment="相似意图语料编号。"
    )
    version: Mapped[str] = mapped_column(String(32), primary_key=True, comment="语料版本号。")
    intent_id: Mapped[str] = mapped_column(String(64), nullable=False, comment="所属全局意图编号。")
    intent_version: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="所属全局意图版本。"
    )
    text: Mapped[str] = mapped_column(Text, nullable=False, comment="审核时阅读的语料原文。")
    normalized_text: Mapped[str] = mapped_column(
        Text, nullable=False, comment="进入 Embedding 前使用的规范化文本。"
    )
    locale: Mapped[str] = mapped_column(String(16), nullable=False, comment="语料语言，如 zh-CN。")
    lifecycle_status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        comment="DRAFT=草稿；IN_REVIEW=审核中；APPROVED=已批准；PUBLISHED=已建索引；RETIRED=已下线。",
    )
    source_ref: Mapped[str] = mapped_column(String(255), nullable=False, comment="语料来源。")
    approved_by: Mapped[str | None] = mapped_column(String(64), comment="批准该语料的主体编号。")
    approved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), comment="语料批准时间。"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="语料创建时间。"
    )


class FaqGroupModel(PostgresBase):
    """一个 FAQ 全局意图对应的版本化问题分组。"""

    __tablename__ = "faq_group"
    __table_args__ = (
        ForeignKeyConstraint(
            ["intent_id", "intent_version"],
            ["content.intent_definition.intent_id", "content.intent_definition.version"],
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "lifecycle_status IN ('DRAFT', 'IN_REVIEW', 'APPROVED', 'PUBLISHED', 'RETIRED')",
            name="lifecycle_status",
        ),
        {"schema": "content", "comment": "FAQ 全局意图下的权威问题分组版本。"},
    )

    faq_group_id: Mapped[str] = mapped_column(
        String(64), primary_key=True, comment="FAQ 分组编号。"
    )
    version: Mapped[str] = mapped_column(String(32), primary_key=True, comment="分组版本号。")
    intent_id: Mapped[str] = mapped_column(String(64), nullable=False, comment="所属全局意图编号。")
    intent_version: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="所属意图版本。"
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False, comment="分组展示名称。")
    lifecycle_status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        comment="DRAFT=草稿；IN_REVIEW=审核中；APPROVED=已批准；PUBLISHED=已发布；RETIRED=已下线。",
    )
    source_ref: Mapped[str] = mapped_column(String(255), nullable=False, comment="问题分组的来源。")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="创建时间。"
    )


class FaqItemModel(PostgresBase):
    """具体 FAQ 条目版本；发布前不得对客户直答。"""

    __tablename__ = "faq_item"
    __table_args__ = (
        ForeignKeyConstraint(
            ["faq_group_id", "group_version"],
            ["content.faq_group.faq_group_id", "content.faq_group.version"],
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "lifecycle_status IN ('DRAFT', 'IN_REVIEW', 'APPROVED', 'PUBLISHED', 'RETIRED')",
            name="lifecycle_status",
        ),
        {"schema": "content", "comment": "FAQ 问答条目的权威版本。"},
    )

    faq_item_id: Mapped[str] = mapped_column(String(64), primary_key=True, comment="FAQ 条目编号。")
    version: Mapped[str] = mapped_column(String(32), primary_key=True, comment="条目版本号。")
    faq_group_id: Mapped[str] = mapped_column(String(64), nullable=False, comment="所属 FAQ 分组。")
    group_version: Mapped[str] = mapped_column(String(32), nullable=False, comment="所属分组版本。")
    title: Mapped[str] = mapped_column(String(128), nullable=False, comment="条目标题。")
    lifecycle_status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        comment="DRAFT=草稿；IN_REVIEW=审核中；APPROVED=已批准；PUBLISHED=已发布；RETIRED=已下线。",
    )
    source_ref: Mapped[str] = mapped_column(String(255), nullable=False, comment="问答内容来源。")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="创建时间。"
    )


class FaqQuestionModel(PostgresBase):
    """FAQ 条目的一个已收录问法，不等于意图规则。"""

    __tablename__ = "faq_question"
    __table_args__ = (
        ForeignKeyConstraint(
            ["faq_item_id", "item_version"],
            ["content.faq_item.faq_item_id", "content.faq_item.version"],
            ondelete="RESTRICT",
        ),
        UniqueConstraint("faq_item_id", "item_version", "normalized_question", "locale"),
        {"schema": "content", "comment": "FAQ 条目下的精确或语义候选问法。"},
    )

    variant_id: Mapped[str] = mapped_column(String(64), primary_key=True, comment="问法变体编号。")
    faq_item_id: Mapped[str] = mapped_column(String(64), nullable=False, comment="所属 FAQ 条目。")
    item_version: Mapped[str] = mapped_column(String(32), nullable=False, comment="所属条目版本。")
    question: Mapped[str] = mapped_column(Text, nullable=False, comment="用户可读的问题原文。")
    normalized_question: Mapped[str] = mapped_column(
        Text, nullable=False, comment="用于精确键的规范化问题。"
    )
    exact_match_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, comment="是否允许投影到 Redis 精确索引。"
    )
    locale: Mapped[str] = mapped_column(String(16), nullable=False, comment="问法语言，如 zh-CN。")


class FaqAnswerModel(PostgresBase):
    """FAQ 条目的一版固定答案，保留可检查的适用范围。"""

    __tablename__ = "faq_answer"
    __table_args__ = (
        ForeignKeyConstraint(
            ["faq_item_id", "item_version"],
            ["content.faq_item.faq_item_id", "content.faq_item.version"],
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "effective_to IS NULL OR effective_from IS NULL OR effective_to >= effective_from",
            name="valid_time",
        ),
        {"schema": "content", "comment": "FAQ 固定答案的来源、适用范围和生效时间。"},
    )

    answer_id: Mapped[str] = mapped_column(String(64), primary_key=True, comment="答案版本编号。")
    faq_item_id: Mapped[str] = mapped_column(String(64), nullable=False, comment="所属 FAQ 条目。")
    item_version: Mapped[str] = mapped_column(String(32), nullable=False, comment="所属条目版本。")
    answer: Mapped[str] = mapped_column(Text, nullable=False, comment="经审核的固定答案正文。")
    product_codes: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, comment="适用产品代码；空数组表示不限。"
    )
    channels: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, comment="适用渠道；空数组表示不限。"
    )
    regions: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, comment="适用地区；空数组表示不限。"
    )
    effective_from: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), comment="答案生效时间；空值表示无下界。"
    )
    effective_to: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), comment="答案失效时间；空值表示无上界。"
    )
    source_ref: Mapped[str] = mapped_column(
        String(255), nullable=False, comment="固定答案来源或审核依据。"
    )


class IntentReleaseModel(PostgresBase):
    """记录一个已审核并投影到 Redis 的路由发布版本。"""

    __tablename__ = "intent_release"
    __table_args__ = (
        {
            "schema": "content",
            "comment": "意图目录和规则的发布清单；在线活动版本由 Redis 指针决定。",
        },
    )

    release_version: Mapped[str] = mapped_column(
        String(64), primary_key=True, comment="不可变的发布版本号。"
    )
    manifest: Mapped[dict[str, object]] = mapped_column(
        JSONB, nullable=False, comment="通过冲突检查的发布清单和资产引用。"
    )
    published_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        comment="审核发布入库时间；不代表当前仍是活动版本。",
    )


class FaqReleaseModel(PostgresBase):
    """记录一批已审核 FAQ 及其 Redis 精确查询副本。"""

    __tablename__ = "faq_release"
    __table_args__ = (
        {
            "schema": "content",
            "comment": "FAQ 发布清单；Redis 丢失后可按该清单从 PostgreSQL 重建。",
        },
    )

    release_version: Mapped[str] = mapped_column(
        String(64), primary_key=True, comment="不可变的 FAQ 发布版本号。"
    )
    source_ref: Mapped[str] = mapped_column(
        String(255), nullable=False, comment="本次发布使用的 FAQ 内容来源。"
    )
    manifest: Mapped[dict[str, object]] = mapped_column(
        JSONB, nullable=False, comment="审核人、内容指纹、条目和 Redis 副本清单。"
    )
    published_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="本次 FAQ 发布完成时间。"
    )


class PolicyAnswerModel(PostgresBase):
    """一个高风险策略意图对应的一版固定安全答复。"""

    __tablename__ = "policy_answer"
    __table_args__ = (
        ForeignKeyConstraint(
            ["intent_id", "intent_version"],
            ["content.intent_definition.intent_id", "content.intent_definition.version"],
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "lifecycle_status IN ('DRAFT', 'IN_REVIEW', 'APPROVED', 'PUBLISHED', 'RETIRED')",
            name="lifecycle_status",
        ),
        {
            "schema": "content",
            "comment": "高风险策略意图的固定答复；不经过普通 FAQ 条目与向量检索。",
        },
    )

    intent_id: Mapped[str] = mapped_column(
        String(64), primary_key=True, comment="唯一对应的高风险策略意图编号。"
    )
    version: Mapped[str] = mapped_column(
        String(64), primary_key=True, comment="不可变策略答案版本。"
    )
    intent_version: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="绑定的意图定义版本。"
    )
    answer_text: Mapped[str] = mapped_column(
        Text, nullable=False, comment="经审核的客户可见固定答复。"
    )
    answer_content_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="固定答复正文的 SHA-256。"
    )
    lifecycle_status: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="草稿、审核、发布或逻辑淘汰状态。"
    )
    source_ref: Mapped[str] = mapped_column(
        String(255), nullable=False, comment="策略答复来源和审核依据。"
    )
    release_version: Mapped[str | None] = mapped_column(
        String(64), comment="发布后所属的不可变策略答案发布版本。"
    )
    approved_by: Mapped[str | None] = mapped_column(String(64), comment="批准该答复的主体。")
    approved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), comment="答复批准时间。"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="答复版本创建时间。"
    )


class PolicyAnswerReleaseModel(PostgresBase):
    """保存整批已审核策略答案的不可变发布快照。"""

    __tablename__ = "policy_answer_release"
    __table_args__ = (
        {
            "schema": "content",
            "comment": "策略答案不可变发布快照；活动版本由 Redis 指针选择。",
        },
    )

    release_version: Mapped[str] = mapped_column(
        String(64), primary_key=True, comment="不可变发布版本号。"
    )
    source_ref: Mapped[str] = mapped_column(
        String(255), nullable=False, comment="本次发布使用的策略答案来源。"
    )
    payload: Mapped[dict[str, object]] = mapped_column(
        JSONB, nullable=False, comment="答案映射、内容哈希、审核者与发布时间。"
    )
    published_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="发布快照保存时间。"
    )


class PolicyParameterModel(PostgresBase):
    """项目构造的可引用业务口径；不冒充真实机构或监管规定。"""

    __tablename__ = "policy_parameter"
    __table_args__ = (
        CheckConstraint(
            "unit IN ('DAY', 'WORKDAY', 'AMOUNT', 'RATIO', 'COUNT', 'TEXT')",
            name="unit",
        ),
        CheckConstraint("source_kind = 'PROJECT_CONSTRUCTED'", name="source_kind"),
        CheckConstraint(
            "lifecycle_status IN ('DRAFT', 'IN_REVIEW', 'APPROVED', 'PUBLISHED', 'RETIRED')",
            name="lifecycle_status",
        ),
        CheckConstraint(
            "retired_at IS NULL OR effective_at <= retired_at",
            name="validity",
        ),
        CheckConstraint(
            "(unit = 'TEXT' AND value_numeric IS NULL) OR "
            "(unit <> 'TEXT' AND value_numeric IS NOT NULL)",
            name="numeric_value",
        ),
        CheckConstraint(
            "lifecycle_status IN ('DRAFT', 'IN_REVIEW') OR "
            "(approved_by IS NOT NULL AND approved_at IS NOT NULL)",
            name="approval",
        ),
        {
            "schema": "content",
            "comment": "项目构造口径的权威版本；供知识与 FAQ 引用，不是客户个人事实。",
        },
    )

    parameter_key: Mapped[str] = mapped_column(
        String(64), primary_key=True, comment="稳定口径键；正文只引用此键，不复制裸数字。"
    )
    version: Mapped[str] = mapped_column(
        String(32), primary_key=True, comment="不可变口径版本，采用 parameter-v* 形式。"
    )
    value_text: Mapped[str] = mapped_column(
        String(160), nullable=False, comment="面向客户可读的项目构造取值。"
    )
    value_numeric: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 6), comment="用于一致性校验的规范数值；TEXT 类型为空。"
    )
    unit: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="DAY/WORKDAY/AMOUNT/RATIO/COUNT/TEXT。"
    )
    scope: Mapped[dict[str, object]] = mapped_column(
        JSONB, nullable=False, comment="产品、渠道和地区适用范围；空数组表示不限制。"
    )
    effective_at: Mapped[date] = mapped_column(Date, nullable=False, comment="口径生效日期。")
    retired_at: Mapped[date | None] = mapped_column(Date, comment="口径失效日期；为空表示未设置。")
    source_kind: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="固定为 PROJECT_CONSTRUCTED，避免冒充真实制度。"
    )
    business_reference: Mapped[str | None] = mapped_column(
        String(255), comment="可选的外部业务字段引用，用于跨系统一致性校验。"
    )
    lifecycle_status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        comment="DRAFT=草稿；IN_REVIEW=审核中；APPROVED=已批准；PUBLISHED=已发布；RETIRED=已下线。",
    )
    source_ref: Mapped[str] = mapped_column(
        String(255), nullable=False, comment="作者数据来源和审核依据。"
    )
    approved_by: Mapped[str | None] = mapped_column(String(64), comment="批准该口径的主体。")
    approved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), comment="口径批准时间。"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="口径版本首次创建时间。"
    )


class SemanticIndexBuildModel(PostgresBase):
    """记录一次可复现的相似意图与 FAQ 向量索引构建结果。"""

    __tablename__ = "semantic_index_build"
    __table_args__ = (
        {
            "schema": "content",
            "comment": "离线语义索引构建记录；用于追溯内容、模型配置和 Milvus Collection。",
        },
    )

    index_version: Mapped[str] = mapped_column(
        String(64), primary_key=True, comment="本次索引构建版本。"
    )
    embedding_profile_id: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="Embedding 模型及预处理配置编号。"
    )
    intent_collection: Mapped[str] = mapped_column(
        String(128), nullable=False, comment="相似意图语料所在的 Milvus Collection。"
    )
    faq_collection: Mapped[str] = mapped_column(
        String(128), nullable=False, comment="FAQ 问题变体所在的 Milvus Collection。"
    )
    manifest: Mapped[dict[str, object]] = mapped_column(
        JSONB, nullable=False, comment="内容指纹、数量、任务指令和索引参数。"
    )
    built_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="两个 Collection 均完成写入的时间。"
    )


class SopDefinitionModel(PostgresBase):
    """保存 SOP 草稿及审核版本，已发布版本不能原地改写。"""

    __tablename__ = "sop_definition"
    __table_args__ = (
        ForeignKeyConstraint(
            ["intent_id", "intent_version"],
            ["content.intent_definition.intent_id", "content.intent_definition.version"],
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "lifecycle_status IN ('DRAFT', 'APPROVED', 'PUBLISHED', 'RETIRED')",
            name="lifecycle_status",
        ),
        {"schema": "content", "comment": "SOP 动作绑定、字段、条件分支与文案的权威配置版本。"},
    )
    sop_id: Mapped[str] = mapped_column(
        String(64), primary_key=True, comment="稳定的业务流程编号。"
    )
    version: Mapped[str] = mapped_column(
        String(32), primary_key=True, comment="不可变流程版本；更新发布需创建新版本。"
    )
    intent_id: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="绑定的 ACTION 类全局意图；必须指向 SCENE。"
    )
    intent_version: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="绑定意图定义的版本。"
    )
    config: Mapped[dict[str, object]] = mapped_column(
        JSONB,
        nullable=False,
        comment=(
            "经 SopConfig 校验的配置：字段名/标签/类型/选项、条件必填、命令、影响提示和固定回复。"
        ),
    )
    lifecycle_status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        comment="DRAFT=可编辑草稿；APPROVED=已核验批准；PUBLISHED=已发布；RETIRED=已下线。",
    )
    source_ref: Mapped[str] = mapped_column(
        String(255), nullable=False, comment="配置来源和发布归属。"
    )
    approved_by: Mapped[str | None] = mapped_column(
        String(64), comment="批准配置的操作主体；该记录不替代部署环境的业务审核。"
    )
    approved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), comment="批准时间，未批准时为空。"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="配置版本创建时间。"
    )


class SopReleaseModel(PostgresBase):
    """保存整批已审核配置快照，Redis 丢失时可重建指定发布版本。"""

    __tablename__ = "sop_release"
    __table_args__ = (
        {"schema": "content", "comment": "SOP 不可变发布快照与校验值；活动版本由 Redis 指针选择。"},
    )
    release_version: Mapped[str] = mapped_column(
        String(64), primary_key=True, comment="整批 SOP 发布版本，不能同名覆盖不同内容。"
    )
    payload: Mapped[dict[str, object]] = mapped_column(
        JSONB,
        nullable=False,
        comment="发布快照，包括 definitions、content_hash、审核者和发布时刻。",
    )
    published_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="发布快照保存时间。"
    )
