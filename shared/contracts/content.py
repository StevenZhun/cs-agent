"""权威内容版本、冲突检查和派生投影发布契约。"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import Field, model_validator

from shared.contracts.common import ContractModel
from shared.contracts.enums import (
    ContentAssetType,
    ContentConflictSeverity,
    ContentConflictStatus,
    ContentLifecycleStatus,
    ProjectionTarget,
    PublicationStatus,
)

ASSET_REQUIRED_PROJECTIONS: dict[ContentAssetType, frozenset[ProjectionTarget]] = {
    ContentAssetType.INTENT_DEFINITION: frozenset({ProjectionTarget.REDIS_INTENT_CATALOG}),
    ContentAssetType.INTENT_RULE: frozenset({ProjectionTarget.REDIS_INTENT_RULES}),
    ContentAssetType.INTENT_SIMILARITY: frozenset({ProjectionTarget.MILVUS_INTENT_SIMILARITIES}),
    ContentAssetType.FAQ_GROUP: frozenset(),
    ContentAssetType.FAQ_ITEM: frozenset({ProjectionTarget.REDIS_FAQ_EXACT}),
    ContentAssetType.POLICY_ANSWER: frozenset({ProjectionTarget.REDIS_POLICY_ANSWERS}),
    ContentAssetType.POLICY_PARAMETER: frozenset(),
    ContentAssetType.SOP_DEFINITION: frozenset(),
    ContentAssetType.RAG_DOCUMENT: frozenset({ProjectionTarget.MILVUS_RAG_DOCUMENTS}),
}


class PolicyParameterScope(ContractModel):
    """项目构造口径的适用范围；空数组表示该维度不限制。"""

    product_codes: tuple[str, ...] = ()
    channels: tuple[str, ...] = ()
    regions: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_unique_values(self) -> PolicyParameterScope:
        """同一维度不能重复声明，避免发布快照产生无意义差异。"""

        for values in (self.product_codes, self.channels, self.regions):
            if len(values) != len(set(values)):
                raise ValueError("policy parameter scope values must be unique")
        return self


class PolicyParameterDefinition(ContractModel):
    """可版本化的业务口径；不是监管规定，也不是客户个人业务事实。"""

    parameter_key: str = Field(pattern=r"^[a-z][a-z0-9_]*$", max_length=64)
    version: str = Field(pattern=r"^parameter-v[1-9][0-9]*$", max_length=32)
    value_text: str = Field(min_length=1, max_length=160)
    value_numeric: Decimal | None = None
    unit: Literal["DAY", "WORKDAY", "AMOUNT", "RATIO", "COUNT", "TEXT"]
    scope: PolicyParameterScope = Field(default_factory=PolicyParameterScope)
    effective_at: date
    retired_at: date | None = None
    source_kind: Literal["PROJECT_CONSTRUCTED"] = "PROJECT_CONSTRUCTED"
    business_reference: str | None = Field(default=None, max_length=255)

    @model_validator(mode="after")
    def validate_value(self) -> PolicyParameterDefinition:
        """数值、单位和有效期必须相互一致。"""

        if self.retired_at is not None and self.retired_at < self.effective_at:
            raise ValueError("policy parameter retired_at cannot precede effective_at")
        if self.unit == "TEXT" and self.value_numeric is not None:
            raise ValueError("TEXT policy parameter cannot carry a numeric value")
        if self.unit != "TEXT" and self.value_numeric is None:
            raise ValueError("numeric policy parameter requires value_numeric")
        return self

# FAQ 必须保留精确索引；语义索引只能补充模糊检索，不能替代精确索引。
ASSET_ALLOWED_PROJECTIONS = {
    **ASSET_REQUIRED_PROJECTIONS,
    ContentAssetType.FAQ_ITEM: frozenset(
        {ProjectionTarget.REDIS_FAQ_EXACT, ProjectionTarget.MILVUS_FAQ_VARIANTS}
    ),
}


class ContentAssetVersion(ContractModel):
    """发布清单中一个不可变的权威内容版本。"""

    asset_type: ContentAssetType
    asset_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    lifecycle_status: ContentLifecycleStatus
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    effective_from: datetime | None = None
    effective_to: datetime | None = None
    dependency_refs: tuple[str, ...] = ()

    @property
    def asset_ref(self) -> str:
        """生成清单内部稳定引用，不替代数据库主键。"""

        return f"{self.asset_type.value}:{self.asset_id}:{self.version}"

    @model_validator(mode="after")
    def validate_publishable_version(self) -> ContentAssetVersion:
        """清单只接收已经审核的版本，并拒绝倒置有效期。"""

        if self.lifecycle_status not in {
            ContentLifecycleStatus.APPROVED,
            ContentLifecycleStatus.PUBLISHED,
        }:
            raise ValueError("publication assets must be APPROVED or PUBLISHED")
        if (
            self.effective_from is not None
            and self.effective_to is not None
            and self.effective_from > self.effective_to
        ):
            raise ValueError("effective_from must not be later than effective_to")
        return self


class ContentConflictFinding(ContractModel):
    """发布前检测到的一项版本、路由或适用范围冲突。"""

    code: str = Field(pattern=r"^[A-Z][A-Z0-9_]*$")
    severity: ContentConflictSeverity
    message: str = Field(min_length=1)
    asset_refs: tuple[str, ...]

    @model_validator(mode="after")
    def validate_asset_refs(self) -> ContentConflictFinding:
        """冲突必须指向至少一个可定位资产。"""

        if not self.asset_refs:
            raise ValueError("content conflict finding requires asset_refs")
        return self


class ContentConflictCheck(ContractModel):
    """一个发布清单在固定检查器版本下的冲突结论。"""

    status: ContentConflictStatus
    checker_version: str = Field(min_length=1)
    checked_at: datetime
    findings: tuple[ContentConflictFinding, ...] = ()

    @model_validator(mode="after")
    def validate_status(self) -> ContentConflictCheck:
        """阻断状态与 BLOCKING finding 必须相互一致。"""

        has_blocking = any(
            finding.severity is ContentConflictSeverity.BLOCKING for finding in self.findings
        )
        if self.status is ContentConflictStatus.BLOCKED and not has_blocking:
            raise ValueError("BLOCKED conflict check requires a BLOCKING finding")
        if self.status is ContentConflictStatus.PASSED and has_blocking:
            raise ValueError("PASSED conflict check cannot contain BLOCKING findings")
        return self


class ProjectionBuild(ContractModel):
    """一次发布需要原子切换的一个 Redis 或 Milvus 派生版本。"""

    target: ProjectionTarget
    projection_version: str = Field(min_length=1)
    asset_refs: tuple[str, ...]

    @model_validator(mode="after")
    def validate_assets(self) -> ProjectionBuild:
        """空投影不能被记录为已经构建的版本。"""

        if not self.asset_refs:
            raise ValueError("projection build requires asset_refs")
        return self


class ContentPublicationManifest(ContractModel):
    """审核通过后驱动派生索引构建和版本切换的不可变清单。"""

    publication_id: str = Field(min_length=1)
    release_version: str = Field(min_length=1)
    source_revision: str = Field(min_length=1)
    status: PublicationStatus
    assets: tuple[ContentAssetVersion, ...]
    conflict_check: ContentConflictCheck
    projection_builds: tuple[ProjectionBuild, ...]
    approved_by: str = Field(min_length=1)
    approved_at: datetime
    published_at: datetime | None = None

    @model_validator(mode="after")
    def validate_manifest(self) -> ContentPublicationManifest:
        """拒绝重复、冲突或投影不完整的内容发布。"""

        asset_refs = [asset.asset_ref for asset in self.assets]
        if not asset_refs or len(asset_refs) != len(set(asset_refs)):
            raise ValueError("publication requires unique non-empty asset versions")
        if self.conflict_check.status is not ContentConflictStatus.PASSED:
            raise ValueError("only a PASSED conflict check may be published")

        assets_by_ref = {asset.asset_ref: asset for asset in self.assets}
        known_refs = set(assets_by_ref)
        if any(not set(asset.dependency_refs) <= known_refs for asset in self.assets):
            raise ValueError("asset dependency references must exist in the manifest")
        if any(
            not set(finding.asset_refs) <= known_refs for finding in self.conflict_check.findings
        ):
            raise ValueError("conflict findings must reference assets in the manifest")
        targets: set[ProjectionTarget] = set()
        for build in self.projection_builds:
            if not set(build.asset_refs) <= known_refs:
                raise ValueError("projection build references assets outside the manifest")
            if any(
                build.target not in ASSET_ALLOWED_PROJECTIONS[assets_by_ref[ref].asset_type]
                for ref in build.asset_refs
            ):
                raise ValueError("projection target is incompatible with an asset type")
            if build.target in targets:
                raise ValueError("projection targets must be unique within a release")
            targets.add(build.target)

        required_targets: set[ProjectionTarget] = set()
        for asset in self.assets:
            required_targets.update(ASSET_REQUIRED_PROJECTIONS[asset.asset_type])
        if not required_targets <= targets:
            raise ValueError("projection targets do not include all required asset projections")

        was_published = self.status in {PublicationStatus.PUBLISHED, PublicationStatus.RETIRED}
        if was_published and self.published_at is None:
            raise ValueError("PUBLISHED or RETIRED manifest requires published_at")
        if not was_published and self.published_at is not None:
            raise ValueError("READY or FAILED manifest must not carry published_at")
        if self.published_at is not None and self.published_at < self.approved_at:
            raise ValueError("published_at must not be earlier than approved_at")
        return self
