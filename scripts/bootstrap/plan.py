"""定义空环境初始化所需的内容分组、版本和执行顺序。"""

from __future__ import annotations

from dataclasses import dataclass

FAQ_RELEASE_VERSION = "faq-release-v1"
INTENT_RELEASE_VERSION = "routing-release-v2"
POLICY_ANSWER_RELEASE_VERSION = "policy-answer-release-v1"
SOP_RELEASE_VERSION = "sop-release-v1"
SOP_SOURCE_REF = "bootstrap:sop:v1"


@dataclass(frozen=True)
class InitializationSection:
    """一组具有相同业务目的、必须按顺序执行的初始化命令。"""

    label: str
    commands: tuple[tuple[str, ...], ...]


def faq_section(python: str) -> InitializationSection:
    """准备、审核并发布 FAQ 与高风险策略答案。"""

    return InitializationSection(
        label="FAQ 与策略答案",
        commands=(
            (python, "-m", "scripts.bootstrap.faq_content", "seed"),
            (python, "-m", "scripts.bootstrap.policy_intents", "seed"),
            (python, "-m", "scripts.bootstrap.faq_variants"),
            (python, "-m", "scripts.bootstrap.faq_content", "approve"),
            (
                python,
                "-m",
                "scripts.bootstrap.faq_content",
                "publish",
                "--release",
                FAQ_RELEASE_VERSION,
            ),
            (python, "-m", "scripts.bootstrap.intent_content", "seed"),
            (python, "-m", "scripts.bootstrap.intent_content", "approve"),
            (
                python,
                "-m",
                "scripts.bootstrap.policy_intents",
                "approve",
                "--allow-empty",
            ),
            (python, "-m", "scripts.bootstrap.policy_intents", "publish"),
        ),
    )


def intent_catalog_section(python: str) -> InitializationSection:
    """准备 FAQ 到意图映射和确定性路由规则的活动目录。"""

    return InitializationSection(
        label="全局意图目录",
        commands=(
            (python, "-m", "scripts.bootstrap.faq_intent_projection", "seed"),
            (python, "-m", "scripts.bootstrap.faq_intent_projection", "approve"),
            (python, "-m", "scripts.bootstrap.routing_rules", "seed"),
            (
                python,
                "-m",
                "scripts.bootstrap.routing_rules",
                "approve",
                "--allow-empty",
            ),
            (
                python,
                "-m",
                "scripts.bootstrap.routing_rules",
                "publish",
                "--release",
                INTENT_RELEASE_VERSION,
            ),
        ),
    )


def semantic_index_section(python: str, *, semantic_index_version: str) -> InitializationSection:
    """为全部已启用意图和 FAQ 构建可重建的 Milvus 查询投影。"""

    return InitializationSection(
        label="意图与 FAQ 语义索引",
        commands=(
            (
                python,
                "-m",
                "scripts.bootstrap.semantic_index",
                "build",
                "--index-version",
                semantic_index_version,
            ),
        ),
    )


def sop_section(python: str) -> InitializationSection:
    """准备、审核并发布 SOP 流程及其场景意图语料。"""

    return InitializationSection(
        label="SOP 流程",
        commands=(
            (
                python,
                "-m",
                "scripts.bootstrap.sop_content",
                "seed",
                "--version",
                "sop-v1",
                "--source",
                SOP_SOURCE_REF,
            ),
            (
                python,
                "-m",
                "scripts.bootstrap.sop_content",
                "approve",
                "--source",
                SOP_SOURCE_REF,
            ),
            (
                python,
                "-m",
                "scripts.bootstrap.sop_content",
                "publish",
                "--source",
                SOP_SOURCE_REF,
                "--release-version",
                SOP_RELEASE_VERSION,
            ),
            (python, "-m", "scripts.bootstrap.scene_intent_content", "seed"),
            (python, "-m", "scripts.bootstrap.scene_intent_content", "approve"),
        ),
    )


def initialization_sections(
    *, python: str, semantic_index_version: str
) -> tuple[InitializationSection, ...]:
    """返回完整初始化计划；SOP 发布后才构建包含场景意图的语义索引。"""

    return (
        faq_section(python),
        intent_catalog_section(python),
        sop_section(python),
        semantic_index_section(python, semantic_index_version=semantic_index_version),
    )
