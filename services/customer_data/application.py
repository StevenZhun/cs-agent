"""通用客户资料查询模块使用的最小方法约定。"""

from __future__ import annotations

from typing import Protocol

from shared.contracts.business import CustomerProfile, CustomerServiceContext


class CustomerDataQueryPort(Protocol):
    """内部 HTTP 适配器读取客户资料所需的接口。"""

    async def get_customer(self, customer_id: str) -> CustomerProfile | None: ...

    async def list_customers(
        self, customer_ids: tuple[str, ...]
    ) -> tuple[CustomerProfile, ...]: ...

    async def get_customer_context(self, customer_id: str) -> CustomerServiceContext | None: ...
