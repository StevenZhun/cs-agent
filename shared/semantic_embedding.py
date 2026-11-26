"""Qwen3 Embedding 的配置和编码逻辑。"""

from __future__ import annotations

from dataclasses import dataclass

from sentence_transformers import SentenceTransformer


@dataclass(frozen=True)
class EmbeddingProfile:
    """固定一次向量索引构建所使用的模型和预处理规则。"""

    profile_id: str = "qwen3-embedding-0.6b-1024-v1"
    model_name: str = "Qwen/Qwen3-Embedding-0.6B"
    dimension: int = 1024
    corpus_max_tokens: int = 512
    shared_query_instruction: str = (
        "Given a customer-service query, retrieve the most semantically relevant "
        "intent similarities or FAQ questions."
    )
    intent_query_instruction: str = (
        "Given a customer-service query, retrieve the most relevant customer-service action intent."
    )
    faq_query_instruction: str = (
        "Given a customer-service query, retrieve the most relevant FAQ question."
    )
    # RAG 证据检索与意图/FAQ 问法检索是不同任务；同一模型、同一文本但任务指令
    # 不同时，生成的 Query 向量语义也不同，因此不能跨索引复用。
    rag_query_instruction: str = (
        "Given a customer-service question, retrieve passages from service "
        "knowledge documents that contain evidence to answer it."
    )


DEFAULT_EMBEDDING_PROFILE = EmbeddingProfile()


class QwenEmbeddingModel:
    """加载一次 Qwen3 Embedding，并明确区分语料编码和 Query 编码。"""

    def __init__(self, profile: EmbeddingProfile = DEFAULT_EMBEDDING_PROFILE) -> None:
        """按 Profile 加载模型；模型文件由 Hugging Face 全局缓存复用。"""

        self.profile = profile
        self.model = SentenceTransformer(profile.model_name)
        actual_dimension = self.model.get_embedding_dimension()
        if actual_dimension != profile.dimension:
            raise ValueError(
                f"Embedding 维度不一致：配置={profile.dimension}，模型={actual_dimension}"
            )

    @property
    def model_max_tokens(self) -> int:
        """返回当前 SentenceTransformer 实际采用的最大输入 Token 数。"""

        return int(self.model.max_seq_length)

    def count_tokens(self, text: str) -> int:
        """使用模型自己的 tokenizer 计数，不用字符数近似 Token 数。"""

        encoded = self.model.tokenizer(text, add_special_tokens=True, truncation=False)
        return len(encoded["input_ids"])

    def validate_corpus(self, records: list[tuple[str, str]]) -> None:
        """阻止过长语料进入向量索引，不对内容做静默截断。"""

        violations = [
            f"{record_id}={self.count_tokens(text)}"
            for record_id, text in records
            if self.count_tokens(text) > self.profile.corpus_max_tokens
        ]
        if violations:
            details = ", ".join(violations[:10])
            raise ValueError(
                f"语义语料超过 {self.profile.corpus_max_tokens} Token，请拆分或重写：{details}"
            )

    def encode_documents(
        self, texts: list[str], *, show_progress: bool = False
    ) -> list[list[float]]:
        """编码离线语料；文档侧不添加 Query 任务指令。"""

        self._ensure_model_limit(texts)
        vectors = self.model.encode(
            texts,
            batch_size=16,
            convert_to_numpy=True,
            normalize_embeddings=True,
            show_progress_bar=show_progress,
        )
        return vectors.tolist()

    def encode_queries(
        self,
        texts: list[str],
        *,
        instruction: str,
        show_progress: bool = False,
    ) -> list[list[float]]:
        """编码在线或批量 Query；任务指令只加在 Query 一侧。"""

        instructed = [f"Instruct: {instruction}\nQuery: {text}" for text in texts]
        self._ensure_model_limit(instructed)
        vectors = self.model.encode(
            instructed,
            batch_size=16,
            convert_to_numpy=True,
            normalize_embeddings=True,
            show_progress_bar=show_progress,
        )
        return vectors.tolist()

    def _ensure_model_limit(self, texts: list[str]) -> None:
        """在底层模型截断前主动报错，保证参与计算的文本可审计。"""

        violations = [
            f"第{index}条={self.count_tokens(text)}"
            for index, text in enumerate(texts, start=1)
            if self.count_tokens(text) > self.model_max_tokens
        ]
        if violations:
            details = ", ".join(violations[:10])
            raise ValueError(
                f"输入超过模型最大长度 {self.model_max_tokens} Token，不能静默截断：{details}"
            )
