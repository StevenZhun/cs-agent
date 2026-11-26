"""通用客户资料适配器拥有的 PostgreSQL 模型。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from shared.persistence import PostgresBase
from shared.postgres_comments import apply_model_comments

Base = PostgresBase


class CustomerModel(Base):
    """与登录身份分离的脱敏客户资料。"""

    __tablename__ = "customer"
    __table_args__ = (
        CheckConstraint("status IN ('ACTIVE', 'INACTIVE')", name="status"),
        {"schema": "business"},
    )

    customer_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    contact_last4: Mapped[str] = mapped_column(String(4), nullable=False)
    service_level: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    tags: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    records: Mapped[list[ServiceRecordModel]] = relationship(back_populates="customer")


class ServiceRecordModel(Base):
    """任意行业都可映射的一条客户服务记录。"""

    __tablename__ = "service_record"
    __table_args__ = (
        CheckConstraint(
            "status IN ('OPEN', 'PENDING', 'RESOLVED', 'CLOSED')", name="status"
        ),
        Index("ix_service_record_customer_status", "customer_id", "status"),
        {"schema": "business"},
    )

    record_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    customer_id: Mapped[str] = mapped_column(
        ForeignKey("business.customer.customer_id", ondelete="CASCADE"), nullable=False
    )
    category: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    reference_code: Mapped[str | None] = mapped_column(String(64))
    details: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    customer: Mapped[CustomerModel] = relationship(back_populates="records")


apply_model_comments(Base.metadata, "business")
