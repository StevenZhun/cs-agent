"""跨服务接口模型共用的响应外壳与校验基类。"""

from __future__ import annotations

from datetime import datetime
from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field


class ContractModel(BaseModel):
    """跨服务数据模型基类，统一执行严格字段与字符串校验。"""

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        validate_assignment=True,
    )


class RawContractModel(BaseModel):
    """需要保留原始字符串字符的契约基类。"""

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=False,
        validate_assignment=True,
    )


class ResponseMeta(ContractModel):
    """每个接口响应都可携带的请求追踪和契约版本信息。"""

    request_id: str = Field(min_length=1)
    timestamp: datetime
    contract_version: str = "2.0"


T = TypeVar("T")


class ApiResponse(ContractModel, Generic[T]):
    """成功接口响应的通用泛型外壳。"""

    data: T
    meta: ResponseMeta


class ApiError(ContractModel):
    """跨服务统一错误响应。"""

    code: str = Field(pattern=r"^[A-Z][A-Z0-9_]+$")
    message: str = Field(min_length=1)
    request_id: str = Field(min_length=1)
    retryable: bool = False
    details: dict[str, object] = Field(default_factory=dict)
