"""读取客户资料和服务记录，并转换为跨服务契约。"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from services.customer_data.infrastructure.models import CustomerModel, ServiceRecordModel
from shared.contracts.business import CustomerProfile, CustomerServiceContext, ServiceRecord


class SqlAlchemyCustomerDataRepository:
    """只在调用方已经授权的客户范围内读取通用客户数据。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get_customer(self, customer_id: str) -> CustomerProfile | None:
        """返回指定客户的脱敏资料。"""

        async with self._session_factory() as session:
            row = await session.get(CustomerModel, customer_id)
            return self._to_customer(row) if row is not None else None

    async def list_customers(self, customer_ids: tuple[str, ...]) -> tuple[CustomerProfile, ...]:
        """只返回调用方显式传入的客户集合。"""

        if not customer_ids:
            return ()
        async with self._session_factory() as session:
            rows = (
                await session.scalars(
                    select(CustomerModel)
                    .where(
                        CustomerModel.customer_id.in_(customer_ids),
                        CustomerModel.active.is_(True),
                    )
                    .order_by(CustomerModel.customer_id)
                )
            ).all()
            return tuple(self._to_customer(row) for row in rows)

    async def get_customer_context(self, customer_id: str) -> CustomerServiceContext | None:
        """聚合一个客户及其服务记录，不读取其他客户数据。"""

        async with self._session_factory() as session:
            customer = await session.get(CustomerModel, customer_id)
            if customer is None:
                return None
            records = (
                await session.scalars(
                    select(ServiceRecordModel)
                    .where(ServiceRecordModel.customer_id == customer_id)
                    .order_by(ServiceRecordModel.updated_at.desc(), ServiceRecordModel.record_id)
                )
            ).all()
            return CustomerServiceContext(
                customer=self._to_customer(customer),
                records=tuple(self._to_record(row) for row in records),
            )

    @staticmethod
    def _to_customer(row: CustomerModel) -> CustomerProfile:
        return CustomerProfile(
            customer_id=row.customer_id,
            display_name=row.display_name,
            masked_contact=f"*** **** {row.contact_last4}",
            service_level=row.service_level,
            status=row.status,
            tags=tuple(row.tags),
        )

    @staticmethod
    def _to_record(row: ServiceRecordModel) -> ServiceRecord:
        return ServiceRecord(
            record_id=row.record_id,
            customer_id=row.customer_id,
            category=row.category,
            title=row.title,
            status=row.status,
            reference_code=row.reference_code,
            updated_at=row.updated_at,
        )
