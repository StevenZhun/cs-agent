"""客户资料适配器对外提供的通用请求与响应模型。"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field, model_validator

from shared.contracts.common import ContractModel
from shared.contracts.enums import DataSource, TicketPriority, TicketStatus


class CustomerProfile(ContractModel):
    """坐席工作台使用的最小脱敏客户资料。"""

    customer_id: str = Field(min_length=1, max_length=64)
    display_name: str = Field(min_length=1, max_length=128)
    masked_contact: str = Field(min_length=1, max_length=64)
    service_level: str = Field(min_length=1, max_length=32)
    status: Literal["ACTIVE", "INACTIVE"]
    tags: tuple[str, ...] = ()
    data_source: DataSource = DataSource.CUSTOMER_DATA


class ServiceRecord(ContractModel):
    """客户数据系统中可供坐席查看的一条通用服务记录。"""

    record_id: str = Field(min_length=1, max_length=64)
    customer_id: str = Field(min_length=1, max_length=64)
    category: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=128)
    status: Literal["OPEN", "PENDING", "RESOLVED", "CLOSED"]
    reference_code: str | None = Field(default=None, max_length=64)
    updated_at: datetime
    data_source: DataSource = DataSource.CUSTOMER_DATA


class CustomerServiceContext(ContractModel):
    """BFF 聚合的客户资料与通用服务记录视图。"""

    customer: CustomerProfile
    records: tuple[ServiceRecord, ...] = ()

    @model_validator(mode="after")
    def validate_customer_scope(self) -> CustomerServiceContext:
        """所有服务记录必须属于当前客户，防止跨客户资料混入。"""

        if any(record.customer_id != self.customer.customer_id for record in self.records):
            raise ValueError("服务记录不属于当前客户")
        return self


class CreateTicketDraftRequest(ContractModel):
    """AI 建议创建但仍需坐席确认的工单草稿请求。"""

    conversation_id: str = Field(min_length=1)
    customer_id: str = Field(min_length=1)
    ticket_type: str = Field(pattern=r"^[A-Z][A-Z0-9_]+$")
    title: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    priority: TicketPriority
    assigned_group: str | None = None


class Ticket(ContractModel):
    """工单系统保存并返回的通用工单事实。"""

    ticket_id: str = Field(min_length=1)
    conversation_id: str = Field(min_length=1)
    customer_id: str = Field(min_length=1)
    ticket_type: str = Field(pattern=r"^[A-Z][A-Z0-9_]+$")
    title: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    priority: TicketPriority
    status: TicketStatus
    assigned_group: str | None = None
    created_by: str = Field(min_length=1)
    created_at: datetime
    updated_at: datetime
    data_source: DataSource = DataSource.TICKET_SYSTEM
