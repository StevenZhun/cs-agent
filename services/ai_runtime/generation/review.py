"""RAG 草稿人工审核停点：草稿不可改，最终答复由明确动作决定。"""

from langchain_core.runnables import RunnableConfig
from langgraph.types import interrupt

from services.ai_runtime.persistence.artifacts import (
    ArtifactValidationError,
    RuntimeArtifactRepository,
)
from shared.contracts.rag import RagDraft, RagReviewInstruction, RagReviewOutcome
from shared.safe_fallback import RAG_REVIEW_REJECTED_REPLY


async def _load_draft(
    state: dict,
    artifact_repository: RuntimeArtifactRepository | None,
) -> RagDraft:
    """优先按稳定引用回装草稿，并兼容旧 checkpoint 的全文字段。"""

    if state.get("rag_draft_ref"):
        if artifact_repository is None:
            raise ArtifactValidationError("RAG draft repository is unavailable")
        payload = await artifact_repository.load(
            state["rag_draft_ref"],
            authorized_conversation_id=str(state["conversation_id"]),
            expected_kind="RAG_DRAFT",
        )
        return RagDraft.model_validate(payload)
    return RagDraft.model_validate(state["rag_draft"])


async def await_rag_review(
    state: dict,
    config: RunnableConfig,
    artifact_repository: RuntimeArtifactRepository | None = None,
) -> dict:
    """把稳定草稿标识交给 LangGraph interrupt，等待 BFF 落库后的坐席动作。

    interrupt 之前不发送草稿、不改推荐状态。恢复时节点会重新执行，因此这里只能
    读取 checkpoint 中已有的数据，并把恢复指令交给下一个无外部副作用的节点。
    """

    draft = await _load_draft(state, artifact_repository)
    decision = interrupt(
        {
            "review_type": "RAG",
            "turn_id": state["turn_id"],
            "draft_id": draft.draft_id,
            "retrieval_id": draft.retrieval_id,
        }
    )
    instruction = RagReviewInstruction.model_validate(
        {name: decision[name] for name in RagReviewInstruction.model_fields if name in decision}
    )
    if instruction.turn_id != state["turn_id"] or instruction.draft_id != draft.draft_id:
        raise ValueError("待审核 RAG 草稿或轮次已变化")

    # 人工审核沿用原客户 Turn，但产生新的 Run。prepare_turn 只更新本次 Run 身份，
    # 不会重复追加客户消息或重新执行检索与生成。
    from services.ai_runtime.graph import prepare_turn

    supplied = decision["graph_input"]
    prepared = prepare_turn({**state, **supplied}, config)
    return {
        **supplied,
        **prepared,
        "rag_review_decision": instruction.model_dump(mode="json"),
    }


async def apply_rag_review(
    state: dict,
    artifact_repository: RuntimeArtifactRepository | None = None,
) -> dict:
    """将采用、编辑或拒绝转换为确定终态；本节点不直接写应用数据库。"""

    decision = RagReviewInstruction.model_validate(state["rag_review_decision"])
    if decision.action == "REGENERATE":
        return {
            "rag_review_decision": None,
            "rag_review_outcome": None,
            "last_rag_review_decision_id": decision.decision_id,
            "rag_retrieval": None,
            "rag_retrieval_ref": None,
            "rag_retrieval_summary": None,
            "rag_draft": None,
            "rag_draft_ref": None,
            "rag_turn_id": "",
            "rag_generation_turn_id": "",
            "model_calls": [],
        }
    draft = await _load_draft(state, artifact_repository)
    if decision.action == "APPROVE":
        content = draft.answer
        reason = "RAG_REVIEW_APPROVED"
    elif decision.action == "EDIT":
        content = decision.edited_answer or ""
        reason = "RAG_REVIEW_EDITED"
    else:
        # 拒绝时绝不把未通过审核的草稿回传给客户。
        content = RAG_REVIEW_REJECTED_REPLY
        reason = "RAG_REVIEW_REJECTED"
    outcome = RagReviewOutcome(
        decision_id=decision.decision_id,
        recommendation_id=decision.recommendation_id,
        draft_id=draft.draft_id,
        action=decision.action,
        actor_id=decision.actor_id,
        final_answer=content,
        reviewed_at=decision.acted_at,
    )
    return {
        "rag_review_decision": None,
        "rag_review_outcome": outcome.model_dump(mode="json"),
        "last_rag_review_decision_id": decision.decision_id,
        "terminal_turn_id": state["turn_id"],
        "terminal_run_id": state["run_id"],
        "terminal_response_path": "RAG",
        "terminal_content": content,
        "terminal_reason_code": reason,
        # 审核恢复不调用模型；用量仍归属于生成草稿的原 Run。
        "model_calls": [],
    }


def next_after_rag_review(state: dict) -> str:
    """重新生成回到检索节点，其余人工动作形成终局。"""

    return "retrieve_rag" if state.get("rag_review_outcome") is None else "end"
