"""AI Runtime 对 Redis 和 Milvus 执行协议级就绪检查。"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress

from pymilvus import MilvusClient, MilvusException
from redis.asyncio import Redis
from redis.exceptions import RedisError

from services.content.rag.publication import load_rag_release
from services.content.semantic_index import collection_names
from shared.contracts.health import DependencyHealth, DependencyState
from shared.web.service_app import TcpDependency, probe_tcp
from shared.web.settings import ServiceSettings

logger = logging.getLogger(__name__)

MILVUS_RETRY_BASE_SECONDS = 5.0
MILVUS_RETRY_MAX_SECONDS = 30.0


class RuntimeDependencyProbe:
    """复用轻量客户端检查依赖是否真的能提供 Runtime 所需能力。"""

    def __init__(self, settings: ServiceSettings) -> None:
        self._settings = settings
        self._redis = Redis.from_url(settings.redis_url)
        self._milvus: MilvusClient | None = None
        self._milvus_lock = asyncio.Lock()
        self._last_states: dict[str, DependencyState] = {}
        self._milvus_failures = 0
        self._milvus_retry_after = 0.0

    async def __call__(self, dependency: TcpDependency) -> DependencyHealth:
        """PostgreSQL 检查连通性，Redis 和 Milvus 检查实际协议能力。"""

        if dependency.name == "redis":
            result = await self._probe_redis(dependency)
        elif dependency.name == "milvus":
            result = await self._probe_milvus(dependency)
        else:
            result = await probe_tcp(dependency)
        self._log_state_change(result)
        return result

    async def close(self) -> None:
        """释放健康检查持有的连接，不影响 Runtime 自己的客户端。"""

        await self._redis.aclose()
        async with self._milvus_lock:
            if self._milvus is not None:
                await asyncio.to_thread(self._milvus.close)
                self._milvus = None

    async def _probe_redis(self, dependency: TcpDependency) -> DependencyHealth:
        try:
            pong = await asyncio.wait_for(self._redis.ping(), timeout=dependency.timeout_seconds)
        except (TimeoutError, OSError, RedisError):
            return DependencyHealth(name=dependency.name, state=DependencyState.DOWN)
        return DependencyHealth(
            name=dependency.name,
            state=DependencyState.UP if pong else DependencyState.DOWN,
        )

    async def _probe_milvus(self, dependency: TcpDependency) -> DependencyHealth:
        """描述在线集合；端口可连但索引缺失时仍判定为未就绪。"""

        # 先检查 TCP。进程已退出时不创建 Pymilvus 客户端，避免 SDK 在每次
        # readiness 请求中输出一整段连接恢复堆栈。
        tcp_result = await probe_tcp(dependency)
        if tcp_result.state is DependencyState.DOWN:
            self._milvus_failures = 0
            self._milvus_retry_after = 0.0
            await self._reset_milvus()
            return tcp_result

        now = asyncio.get_running_loop().time()
        if now < self._milvus_retry_after:
            return DependencyHealth(name=dependency.name, state=DependencyState.DOWN)

        required = []
        if self._settings.semantic_enabled:
            names = collection_names(self._settings.semantic_index_version)
            required.extend((names.intent, names.faq))
        if self._settings.rag_enabled:
            try:
                rag_release = await load_rag_release(self._redis)
            except (OSError, RedisError, TypeError, ValueError):
                return DependencyHealth(name=dependency.name, state=DependencyState.DOWN)
            collection = str(rag_release.get("collection_name", ""))
            if not collection:
                return DependencyHealth(name=dependency.name, state=DependencyState.DOWN)
            required.append(collection)
        try:
            async with self._milvus_lock:
                await asyncio.wait_for(
                    asyncio.to_thread(self._describe_collections, tuple(required)),
                    timeout=dependency.timeout_seconds,
                )
        except (TimeoutError, OSError, RuntimeError, MilvusException):
            self._milvus_failures += 1
            delay = min(
                MILVUS_RETRY_MAX_SECONDS,
                MILVUS_RETRY_BASE_SECONDS * (2 ** (self._milvus_failures - 1)),
            )
            self._milvus_retry_after = now + delay
            await self._reset_milvus()
            return DependencyHealth(name=dependency.name, state=DependencyState.DOWN)
        self._milvus_failures = 0
        self._milvus_retry_after = 0.0
        return DependencyHealth(name=dependency.name, state=DependencyState.UP)

    def _describe_collections(self, required: tuple[str, ...]) -> None:
        if self._milvus is None:
            self._milvus = MilvusClient(uri=self._settings.milvus_uri, timeout=2)
        for collection in required:
            self._milvus.describe_collection(collection_name=collection, timeout=2)

    async def _reset_milvus(self) -> None:
        """连接异常后丢弃旧客户端，依赖恢复时允许下一次探测重建。"""

        async with self._milvus_lock:
            if self._milvus is not None:
                with suppress(OSError, RuntimeError, MilvusException):
                    await asyncio.to_thread(self._milvus.close)
                self._milvus = None

    def _log_state_change(self, result: DependencyHealth) -> None:
        """只记录依赖状态变化，持续故障不重复刷相同日志。"""

        previous = self._last_states.get(result.name)
        if previous is result.state:
            return
        self._last_states[result.name] = result.state
        log = logger.warning if result.state is DependencyState.DOWN else logger.info
        log(
            "runtime_dependency_state_changed dependency=%s previous=%s current=%s",
            result.name,
            previous.value if previous is not None else "UNKNOWN",
            result.state.value,
        )
