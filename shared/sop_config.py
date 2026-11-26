"""已发布 SOP 的通用配置、字段校验和固定回复渲染。"""

from __future__ import annotations

import hashlib
import json
import re
from decimal import Decimal, InvalidOperation
from typing import Literal

from pydantic import Field, model_validator
from redis.asyncio import Redis

from shared.contracts.common import ContractModel
from shared.contracts.enums import SopExecutionStatus
from shared.contracts.sop import PendingSopLookup, SopProgress

SOP_ACTIVE_KEY = "content:sop:active"


class SopFieldConfig(ContractModel):
    """描述一个由内容发布系统维护的流程字段。"""

    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    label: str = Field(min_length=1, max_length=40)
    kind: Literal["CHOICE", "TEXT", "IDENTIFIER", "MONEY"]
    prompt: str = Field(min_length=1, max_length=250)
    choices: dict[str, str] = Field(default_factory=dict)
    aliases: dict[str, str] = Field(default_factory=dict)
    required_when: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    depends_on: tuple[str, ...] = ()
    min_value: str | None = None
    max_value: str | None = None
    lookup_capability: str | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9_]*$", max_length=64)
    lookup_mode: Literal["READ_ONLY"] | None = None
    prefetch_eligible: bool = False
    history_recall_mode: Literal["DISABLED", "CANDIDATE_CONFIRMATION"] = "DISABLED"

    @model_validator(mode="after")
    def validate_lookup_policy(self) -> SopFieldConfig:
        """只允许显式发布的只读查询能力参与候选补全。"""

        if bool(self.lookup_capability) != bool(self.lookup_mode):
            raise ValueError("SOP 字段查询能力和模式必须同时配置")
        if self.prefetch_eligible and not self.lookup_capability:
            raise ValueError("只有已发布只读查询能力的字段可以受控预取")
        if self.prefetch_eligible and self.required_when:
            raise ValueError("依赖前序选择的字段不能受控预取")
        if self.history_recall_mode != "DISABLED" and self.lookup_capability:
            raise ValueError("权威系统可查询字段不能用聊天历史替代")
        return self


class SopConfig(ContractModel):
    """一个不可变版本的通用流程定义。"""

    sop_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$", max_length=64)
    version: str = Field(min_length=1, max_length=32)
    intent_id: str = Field(pattern=r"^[A-Z][A-Z0-9_]*$", max_length=64)
    intent_name: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=128)
    executor: Literal["FORM"] = "FORM"
    entry_phrases: tuple[str, ...]
    negative_phrases: tuple[str, ...]
    fields: tuple[SopFieldConfig, ...]
    cancel_phrases: tuple[str, ...]
    resume_phrases: tuple[str, ...]
    confirm_phrases: tuple[str, ...]
    human_phrases: tuple[str, ...]
    switch_phrases: tuple[str, ...]
    notices: tuple[dict[str, object], ...] = ()
    stop_if: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    messages: dict[str, str]
    collection_notice: str = ""
    tools: tuple[str, ...] = ()
    tool_messages: dict[str, str] = Field(default_factory=dict)
    fact_labels: dict[str, str] = Field(default_factory=dict)
    retry_phrases: tuple[str, ...] = ()
    lookup_messages: dict[str, str] = Field(default_factory=dict)
    lookup_strategy: Literal["ON_DEMAND", "CONTROLLED_PREFETCH"] = "ON_DEMAND"
    lookup_reselection_enabled: bool = False
    lookup_reselection_limit: int = Field(default=3, ge=0, le=3)

    @model_validator(mode="after")
    def validate_definition(self) -> SopConfig:
        """在发布前检查字段依赖、消息出口和安全边界。"""

        names = [field.name for field in self.fields]
        if not names or len(names) != len(set(names)) or not self.entry_phrases:
            raise ValueError("SOP 必须有唯一字段和明确入口")
        if any(name in {"password", "secret", "token", "full_identity_number"} for name in names):
            raise ValueError("禁止在普通对话中收集秘密信息")
        required_messages = {
            "confirm",
            "invalid",
            "cancelled",
            "human",
            "ready",
            "manual_review",
            "sensitive",
            "unsupported",
            "prerequisite",
        }
        if set(self.messages) != required_messages or any(
            not value for value in self.messages.values()
        ):
            raise ValueError("流程消息必须包含确认、取消和人工出口")
        if not self.confirm_phrases or not self.cancel_phrases or not self.human_phrases:
            raise ValueError("流程必须有确认、取消和转人工入口")
        if self.tools:
            raise ValueError("公开默认流程不启用行业业务工具")
        if (
            self.lookup_messages
            or self.lookup_reselection_enabled
            or self.lookup_strategy != "ON_DEMAND"
        ):
            raise ValueError("未发布查询能力时不能启用候选查询策略")

        for field in self.fields:
            if any(key not in names for key in field.required_when):
                raise ValueError("条件必填引用未知字段")
            if any(key not in names for key in field.depends_on):
                raise ValueError("字段依赖引用未知字段")
            if any(names.index(key) >= names.index(field.name) for key in field.depends_on):
                raise ValueError("字段只能依赖排在前面的字段")
            for key, allowed in field.required_when.items():
                dependency = self.fields[names.index(key)]
                if names.index(key) >= names.index(field.name):
                    raise ValueError("条件必填只能引用排在前面的字段")
                if (
                    not allowed
                    or dependency.kind != "CHOICE"
                    or any(value not in dependency.choices for value in allowed)
                ):
                    raise ValueError("条件必填引用无效选项")
            if field.kind == "CHOICE" and (
                not field.choices
                or any(value not in field.choices for value in field.aliases.values())
            ):
                raise ValueError("选项及别名映射不合法")
            if field.kind != "CHOICE" and (field.choices or field.aliases):
                raise ValueError("只有选择字段可以发布选项和别名")
            for bound in (field.min_value, field.max_value):
                try:
                    if bound is not None and not Decimal(bound).is_finite():
                        raise ValueError("数值边界必须为有限数值")
                except InvalidOperation as exc:
                    raise ValueError("数值边界必须为数字") from exc
            if (
                field.min_value
                and field.max_value
                and Decimal(field.min_value) > Decimal(field.max_value)
            ):
                raise ValueError("数值边界倒置")
        if any(key not in names for key in self.stop_if):
            raise ValueError("停止条件引用未知字段")
        return self

    def required_fields(self, values: dict[str, str]) -> tuple[SopFieldConfig, ...]:
        """按发布顺序返回当前分支需要收集的字段。"""

        return tuple(
            field for field in self.fields if conditions_match(field.required_when, values)
        )


def conditions_match(conditions: dict[str, object], values: dict[str, str]) -> bool:
    """只解释有限值条件，不执行内容库中的代码或表达式。"""

    return all(values.get(name) in allowed for name, allowed in conditions.items())


def valid_sop_value(field: SopFieldConfig, value: object) -> bool:
    """校验字段格式；通过格式校验不代表业务事实已经核验。"""

    if not isinstance(value, str):
        return False
    value = value.strip()
    if field.kind == "CHOICE":
        return value in field.choices
    if field.kind == "TEXT":
        return 2 <= len(value) <= 500 and not any(
            ord(char) < 32 and char not in "\n\t" for char in value
        )
    if field.kind == "IDENTIFIER":
        return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{2,63}", value))
    if field.kind == "MONEY":
        if not re.fullmatch(r"\d{1,12}(?:\.\d{1,2})?", value):
            return False
        number = Decimal(value)
        return (field.min_value is None or number >= Decimal(field.min_value)) and (
            field.max_value is None or number <= Decimal(field.max_value)
        )
    return False


def sop_release_key(version: str) -> str:
    """返回不可变 SOP 发布快照的 Redis 键。"""

    return f"content:sop:release:{version}"


def sop_content_hash(definitions: dict[str, object]) -> str:
    """对发布内容做稳定编码，阻止同名版本被静默改写。"""

    encoded = json.dumps(definitions, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


async def load_sop_release(redis: Redis | None, version: str | None = None) -> dict[str, object]:
    """读取指定版或当前版配置；缓存缺失时不使用代码内业务配置兜底。"""

    if redis is None:
        return {}
    if version is None:
        active = await redis.get(SOP_ACTIVE_KEY)
        if active is None:
            return {}
        version = active.decode() if isinstance(active, bytes) else str(active)
    raw = await redis.get(sop_release_key(version))
    if raw is None:
        return {}
    release = json.loads(raw)
    if not isinstance(release, dict) or not isinstance(release.get("definitions"), dict):
        raise ValueError("SOP 发布快照缺少配置集合")
    if release.get("release_version") != version or sop_content_hash(
        release["definitions"]
    ) != release.get("content_hash"):
        raise ValueError("SOP 发布配置校验值不一致")
    for sop_id, payload in release["definitions"].items():
        if SopConfig.model_validate(payload).sop_id != sop_id:
            raise ValueError("SOP 配置编号与快照索引不一致")
    return release


def format_sop_text(text: str) -> str:
    """把模板中的完整句子和并列事项分行，便于聊天窗口阅读。"""

    formatted = text.replace("；", "\n").replace("。", "。\n\n").strip()
    return re.sub(r"\n{3,}", "\n\n", formatted)


def render_sop_lookup_message(
    config: SopConfig,
    progress: SopProgress,
    reason: str,
    pending: PendingSopLookup | None = None,
) -> str:
    """渲染只读查询结果；公开默认流程未启用时会明确拒绝调用。"""

    del config, progress, reason, pending
    raise ValueError("当前 SOP 未发布只读候选查询能力")


def render_sop_message(
    config: SopConfig,
    progress: SopProgress,
    reason: str,
    *,
    include_notice: bool = False,
) -> str:
    """根据已发布配置和当前进度生成确定性流程消息。"""

    if (progress.sop_id, progress.sop_version) != (config.sop_id, config.version):
        raise ValueError("配置版本与流程进度不一致")
    slots = (*progress.candidate_slots, *progress.confirmed_slots)
    values = {slot.name: str(slot.value) for slot in slots}
    fields = {field.name: field for field in config.fields}
    if len(values) != len(slots) or any(
        name not in fields or not valid_sop_value(fields[name], value)
        for name, value in values.items()
    ):
        raise ValueError("槽位重复、未知或格式不合法")
    required = config.required_fields(values)
    missing = tuple(field.name for field in required if field.name not in values)
    if missing != progress.missing_slots:
        raise ValueError("槽位与当前流程分支不一致")

    if reason == "SOP_CANCELLED" and progress.status is SopExecutionStatus.CANCELLED:
        return format_sop_text(config.messages["cancelled"])
    if progress.status is SopExecutionStatus.SUSPENDED:
        message_key = {
            "SOP_HUMAN_REQUESTED": "human",
            "SOP_MANUAL_REVIEW": "manual_review",
            "SOP_SENSITIVE_REVIEW": "sensitive",
            "SOP_UNSUPPORTED_TYPE": "unsupported",
            "SOP_PREREQUISITE_REQUIRED": "prerequisite",
        }.get(reason)
        if message_key is None:
            raise ValueError("未知暂停原因")
        return format_sop_text(config.messages[message_key])
    if progress.status is not SopExecutionStatus.ACTIVE:
        raise ValueError("未知流程状态")
    if reason in {"SOP_COLLECT_SLOT", "SOP_INVALID_SLOT"} and missing:
        if progress.current_step_id != missing[0]:
            raise ValueError("追问步骤与首个缺失字段不一致")
        return format_sop_text(
            "\n\n".join(
                part
                for part in (
                    config.collection_notice if include_notice else "",
                    config.messages["invalid"] if reason == "SOP_INVALID_SLOT" else "",
                    fields[missing[0]].prompt,
                )
                if part
            )
        )
    if reason == "SOP_CONFIRM_SLOTS" and not missing:
        if progress.current_step_id != "confirm_slots" or not progress.candidate_slots:
            raise ValueError("确认步骤与候选槽位不一致")
        details = "\n".join(
            f"• {fields[name].label}：{fields[name].choices.get(value, value)}"
            for name, value in values.items()
        )
        return format_sop_text(config.messages["confirm"].replace("{details}", details))
    if reason == "SOP_MANUAL_REVIEW" and not missing and not progress.candidate_slots:
        return format_sop_text(config.messages["manual_review"])
    raise ValueError("流程状态与回复原因不一致")
