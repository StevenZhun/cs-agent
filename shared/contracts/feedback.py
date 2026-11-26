"""推荐交互事件与坐席显式反馈契约。"""

from __future__ import annotations

from datetime import datetime

from pydantic import Field, model_validator

from shared.contracts.common import ContractModel
from shared.contracts.enums import AdoptionStatus, Helpfulness, InteractionAction


class RecommendationInteractionEvent(ContractModel):
    """查看、复制等可观察但不代表采用的推荐交互。"""

    interaction_id: str = Field(min_length=1)
    request_id: str = Field(min_length=1)
    recommendation_id: str = Field(min_length=1)
    conversation_id: str = Field(min_length=1)
    turn_id: str = Field(min_length=1)
    actor_id: str = Field(min_length=1)
    action: InteractionAction
    created_at: datetime


class RecommendationFeedback(ContractModel):
    """坐席明确提交的采用与帮助程度反馈。"""

    feedback_id: str = Field(min_length=1)
    request_id: str = Field(min_length=1)
    recommendation_id: str = Field(min_length=1)
    conversation_id: str = Field(min_length=1)
    turn_id: str = Field(min_length=1)
    trace_id: str = Field(min_length=1)
    actor_id: str = Field(min_length=1)
    adoption_status: AdoptionStatus = AdoptionStatus.NOT_RATED
    helpfulness: Helpfulness = Helpfulness.NOT_RATED
    result_message_id: str | None = None
    edit_summary: str | None = None
    reason_codes: tuple[str, ...] = ()
    comment: str | None = None
    created_at: datetime

    @model_validator(mode="after")
    def validate_adoption_details(self) -> RecommendationFeedback:
        """修改后采用必须引用最终消息并说明必要差异。"""

        if self.adoption_status is AdoptionStatus.ADOPTED_AFTER_EDIT and (
            not self.result_message_id or not self.edit_summary
        ):
            raise ValueError("ADOPTED_AFTER_EDIT requires result_message_id and edit_summary")
        return self
