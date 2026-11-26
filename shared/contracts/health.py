"""Service liveness and dependency-readiness contracts."""

from enum import StrEnum

from shared.contracts.common import ContractModel


class HealthState(StrEnum):
    """Externally visible service health state."""

    UP = "UP"
    READY = "READY"
    NOT_READY = "NOT_READY"


class DependencyState(StrEnum):
    """Result of one required dependency probe."""

    UP = "UP"
    DOWN = "DOWN"


class DependencyHealth(ContractModel):
    """A non-sensitive dependency readiness result."""

    name: str
    state: DependencyState


class ServiceHealth(ContractModel):
    """Stable payload shared by all service health endpoints."""

    service: str
    version: str
    environment: str
    state: HealthState
    dependencies: tuple[DependencyHealth, ...] = ()
