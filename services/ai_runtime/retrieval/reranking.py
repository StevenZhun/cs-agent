"""调用百炼文本精排接口，对有限候选重新排序。"""

from __future__ import annotations

from dataclasses import dataclass

import httpx


@dataclass(frozen=True)
class RerankScore:
    """一个输入文档在精排结果中的位置和相关性分数。"""

    index: int
    score: float


class DashScopeTextReranker:
    """封装百炼文本精排协议，供 RAG 和 FAQ 候选共同使用。"""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None,
        model: str,
        timeout_seconds: float,
    ) -> None:
        """保存连接配置；空密钥表示当前环境未启用云端精排。"""

        self._base_url = base_url.rstrip("/")
        self._api_key = api_key or ""
        self._model = model
        self._timeout_seconds = timeout_seconds

    async def rerank(
        self,
        query: str,
        documents: list[str],
        *,
        top_n: int | None = None,
    ) -> tuple[RerankScore, ...]:
        """返回按相关性降序排列的有限结果；未配置时返回空结果。"""

        if not self._api_key or not documents:
            return ()
        limit = min(top_n or len(documents), len(documents))
        endpoint = self._base_url + "/services/rerank/text-rerank/text-rerank"
        payload = {
            "model": self._model,
            "input": {"query": query, "documents": documents},
            "parameters": {"top_n": limit, "return_documents": False},
        }
        async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
            response = await client.post(
                endpoint,
                json=payload,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
            )
            response.raise_for_status()
            body = response.json()

        scores: list[RerankScore] = []
        seen: set[int] = set()
        for raw in body.get("output", {}).get("results", ()):
            index = int(raw["index"])
            score = float(raw["relevance_score"])
            if index in seen or not 0 <= index < len(documents) or not 0 <= score <= 1:
                raise ValueError("invalid rerank result")
            seen.add(index)
            scores.append(RerankScore(index=index, score=score))
        scores.sort(key=lambda item: item.score, reverse=True)
        return tuple(scores)
