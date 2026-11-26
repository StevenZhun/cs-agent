"""把 Jev 的有限候选判断转换为项目自己的决策信号。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from time import perf_counter
from typing import Any

from langchain_core.decisions import Choice, Noul
from langchain_typesafe import TypeSafeClassifier
from langchain_typesafe.client import (
    TypeSafeAPIConnectionError,
    TypeSafeAPIError,
    TypeSafeAPIResponseValidationError,
    TypeSafeAPITimeoutError,
    TypeSafeAuthenticationError,
    TypeSafePermissionDeniedError,
    TypeSafeRateLimitError,
)

from shared.contracts.enums import DecisionProfile, DecisionTask
from shared.contracts.routing import IntentCandidate
from shared.contracts.runtime import DecisionShadowObservation, DecisionSignal, DecisionTrace
from shared.guardrails import redact_protected_secrets

JEV_INTENT_POLICY_VERSION = "jev-intent-observation-v1"


@dataclass(frozen=True)
class JevIntentEvaluation:
    """一次 Jev 调用的安全观测结果，不包含原始请求或供应商正文。"""

    signals: tuple[DecisionSignal, ...]
    provider_model: str
    provider_request_id: str | None
    provider_usage: dict[str, int]
    latency_ms: int
    input_snapshot_ref: str
    candidate_set_version: str
    error_code: str | None = None

    @property
    def succeeded(self) -> bool:
        """只有响应通过候选和值域校验才算成功。"""

        return self.error_code is None

    def to_offline_trace(self, *, decision_id: str, final_decision_ref: str) -> DecisionTrace:
        """生成 Shadow 对照 Trace；该 Trace 不得冒充在线 active 路由结果。"""

        return DecisionTrace(
            decision_id=decision_id,
            decision_task=DecisionTask.ROUTING,
            active_profile=DecisionProfile.JEV,
            provider="typesafe",
            provider_model=self.provider_model,
            provider_request_id=self.provider_request_id,
            input_snapshot_ref=self.input_snapshot_ref,
            candidate_set_version=self.candidate_set_version,
            threshold_policy_version=JEV_INTENT_POLICY_VERSION,
            signals=self.signals,
            final_decision_ref=final_decision_ref,
            latency_ms=self.latency_ms,
            provider_usage=self.provider_usage,
            created_at=datetime.now(UTC),
        )

    def to_shadow_observation(self) -> DecisionShadowObservation:
        """转换为可随 active Trace 保存、但没有业务决策权的旁路观测。"""

        return DecisionShadowObservation(
            profile=DecisionProfile.JEV,
            provider="typesafe",
            provider_model=self.provider_model,
            provider_request_id=self.provider_request_id,
            input_snapshot_ref=self.input_snapshot_ref,
            candidate_set_version=self.candidate_set_version,
            signals=self.signals if self.succeeded else (),
            latency_ms=self.latency_ms,
            provider_usage=self.provider_usage,
            error_code=self.error_code,
        )


class JevIntentDecisionAdapter:
    """只判断已召回的有限全局意图候选，不拥有最终路由决策权。"""

    def __init__(self, classifier: Any, *, model: str, timeout_seconds: float) -> None:
        self._classifier = classifier
        self._model = model
        self._timeout_seconds = timeout_seconds

    async def evaluate(
        self,
        query: str,
        *,
        candidates: tuple[IntentCandidate, ...],
        catalog: dict[str, dict[str, str]],
        candidate_set_version: str,
    ) -> JevIntentEvaluation:
        """发送脱敏 Query 和有限候选，失败时返回安全原因码而不抛给主链路。"""

        started = perf_counter()
        snapshot = _minimal_snapshot(query, candidates=candidates, catalog=catalog)
        snapshot_ref = _snapshot_ref(snapshot)
        candidate_ids = tuple(item.intent_id for item in candidates)
        if not candidate_ids:
            return _failed_evaluation(
                model=self._model,
                latency_ms=_elapsed_ms(started),
                snapshot_ref=snapshot_ref,
                candidate_set_version=candidate_set_version,
                error_code="JEV_CANDIDATES_EMPTY",
            )

        questions = {
            "intent": Choice(
                instructions="哪一个已发布全局意图最适合当前客户请求？只能在候选中选择。",
                criteria={
                    intent_id: _candidate_description(intent_id, catalog)
                    for intent_id in candidate_ids
                },
            ),
            "candidate_applies": Noul(
                instructions="至少有一个候选意图能够准确覆盖客户当前请求吗？"
            ),
            "multiple_intents": Noul(
                instructions="客户是否在同一条消息中明确提出多个需要分别处理的业务诉求？"
            ),
            **{
                f"candidate_{index}_applies": Noul(
                    instructions=(
                        f"已发布意图 {intent_id} 是否准确覆盖客户当前请求？"
                        "只判断该候选，不得假设候选集之外的意图。"
                    )
                )
                for index, intent_id in enumerate(candidate_ids)
            },
        }
        try:
            response = await asyncio.wait_for(
                self._classifier.ainvoke({"state": snapshot, "questions": questions}),
                timeout=self._timeout_seconds,
            )
            selected = response.choices["intent"]
            applies = response.nouls["candidate_applies"].noul
            multiple = response.nouls["multiple_intents"].noul
            candidate_applies = tuple(
                response.nouls[f"candidate_{index}_applies"].noul
                for index in range(len(candidate_ids))
            )
            if selected.choice not in candidate_ids:
                raise ValueError("Jev selected an intent outside the supplied candidate set")
            signals = (
                DecisionSignal(
                    signal_name="selected_intent",
                    value=selected.choice,
                    probabilities=dict(selected.probabilities),
                    confidence=selected.confidence,
                    candidate_refs=candidate_ids,
                    reason_codes=("JEV_INTENT_CHOICE",),
                ),
                _noul_signal("candidate_applies", applies),
                _noul_signal("multiple_intents", multiple),
                *(
                    _noul_signal(
                        "candidate_intent_applies",
                        probability,
                        candidate_refs=(intent_id,),
                    )
                    for intent_id, probability in zip(
                        candidate_ids,
                        candidate_applies,
                        strict=True,
                    )
                ),
            )
            return JevIntentEvaluation(
                signals=signals,
                provider_model=response.model,
                provider_request_id=response.request_id,
                provider_usage=_usage(response.usage),
                latency_ms=_elapsed_ms(started),
                input_snapshot_ref=snapshot_ref,
                candidate_set_version=candidate_set_version,
            )
        except (TimeoutError, TypeSafeAPITimeoutError):
            code = "JEV_TIMEOUT"
        except TypeSafeRateLimitError:
            code = "JEV_RATE_LIMITED"
        except (TypeSafeAuthenticationError, TypeSafePermissionDeniedError):
            code = "JEV_AUTHENTICATION_FAILED"
        except TypeSafeAPIResponseValidationError:
            code = "JEV_RESPONSE_INVALID"
        except TypeSafeAPIConnectionError:
            code = "JEV_CONNECTION_FAILED"
        except TypeSafeAPIError:
            code = "JEV_API_FAILED"
        except (AttributeError, KeyError, TypeError, ValueError):
            code = "JEV_RESPONSE_INVALID"
        except Exception:
            # 未预见的 Provider 异常也只能使本次观测失败，不能击穿 baseline 主链路。
            code = "JEV_UNEXPECTED_FAILURE"

        return _failed_evaluation(
            model=self._model,
            latency_ms=_elapsed_ms(started),
            snapshot_ref=snapshot_ref,
            candidate_set_version=candidate_set_version,
            error_code=code,
        )

    async def aclose(self) -> None:
        """关闭分类客户端暴露的同步和异步连接池。"""

        async_client = getattr(self._classifier, "async_client", None)
        if async_client is not None:
            await async_client.aclose()
        client = getattr(self._classifier, "client", None)
        if client is not None:
            client.close()


def create_jev_intent_adapter(
    *,
    api_key: str | None,
    base_url: str,
    model: str,
    timeout_seconds: float,
) -> JevIntentDecisionAdapter | None:
    """只有凭据完整时才创建外部客户端；空凭据不影响 baseline 启动。"""

    if not api_key or not api_key.strip():
        return None
    classifier = TypeSafeClassifier(
        api_key=api_key,
        base_url=base_url,
        model=model,
        timeout=timeout_seconds,
    )
    return JevIntentDecisionAdapter(
        classifier,
        model=model,
        timeout_seconds=timeout_seconds,
    )


def _minimal_snapshot(
    query: str,
    *,
    candidates: tuple[IntentCandidate, ...],
    catalog: dict[str, dict[str, str]],
) -> dict[str, object]:
    """只保留候选判断所需字段，不发送客户、会话、业务对象或历史标识。"""

    return {
        "query": redact_protected_secrets(query),
        "candidates": [
            {
                "intent_id": item.intent_id,
                "description": _candidate_description(item.intent_id, catalog),
                "route_target": item.route_target.value,
            }
            for item in candidates
        ],
    }


def _candidate_description(intent_id: str, catalog: dict[str, dict[str, str]]) -> str:
    item = catalog.get(intent_id, {})
    return str(item.get("description") or item.get("name") or intent_id)


def _snapshot_ref(snapshot: dict[str, object]) -> str:
    import json

    canonical = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + sha256(canonical.encode("utf-8")).hexdigest()


def _noul_signal(
    name: str,
    probability: float,
    *,
    candidate_refs: tuple[str, ...] = (),
) -> DecisionSignal:
    return DecisionSignal(
        signal_name=name,
        value=probability >= 0.5,
        probabilities={"true": probability, "false": 1 - probability},
        confidence=abs(probability - 0.5) * 2,
        candidate_refs=candidate_refs,
        reason_codes=("JEV_NOUL",),
    )


def _usage(value: object) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    return {
        key: int(amount) for key, amount in value.items() if isinstance(amount, int) and amount >= 0
    }


def _elapsed_ms(started: float) -> int:
    return max(0, round((perf_counter() - started) * 1000))


def _failed_evaluation(
    *,
    model: str,
    latency_ms: int,
    snapshot_ref: str,
    candidate_set_version: str,
    error_code: str,
) -> JevIntentEvaluation:
    return JevIntentEvaluation(
        signals=(
            DecisionSignal(
                signal_name="provider_status",
                value="UNAVAILABLE",
                reason_codes=(error_code,),
            ),
        ),
        provider_model=model,
        provider_request_id=None,
        provider_usage={},
        latency_ms=latency_ms,
        input_snapshot_ref=snapshot_ref,
        candidate_set_version=candidate_set_version,
        error_code=error_code,
    )
