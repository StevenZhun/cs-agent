"""业务会话入口与前端消息 DTO。"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import Field, model_validator

from shared.contracts.common import ContractModel, RawContractModel
from shared.contracts.enums import (
    ConversationModelProfile,
    ConversationStatus,
    DecisionMode,
    MessageRole,
    TurnStatus,
)


class TurnTokenUsage(ContractModel):
    """当前会话上下文周期截至最近一轮的累计模型 Token 用量。"""

    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    context_window_tokens: int = Field(ge=1)
    used_percentage: float = Field(ge=0)


class CreateConversationRequest(ContractModel):
    """坐席开始一次业务会话时提交的请求。"""

    customer_id: str = Field(min_length=1)
    channel: str = Field(default="WEB", min_length=1)


class CreateConversationResponse(ContractModel):
    """BFF 创建业务会话后的稳定前端响应。"""

    conversation_id: str = Field(min_length=1)
    status: ConversationStatus = ConversationStatus.ACTIVE
    created_at: datetime


class ConversationSummary(ContractModel):
    """最近咨询列表展示所需的稳定业务字段。"""

    conversation_id: str = Field(min_length=1)
    title: str = Field(min_length=1, max_length=30)
    is_pinned: bool = False
    customer_id: str = Field(min_length=1)
    channel: str = Field(min_length=1)
    status: ConversationStatus
    needs_manual_review: bool = Field(
        default=False,
        description="此会话最新已完成轮次的 SOP 是否等待人工核对；不改变会话本身的状态。",
    )
    created_at: datetime
    updated_at: datetime
    last_message_at: datetime | None = Field(
        default=None,
        description="最后一条客户、助手或人工消息的时间；无消息时为空，不使用结束小结时间。",
    )


class RenameConversationRequest(ContractModel):
    """客户为一条咨询设置便于自己识别的标题。"""

    title: str = Field(min_length=1, max_length=30)


class CustomerConversationWorkload(ContractModel):
    """坐席客户列表的真实会话计数；待办是待核对和小结待确认的合计。"""

    customer_id: str
    open_count: int = Field(ge=0)
    waiting_count: int = Field(ge=0)


class UpdateConversationPinRequest(ContractModel):
    """当前登录主体设置或取消自己的会话置顶。"""

    pinned: bool


class SendMessageRequest(RawContractModel):
    """向已有业务会话追加一条保留原始字符的文本消息。"""

    message_id: str = Field(min_length=1, max_length=64)
    content: str = Field(min_length=1, max_length=4000)
    sent_at: datetime
    conversation_model_profile: ConversationModelProfile = ConversationModelProfile.DEFAULT
    decision_mode: DecisionMode = DecisionMode.LLM

    @model_validator(mode="after")
    def validate_non_blank_content(self) -> SendMessageRequest:
        """允许保留首尾空白，但拒绝只有空白的消息。"""

        if not self.content.strip():
            raise ValueError("content must contain non-whitespace content")
        if any(ord(character) < 32 and character not in "\n\r\t" for character in self.content):
            raise ValueError("content contains unsupported control characters")
        return self


class ModelSelectionOption(ContractModel):
    """前端可以展示和提交的一个服务端受控模型选项。"""

    value: str = Field(min_length=1, max_length=64)
    label: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=1, max_length=240)
    enabled: bool = True


class ModelSelectionCatalog(ContractModel):
    """客户页模型菜单的受控目录，不向前端暴露密钥或供应商地址。"""

    conversation_models: tuple[ModelSelectionOption, ...]
    decision_models: tuple[ModelSelectionOption, ...]
    default_conversation_model: ConversationModelProfile
    default_decision_mode: DecisionMode


class ConversationMessage(RawContractModel):
    """经过授权、脱敏后向前端展示的一条会话消息。"""

    message_id: str = Field(min_length=1)
    conversation_id: str = Field(min_length=1)
    turn_id: str = Field(min_length=1)
    role: MessageRole
    content: str = Field(min_length=1)
    created_at: datetime

    @model_validator(mode="after")
    def validate_non_blank_content(self) -> ConversationMessage:
        """保留原始字符，同时拒绝空白消息。"""

        if not self.content.strip():
            raise ValueError("content must contain non-whitespace content")
        return self


class CustomerTurnResult(ContractModel):
    """客户轮次处理结果；完成答复或人工等待提示都必须先保存消息再返回。"""

    conversation_id: str = Field(min_length=1)
    turn_id: str = Field(min_length=1)
    customer_message_id: str = Field(min_length=1)
    status: TurnStatus
    assistant_message: ConversationMessage | None = None
    token_usage: TurnTokenUsage | None = None

    @model_validator(mode="after")
    def validate_completion(self) -> CustomerTurnResult:
        """人工中断也有已保存的提示；仍在执行或失败时不能夹带未放行的回复。"""
        if (self.status in {TurnStatus.COMPLETED, TurnStatus.INTERRUPTED}) != (
            self.assistant_message is not None
        ):
            raise ValueError("已完成或等待人工处理的轮次必须有一条已保存的助手消息")
        return self


class RealtimeEventType(StrEnum):
    """Transport notifications; the app-owned message table remains authoritative."""

    MESSAGE_CREATED = "MESSAGE_CREATED"
    CONVERSATION_UPDATED = "CONVERSATION_UPDATED"
    STATE_SNAPSHOT = "STATE_SNAPSHOT"


class RealtimeEventEnvelope(ContractModel):
    """为排序和断线补发预留的 SSE/WS 事件信封。"""

    event_id: str = Field(min_length=1)
    sequence: int = Field(ge=1)
    conversation_id: str = Field(min_length=1)
    turn_id: str | None = None
    event_type: RealtimeEventType
    occurred_at: datetime
    payload: dict[str, object]
