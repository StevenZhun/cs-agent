"""用显式门槛把 Jev 原子信号收敛为全局意图状态。"""

from __future__ import annotations

from dataclasses import dataclass

from services.ai_runtime.decision.jev import JevIntentEvaluation
from shared.contracts.enums import IntentDecisionStatus, IntentMatchSource
from shared.contracts.routing import IntentCandidate, IntentDecision


@dataclass(frozen=True)
class JevIntentPolicy:
    """把 Jev 概率信号收敛为意图状态所需的显式、带版本门槛。"""

    version: str
    candidate_applies_threshold: float
    multiple_intents_threshold: float
    choice_threshold: float
    choice_margin: float

    def __post_init__(self) -> None:
        """拒绝空版本和非法概率，避免未校准参数进入决策链。"""

        if not self.version.strip():
            raise ValueError("Jev intent policy version must not be blank")
        values = (
            self.candidate_applies_threshold,
            self.multiple_intents_threshold,
            self.choice_threshold,
            self.choice_margin,
        )
        if any(value < 0 or value > 1 for value in values):
            raise ValueError("Jev intent policy thresholds must be between 0 and 1")


def decide_jev_intent(
    evaluation: JevIntentEvaluation,
    *,
    candidates: tuple[IntentCandidate, ...],
    catalog_version: str,
    policy: JevIntentPolicy,
) -> IntentDecision:
    """Jev 只提供感知信号，唯一命中、拒判和歧义由同一 Policy 形成。"""

    if not evaluation.succeeded:
        return _unknown(
            catalog_version,
            reason_code=evaluation.error_code or "JEV_PROVIDER_UNAVAILABLE",
        )
    candidate_by_id = {item.intent_id: item for item in candidates}
    if not candidate_by_id:
        return _unknown(catalog_version, reason_code="JEV_CANDIDATES_EMPTY")

    selected_signal = _unique_signal(evaluation, "selected_intent")
    applies_signal = _unique_signal(evaluation, "candidate_applies")
    multiple_signal = _unique_signal(evaluation, "multiple_intents")
    selected_id = str(selected_signal.value)
    selected_candidate = candidate_by_id.get(selected_id)
    if selected_candidate is None:
        return _unknown(catalog_version, reason_code="JEV_SELECTED_CANDIDATE_INVALID")

    per_candidate_probabilities = _candidate_probabilities(evaluation)
    applicable = tuple(
        item
        for item in candidates
        if per_candidate_probabilities.get(item.intent_id, 0.0)
        >= policy.candidate_applies_threshold
    )
    multiple_probability = _true_probability(multiple_signal)
    if multiple_probability >= policy.multiple_intents_threshold and len(applicable) >= 2:
        return IntentDecision(
            status=IntentDecisionStatus.MULTI_INTENT,
            catalog_version=catalog_version,
            provider_model=evaluation.provider_model,
            candidates=applicable,
            reason_codes=("JEV_MULTIPLE_APPLICABLE_CANDIDATES", policy.version),
        )

    if _true_probability(applies_signal) < policy.candidate_applies_threshold:
        return _unknown(
            catalog_version,
            reason_code="JEV_NO_APPLICABLE_CANDIDATE",
            provider_model=evaluation.provider_model,
        )

    choice_probabilities = selected_signal.probabilities or {}
    selected_probability = float(choice_probabilities.get(selected_id, 0.0))
    runner_up = max(
        (
            float(probability)
            for intent_id, probability in choice_probabilities.items()
            if intent_id != selected_id and intent_id in candidate_by_id
        ),
        default=0.0,
    )
    selected_is_applicable = (
        per_candidate_probabilities.get(selected_id, 0.0) >= policy.candidate_applies_threshold
    )
    if (
        selected_is_applicable
        and selected_probability >= policy.choice_threshold
        and selected_probability - runner_up >= policy.choice_margin
    ):
        return IntentDecision(
            status=IntentDecisionStatus.MATCHED,
            intent_id=selected_id,
            route_target=selected_candidate.route_target,
            match_source=IntentMatchSource.MODEL,
            confidence=selected_probability,
            catalog_version=catalog_version,
            provider_model=evaluation.provider_model,
            matched_source_ref=evaluation.input_snapshot_ref,
            reason_codes=("JEV_POLICY_MATCHED", policy.version),
        )

    if len(candidates) >= 2:
        return IntentDecision(
            status=IntentDecisionStatus.AMBIGUOUS,
            catalog_version=catalog_version,
            provider_model=evaluation.provider_model,
            candidates=candidates,
            reason_codes=("JEV_POLICY_AMBIGUOUS", policy.version),
        )
    return _unknown(
        catalog_version,
        reason_code="JEV_SINGLE_CANDIDATE_UNCERTAIN",
        provider_model=evaluation.provider_model,
    )


def _unique_signal(evaluation: JevIntentEvaluation, name: str):
    matches = [signal for signal in evaluation.signals if signal.signal_name == name]
    if len(matches) != 1:
        raise ValueError(f"Jev evaluation requires exactly one {name} signal")
    return matches[0]


def _candidate_probabilities(evaluation: JevIntentEvaluation) -> dict[str, float]:
    result: dict[str, float] = {}
    for signal in evaluation.signals:
        if signal.signal_name != "candidate_intent_applies" or len(signal.candidate_refs) != 1:
            continue
        result[signal.candidate_refs[0]] = _true_probability(signal)
    return result


def _true_probability(signal) -> float:
    return float((signal.probabilities or {}).get("true", 0.0))


def _unknown(
    catalog_version: str,
    *,
    reason_code: str,
    provider_model: str | None = None,
) -> IntentDecision:
    return IntentDecision(
        status=IntentDecisionStatus.UNKNOWN,
        catalog_version=catalog_version,
        provider_model=provider_model,
        reason_codes=(reason_code,),
    )
