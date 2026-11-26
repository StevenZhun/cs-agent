"""JWT, RBAC and object-authorization boundaries owned by the BFF."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import secrets
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Annotated, Protocol
from uuid import uuid4

import jwt
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import ExpiredSignatureError, InvalidTokenError
from pydantic import Field, SecretStr, ValidationError, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from services.bff.errors import AppError
from shared.contracts.common import ContractModel


class Role(StrEnum):
    """Application roles used by authorization checks."""

    CUSTOMER = "CUSTOMER"
    AGENT = "AGENT"
    ADMIN = "ADMIN"


class AuthPrincipal(ContractModel):
    """Validated identity available to BFF routes and authorization ports."""

    subject_id: str = Field(min_length=1)
    roles: frozenset[Role]
    token_id: str = Field(min_length=1)


class LoginRequest(ContractModel):
    """Local application credentials; password is never returned or logged."""

    username: str = Field(min_length=1, max_length=128)
    password: SecretStr


class LoginResponse(ContractModel):
    """Short-lived bearer token issued after local credential verification."""

    access_token: str = Field(min_length=1)
    token_type: str = "Bearer"
    expires_in: int = Field(gt=0)
    principal: AuthPrincipal


@dataclass(frozen=True)
class StoredIdentity:
    """Minimal password-verification projection loaded from the app schema."""

    subject_id: str
    password_hash: str | None
    status: str
    roles: frozenset[Role]


class IdentityRepository(Protocol):
    """Application-owned identity lookup port."""

    async def find_by_username(self, username: str) -> StoredIdentity | None: ...


class ScryptPasswordService:
    """Versioned scrypt password hashes using only Python's standard library."""

    algorithm = "scrypt"
    n = 2**14
    r = 8
    p = 1
    salt_bytes = 16
    derived_key_bytes = 32

    def hash_password(self, password: str) -> str:
        if not password:
            raise ValueError("password must not be empty")
        salt = secrets.token_bytes(self.salt_bytes)
        derived = hashlib.scrypt(
            password.encode(),
            salt=salt,
            n=self.n,
            r=self.r,
            p=self.p,
            dklen=self.derived_key_bytes,
        )
        return "$".join(
            (
                self.algorithm,
                str(self.n),
                str(self.r),
                str(self.p),
                base64.urlsafe_b64encode(salt).decode(),
                base64.urlsafe_b64encode(derived).decode(),
            )
        )

    def verify_password(self, password: str, encoded_hash: str) -> bool:
        try:
            algorithm, n, r, p, salt_text, expected_text = encoded_hash.split("$")
            if algorithm != self.algorithm:
                return False
            salt = base64.urlsafe_b64decode(salt_text.encode())
            expected = base64.urlsafe_b64decode(expected_text.encode())
            actual = hashlib.scrypt(
                password.encode(),
                salt=salt,
                n=int(n),
                r=int(r),
                p=int(p),
                dklen=len(expected),
            )
        except (ValueError, TypeError, binascii.Error):
            return False
        return hmac.compare_digest(actual, expected)


class BffSecuritySettings(BaseSettings):
    """从根目录 `.env` 读取 JWT 配置，不在代码中保留密钥默认值。"""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str
    bff_jwt_secret: SecretStr
    bff_jwt_issuer: str
    bff_jwt_audience: str
    bff_jwt_ttl_seconds: int = Field(ge=60, le=86400)

    @model_validator(mode="after")
    def validate_jwt_secret(self) -> BffSecuritySettings:
        """拒绝长度不足的 JWT 密钥，避免误用明显弱口令。"""

        secret = self.bff_jwt_secret.get_secret_value()
        if len(secret) < 32:
            raise ValueError("BFF JWT secret must contain at least 32 characters")
        return self


class JwtTokenService:
    """Issue and validate local HS256 access tokens behind one replaceable port."""

    def __init__(self, settings: BffSecuritySettings) -> None:
        self._secret = settings.bff_jwt_secret.get_secret_value()
        self._issuer = settings.bff_jwt_issuer
        self._audience = settings.bff_jwt_audience
        self._ttl = timedelta(seconds=settings.bff_jwt_ttl_seconds)

    @property
    def ttl_seconds(self) -> int:
        return int(self._ttl.total_seconds())

    def issue_access_token(
        self,
        *,
        subject_id: str,
        roles: Iterable[Role],
        ttl: timedelta | None = None,
    ) -> str:
        """Issue a token for the future login use case; passwords stay elsewhere."""

        now = datetime.now(UTC)
        payload = {
            "sub": subject_id,
            "roles": [role.value for role in roles],
            "jti": f"token_{uuid4().hex}",
            "iss": self._issuer,
            "aud": self._audience,
            "iat": now,
            "exp": now + (ttl if ttl is not None else self._ttl),
        }
        return jwt.encode(payload, self._secret, algorithm="HS256")

    def decode_access_token(self, token: str) -> AuthPrincipal:
        """Validate signature and registered claims before constructing a principal."""

        try:
            claims = jwt.decode(
                token,
                self._secret,
                algorithms=["HS256"],
                issuer=self._issuer,
                audience=self._audience,
                options={"require": ["sub", "roles", "jti", "iat", "exp"]},
            )
            return AuthPrincipal(
                subject_id=claims["sub"],
                roles=frozenset(claims["roles"]),
                token_id=claims["jti"],
            )
        except ExpiredSignatureError as error:
            raise AppError(
                code="AUTH_TOKEN_EXPIRED",
                message="登录状态已过期",
                status_code=401,
            ) from error
        except (InvalidTokenError, KeyError, TypeError, ValidationError) as error:
            raise AppError(
                code="AUTH_TOKEN_INVALID",
                message="访问令牌无效",
                status_code=401,
            ) from error


class LocalAuthenticationService:
    """Verify local course credentials and issue a replaceable JWT session."""

    def __init__(
        self,
        repository: IdentityRepository,
        passwords: ScryptPasswordService,
        tokens: JwtTokenService,
    ) -> None:
        self._repository = repository
        self._passwords = passwords
        self._tokens = tokens
        self._dummy_hash = passwords.hash_password("dummy-password-for-timing-equalization")

    async def login(self, request: LoginRequest) -> LoginResponse:
        identity = await self._repository.find_by_username(request.username)
        encoded_hash = (
            identity.password_hash
            if identity is not None and identity.password_hash is not None
            else self._dummy_hash
        )
        password_matches = self._passwords.verify_password(
            request.password.get_secret_value(), encoded_hash
        )
        if identity is None or identity.status != "ACTIVE" or not password_matches:
            raise AppError(
                code="AUTH_CREDENTIALS_INVALID",
                message="用户名或密码错误",
                status_code=401,
            )
        if not identity.roles:
            raise AppError(
                code="AUTH_ROLE_MISSING",
                message="账号尚未分配角色",
                status_code=403,
            )

        principal = AuthPrincipal(
            subject_id=identity.subject_id,
            roles=identity.roles,
            token_id="pending",
        )
        access_token = self._tokens.issue_access_token(
            subject_id=principal.subject_id,
            roles=principal.roles,
        )
        decoded_principal = self._tokens.decode_access_token(access_token)
        return LoginResponse(
            access_token=access_token,
            expires_in=self._tokens.ttl_seconds,
            principal=decoded_principal,
        )


class ObjectAuthorizationPort(Protocol):
    """Read application-owned subject-to-object bindings without trusting JWT scope lists."""

    async def can_access_customer(self, subject_id: str, customer_id: str) -> bool: ...

    async def can_access_conversation(self, subject_id: str, conversation_id: str) -> bool: ...


class DenyAllObjectAuthorizer:
    """Safe default until the PostgreSQL app-schema repository is connected."""

    async def can_access_customer(self, subject_id: str, customer_id: str) -> bool:
        return False

    async def can_access_conversation(self, subject_id: str, conversation_id: str) -> bool:
        return False


bearer = HTTPBearer(auto_error=False)


async def get_current_principal(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> AuthPrincipal:
    """Resolve a bearer token through the app-owned token service."""

    if credentials is None or credentials.scheme.lower() != "bearer":
        raise AppError(
            code="AUTH_REQUIRED",
            message="需要登录后访问",
            status_code=401,
        )
    token_service: JwtTokenService = request.app.state.token_service
    return token_service.decode_access_token(credentials.credentials)


def authorize_roles(principal: AuthPrincipal, allowed_roles: Iterable[Role]) -> None:
    """Require at least one explicit application role."""

    allowed = frozenset(allowed_roles)
    if not principal.roles.intersection(allowed):
        raise AppError(
            code="AUTH_FORBIDDEN",
            message="当前角色无权执行此操作",
            status_code=403,
        )


async def authorize_customer(
    principal: AuthPrincipal,
    customer_id: str,
    authorizer: ObjectAuthorizationPort,
) -> None:
    """Check the current binding instead of trusting object IDs embedded in a token."""

    if not await authorizer.can_access_customer(principal.subject_id, customer_id):
        raise AppError(
            code="OBJECT_ACCESS_DENIED",
            message="无权访问该客户对象",
            status_code=403,
        )
