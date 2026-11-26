"""使用独立提示词生成带引用的 RAG 坐席草稿。

本模块不决定是否进入 RAG，也不直接向客户发送文本。准入由路由图完成，
草稿必须通过引用校验并进入坐席审核，模型只能在检索证据范围内组织表达。
"""

from __future__ import annotations

import json
import re
import time
from datetime import UTC, datetime
from typing import Any
from uuid import NAMESPACE_URL, uuid5

import httpx

from prompts.rag import (
    GROUNDED_PROMPT_VERSION,
    grounded_answer_retry_prompt,
    grounded_answer_system_prompt,
    grounded_answer_user_prompt,
)
from services.ai_runtime.model_request import (
    ModelContextWindowExceeded,
    validate_chat_request_budget,
)
from shared.contracts.enums import (
    ModelCallStatus,
    RetrievalAssessmentStatus,
    RetrievalRecommendedAction,
)
from shared.contracts.rag import RagCitation, RagDraft
from shared.contracts.retrieval import (
    RetrievalAssessment,
    RetrievalDocument,
    RetrievalResult,
)
from shared.contracts.runtime import ModelCallRecord

GROUNDED_OUTPUT_MAX_ATTEMPTS = 2
SEMANTIC_REVIEWABLE_REASONS = frozenset({"RERANK_KNOWLEDGE_MARGIN_TOO_SMALL"})
UNSUPPORTED_EVIDENCE_REASONS = frozenset(
    {
        "EVIDENCE_DOES_NOT_ANSWER",
        "EVIDENCE_NOT_AUTHORITATIVE",
        "EVIDENCE_SCOPE_MISMATCH",
        "EVIDENCE_CONFLICTING",
        "EVIDENCE_MISSING_KEY_CONDITION",
    }
)
RETRYABLE_OUTPUT_ERRORS = frozenset(
    {
        "GROUNDED_JSON_INVALID",
        "GROUNDED_SCHEMA_INVALID",
        "GROUNDED_CITATION_INVALID",
        "GROUNDED_CITATION_MISMATCH",
    }
)


def semantic_evidence_review_allowed(assessment: RetrievalAssessment) -> bool:
    """只让通过基础检索门槛或需要内容裁决的候选进入模型。"""

    return assessment.status is RetrievalAssessmentStatus.SUFFICIENT or bool(
        SEMANTIC_REVIEWABLE_REASONS.intersection(assessment.reason_codes)
    )


class GroundedOutputError(ValueError):
    """表示供应商已返回结果，但结果未通过本地草稿安全校验。"""

    def __init__(self, error_code: str, message: str) -> None:
        """保存稳定原因码；日志和审计不需要持久化模型原始正文。"""

        super().__init__(message)
        self.error_code = error_code


class GroundedAnswerGenerator:
    """调用 OpenAI 兼容模型生成草稿，并拒绝不带有效引用的输出。"""

    def __init__(
        self,
        *,
        provider: str,
        base_url: str,
        api_key: str,
        model: str,
        timeout_seconds: float,
        context_window_tokens: int,
    ) -> None:
        """保存连接配置；生成失败由调用方转安全兜底，不抛给客户。"""

        self._provider = provider
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._timeout_seconds = timeout_seconds
        self._context_window_tokens = context_window_tokens

    @property
    def model(self) -> str:
        """返回生成模型名称，写入草稿和审计记录。"""

        return self._model

    async def generate(
        self,
        query: str,
        retrieval: RetrievalResult,
        *,
        state: dict[str, object],
    ) -> tuple[
        RagDraft | None,
        RetrievalAssessment,
        tuple[ModelCallRecord, ...],
    ]:
        """同时判断证据充分性并生成草稿；结构失败至多重试一次。"""

        if (
            not semantic_evidence_review_allowed(retrieval.assessment)
            or not self._base_url
            or not self._api_key
        ):
            started = time.perf_counter()
            return (
                None,
                retrieval.assessment,
                (
                    self._failure(
                        state,
                        self._call_id(state, 1),
                        "GROUNDED_CONFIG_OR_EVIDENCE",
                        started,
                    ),
                ),
            )
        evidence = self._evidence(retrieval.documents)
        base_payload = {
            "model": self._model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": self._system_prompt()},
                {
                    "role": "user",
                    "content": grounded_answer_user_prompt(query, evidence),
                },
            ],
        }
        records: list[ModelCallRecord] = []
        previous_error: str | None = None
        for attempt in range(1, GROUNDED_OUTPUT_MAX_ATTEMPTS + 1):
            payload = dict(base_payload)
            messages = list(base_payload["messages"])
            if previous_error:
                messages.insert(
                    1,
                    {
                        "role": "system",
                        "content": grounded_answer_retry_prompt(),
                    },
                )
            payload["messages"] = messages
            draft, assessment, record = await self._generate_once(
                payload,
                retrieval,
                state,
                call_id=self._call_id(state, attempt),
            )
            records.append(record)
            if assessment is not None:
                return draft, assessment, tuple(records)
            previous_error = record.error_code
            if record.error_code not in RETRYABLE_OUTPUT_ERRORS:
                break
        return None, retrieval.assessment, tuple(records)

    async def _generate_once(
        self,
        payload: dict[str, Any],
        retrieval: RetrievalResult,
        state: dict[str, object],
        *,
        call_id: str,
    ) -> tuple[RagDraft | None, RetrievalAssessment | None, ModelCallRecord]:
        """执行一次供应商调用；网络、结构和引用失败使用不同原因码。"""

        started = time.perf_counter()
        estimated_input_tokens: int | None = None
        try:
            estimated_input_tokens = validate_chat_request_budget(
                payload,
                context_window_tokens=self._context_window_tokens,
            )
            async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
                response = await client.post(
                    f"{self._base_url}/chat/completions",
                    json=payload,
                    headers={"Authorization": f"Bearer {self._api_key}"},
                )
                response.raise_for_status()
                body = response.json()
        except ModelContextWindowExceeded as error:
            return (
                None,
                None,
                self._failure(
                    state,
                    call_id,
                    "MODEL_CONTEXT_WINDOW_EXCEEDED",
                    started,
                    estimated_input_tokens=error.estimated_input_tokens,
                ),
            )
        except httpx.TimeoutException:
            return (
                None,
                None,
                self._failure(
                    state,
                    call_id,
                    "GROUNDED_MODEL_TIMEOUT",
                    started,
                    estimated_input_tokens=estimated_input_tokens,
                ),
            )
        except httpx.HTTPError:
            return (
                None,
                None,
                self._failure(
                    state,
                    call_id,
                    "GROUNDED_PROVIDER_ERROR",
                    started,
                    estimated_input_tokens=estimated_input_tokens,
                ),
            )
        except (TypeError, ValueError, json.JSONDecodeError):
            return (
                None,
                None,
                self._failure(
                    state,
                    call_id,
                    "GROUNDED_PROVIDER_RESPONSE_INVALID",
                    started,
                    estimated_input_tokens=estimated_input_tokens,
                ),
            )

        if not isinstance(body, dict):
            return (
                None,
                None,
                self._failure(
                    state,
                    call_id,
                    "GROUNDED_PROVIDER_RESPONSE_INVALID",
                    started,
                    estimated_input_tokens=estimated_input_tokens,
                ),
            )
        usage = body.get("usage") or {}
        provider_request_id = body.get("id")
        try:
            output = self._parse_json(body["choices"][0]["message"]["content"])
            assessment = self._assessment(output, retrieval.assessment)
            draft = (
                self._draft(output, retrieval, state)
                if assessment.status is RetrievalAssessmentStatus.SUFFICIENT
                else None
            )
            return (
                draft,
                assessment,
                self._record(
                    state=state,
                    call_id=call_id,
                    status=ModelCallStatus.COMPLETED,
                    started=started,
                    usage=usage,
                    provider_request_id=provider_request_id,
                    estimated_input_tokens=estimated_input_tokens,
                ),
            )

        except GroundedOutputError as error:
            return (
                None,
                None,
                self._failure(
                    state,
                    call_id,
                    error.error_code,
                    started,
                    usage=usage,
                    provider_request_id=provider_request_id,
                    estimated_input_tokens=estimated_input_tokens,
                ),
            )
        except (KeyError, IndexError, TypeError):
            return (
                None,
                None,
                self._failure(
                    state,
                    call_id,
                    "GROUNDED_PROVIDER_RESPONSE_INVALID",
                    started,
                    usage=usage,
                    provider_request_id=provider_request_id,
                ),
            )

    @staticmethod
    def _call_id(state: dict[str, object], attempt: int) -> str:
        """为每次供应商调用生成稳定且互不覆盖的审计编号。"""

        suffix = "" if attempt == 1 else f":ATTEMPT:{attempt}"
        seed = f"{state['run_id']}:GROUNDED_ANSWER{suffix}"
        return f"mc_{uuid5(NAMESPACE_URL, seed).hex}"

    @staticmethod
    def _system_prompt() -> str:
        """约束模型先判定证据支持性，再生成可引用的坐席草稿。"""

        return grounded_answer_system_prompt()

    @staticmethod
    def _assessment(
        output: dict[str, Any],
        current: RetrievalAssessment,
    ) -> RetrievalAssessment:
        """把受控模型结论转换为证据契约，不接受自由文本原因。"""

        support_status = output.get("support_status")
        reason_code = output.get("reason_code")
        if support_status == "SUPPORTED":
            if reason_code != "EVIDENCE_SUPPORTS_ANSWER":
                raise GroundedOutputError(
                    "GROUNDED_SCHEMA_INVALID",
                    "证据充分结论缺少固定原因码",
                )
            reasons = tuple(
                "RERANK_COMPETITION_REVIEWED"
                if reason == "RERANK_KNOWLEDGE_MARGIN_TOO_SMALL"
                else reason
                for reason in current.reason_codes
            )
            return RetrievalAssessment(
                status=RetrievalAssessmentStatus.SUFFICIENT,
                coverage=1,
                reason_codes=(*reasons, "SEMANTIC_EVIDENCE_SUPPORTED"),
                recommended_action=RetrievalRecommendedAction.ANSWER,
            )
        if support_status != "NOT_SUPPORTED" or reason_code not in UNSUPPORTED_EVIDENCE_REASONS:
            raise GroundedOutputError(
                "GROUNDED_SCHEMA_INVALID",
                "证据判定状态或原因码不受支持",
            )
        if output.get("answer") not in (None, "") or output.get("citations") != []:
            raise GroundedOutputError(
                "GROUNDED_SCHEMA_INVALID",
                "证据不足时不能返回草稿或引用",
            )
        conflicting = reason_code == "EVIDENCE_CONFLICTING"
        return RetrievalAssessment(
            status=(
                RetrievalAssessmentStatus.CONFLICTING
                if conflicting
                else RetrievalAssessmentStatus.INSUFFICIENT
            ),
            coverage=0,
            reason_codes=(*current.reason_codes, str(reason_code)),
            recommended_action=(
                RetrievalRecommendedAction.HUMAN
                if conflicting
                else RetrievalRecommendedAction.SAFE_FALLBACK
            ),
        )

    @staticmethod
    def _evidence(documents: tuple[RetrievalDocument, ...]) -> str:
        """给模型有限的父文档片段，保留页码和标题以便生成可追溯引用。"""

        lines = []
        for index, document in enumerate(documents[:5], start=1):
            metadata = document.metadata
            title = getattr(metadata, "title", "知识文档")
            section = getattr(metadata, "section_title", "")
            page = getattr(metadata, "page", "")
            text = document.page_content[:2400]
            lines.append(f"[{index}] {title} / {section} / 第{page}页\n{text}")
        return "\n\n".join(lines)

    def _draft(
        self,
        output: dict[str, Any],
        retrieval: RetrievalResult,
        state: dict[str, object],
    ) -> RagDraft:
        """把模型引用编号映射回检索文档，并严格拒绝悬空引用。"""

        answer = str(output.get("answer", "")).strip()
        raw_citations = output.get("citations")
        if not answer or not isinstance(raw_citations, list):
            raise GroundedOutputError(
                "GROUNDED_SCHEMA_INVALID",
                "生成结果缺少 answer 或 citations",
            )
        unique_numbers = []
        for value in raw_citations:
            try:
                number = int(value)
            except (TypeError, ValueError) as error:
                raise GroundedOutputError(
                    "GROUNDED_CITATION_INVALID",
                    "生成结果包含非数字引用",
                ) from error
            if number not in unique_numbers:
                unique_numbers.append(number)
        documents = retrieval.documents[:5]
        if not unique_numbers or any(
            number < 1 or number > len(documents) for number in unique_numbers
        ):
            raise GroundedOutputError(
                "GROUNDED_CITATION_INVALID",
                "生成结果包含无效引用编号",
            )
        markers = {f"[{number}]" for number in unique_numbers}
        answer_markers = {f"[{number}]" for number in re.findall(r"\[([1-9][0-9]*)\]", answer)}
        if answer_markers != markers:
            raise GroundedOutputError(
                "GROUNDED_CITATION_MISMATCH",
                "生成正文与引用清单不一致",
            )
        citations = tuple(
            self._citation(number, documents[number - 1]) for number in unique_numbers
        )
        return RagDraft(
            draft_id=f"draft_{uuid5(NAMESPACE_URL, str(state['run_id']) + ':RAG_DRAFT').hex}",
            retrieval_id=retrieval.retrieval_id,
            answer=answer,
            citations=citations,
            provider=self._provider,
            model=self._model,
            prompt_version=GROUNDED_PROMPT_VERSION,
            created_at=datetime.now(UTC),
        )

    @staticmethod
    def _citation(number: int, document: RetrievalDocument) -> RagCitation:
        """将检索元数据转为坐席可读且可审计的引用。"""

        metadata = document.metadata
        return RagCitation(
            citation_id=f"[{number}]",
            document_id=document.document_id,
            parent_doc_id=getattr(metadata, "parent_doc_id", document.document_id),
            title=getattr(metadata, "title", "知识文档"),
            section_title=getattr(metadata, "section_title", ""),
            page=getattr(metadata, "page", 1),
            excerpt=document.page_content[:1200],
            content_hash=metadata.content_hash,
        )

    @staticmethod
    def _parse_json(content: Any) -> dict[str, Any]:
        """去除模型偶尔包裹的代码围栏，再解析对象。"""

        if not isinstance(content, str):
            raise GroundedOutputError("GROUNDED_SCHEMA_INVALID", "模型输出必须是文本")
        text = content.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        try:
            result = json.loads(text)
        except json.JSONDecodeError as error:
            raise GroundedOutputError(
                "GROUNDED_JSON_INVALID",
                "模型输出不是合法 JSON",
            ) from error
        if not isinstance(result, dict):
            raise GroundedOutputError("GROUNDED_SCHEMA_INVALID", "模型输出必须是对象")
        return result

    def _record(
        self,
        *,
        state: dict[str, object],
        call_id: str,
        status: ModelCallStatus,
        started: float,
        usage: dict[str, Any],
        provider_request_id: str | None = None,
        error_code: str | None = None,
        estimated_input_tokens: int | None = None,
    ) -> ModelCallRecord:
        """记录生成调用的真实供应商用量，不把 Embedding 计入模型调用。"""

        input_tokens = self._number(usage.get("prompt_tokens"))
        output_tokens = self._number(usage.get("completion_tokens"))
        total_tokens = self._number(usage.get("total_tokens"))
        return ModelCallRecord(
            call_id=call_id,
            run_id=str(state["run_id"]),
            turn_id=str(state["turn_id"]),
            trace_id=str(state["trace_id"]),
            purpose="GROUNDED_ANSWER",
            provider=self._provider,
            model=self._model,
            provider_request_id=provider_request_id,
            status=status,
            estimated_input_tokens=estimated_input_tokens,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            context_window_tokens=self._context_window_tokens,
            latency_ms=max(0, round((time.perf_counter() - started) * 1000)),
            error_code=error_code,
            created_at=datetime.now(UTC),
        )

    def _failure(
        self,
        state: dict[str, object],
        call_id: str,
        error_code: str,
        started: float,
        *,
        usage: dict[str, Any] | None = None,
        provider_request_id: str | None = None,
        estimated_input_tokens: int | None = None,
    ) -> ModelCallRecord:
        """生成失败保留真实用量和稳定原因码，调用方转安全兜底。"""

        return self._record(
            state=state,
            call_id=call_id,
            status=ModelCallStatus.FAILED,
            started=started,
            usage=usage or {},
            provider_request_id=provider_request_id,
            error_code=error_code,
            estimated_input_tokens=estimated_input_tokens,
        )

    @staticmethod
    def _number(value: Any) -> int | None:
        """供应商缺少 usage 时保留空值，避免伪造 Token 用量。"""

        return int(value) if isinstance(value, (int, float)) and value >= 0 else None
