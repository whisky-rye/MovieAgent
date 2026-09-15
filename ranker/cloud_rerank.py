"""云端重排序模型客户端（阿里百炼 qwen3.7-text-rerank 等）。

与本地 LIGHT pointwise 精排（ranker.llm_rerank.llm_rerank）提供同等能力，
由 ranker.rerank 的 RERANK_PROVIDER 开关选择；业务层无感切换。

为什么不复用 langchain / dashscope SDK：
  1) 百炼重排走专用端点 /api/v1/services/rerank/text-rerank/text-rerank，
     与 LLM/嵌入的 compatible-mode/v1 路径不同，直连 urllib/requests 最稳、零额外依赖；
  2) 请求/响应结构扁平易懂，失败直接抛 HTTP 状态码，便于排查与回退。

接口细节（官方文档，2026-09 更新）：
  - 模型 qwen3.7-text-rerank 单批最多 500 篇文档、单篇 ≤30000 token，
    推荐场景「前 N 精排」远不会触顶，无需分批。
  - 返回 output.results[]，每项 {index, relevance_score(0~1)}，按相关性降序；
    relevance_score 为本次请求内相对分，仅用于本次排序。
"""
import json
import logging

import requests
from config.settings import settings
from retrieval.candidates import Candidate

logger = logging.getLogger("movie_agent.ranker.cloud_rerank")

# 重排序任务指令（英文，对齐百炼建议）。推荐场景用「匹配用户偏好」语义，
# 比默认 web-search 指令更贴合「找符合口味的片子」。可在 .env 关掉（用模型默认）。
DEFAULT_INSTRUCT = (
    "Given a user's movie preference query, retrieve the most relevant movies "
    "that best match the user's described taste, intent, and constraints."
)

# 与本地 LIGHT 精排一致的融合权重：0.7 融合分 + 0.3 重排相关性分
_FUSION_W = 0.7
_RERANK_W = 0.3


class CloudRerankClient:
    """阿里百炼文本重排序模型客户端（默认 qwen3.7-text-rerank）。"""

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        use_instruct: bool | None = None,
        timeout: int = 60,
    ):
        # api_key 默认复用云端 LLM/嵌入的同一 Key（同一供应商）
        self.api_key = (
            api_key or settings.CLOUD_RERANK_API_KEY or settings.CLOUD_LLM_API_KEY
        )
        self.model = model or settings.CLOUD_RERANK_MODEL
        self.use_instruct = (
            settings.CLOUD_RERANK_USE_INSTRUCT if use_instruct is None else use_instruct
        )
        self.timeout = timeout
        # 端点与 LLM/嵌入不同路径，单独推导（见 _derive_url）
        self._url = base_url or self._derive_url()
        if not self.api_key:
            raise ValueError(
                "CloudRerankClient 需要 API Key：请在 .env 配置 CLOUD_RERANK_API_KEY "
                "或 CLOUD_LLM_API_KEY（阿里百炼，与云端 LLM/嵌入同源同 Key）。"
            )

    @staticmethod
    def _derive_url() -> str:
        """从 CLOUD_RERANK_BASE_URL 或 CLOUD_LLM_BASE_URL 推导百炼重排端点。"""
        base = (
            settings.CLOUD_RERANK_BASE_URL or settings.CLOUD_LLM_BASE_URL or ""
        ).rstrip("/")
        if not base:
            raise ValueError(
                "未配置重排端点：请设置 CLOUD_RERANK_BASE_URL 或 CLOUD_LLM_BASE_URL"
            )
        # 已是完整重排端点
        if "services/rerank/text-rerank/text-rerank" in base:
            return base
        # compatible-mode/v1 → 换成重排专用路径
        if base.endswith("/compatible-mode/v1"):
            return (
                base[: -len("/compatible-mode/v1")]
                + "/api/v1/services/rerank/text-rerank/text-rerank"
            )
        if base.endswith("/v1"):
            return base + "/services/rerank/text-rerank/text-rerank"
        return base + "/api/v1/services/rerank/text-rerank/text-rerank"

    @staticmethod
    def _doc_text(c: Candidate) -> str:
        """把候选片构造为送重排的文档串（标题+年份+类型+简介片段）。"""
        snippet = (c.doc or "")[:300]
        parts = [c.title]
        if c.year:
            parts.append(f"（{c.year}）")
        if c.genres:
            parts.append("类型：" + "/".join(c.genres))
        text = "".join(parts)
        if snippet:
            text += "。" + snippet
        return text

    def rerank(
        self, query: str, documents: list[str], top_n: int | None = None
    ) -> list[tuple[int, float]]:
        """返回 [(原始下标, 相关性得分 0~1), ...]，按相关性降序。"""
        body: dict = {
            "model": self.model,
            "input": {"query": query, "documents": documents},
            "parameters": {},
        }
        if top_n is not None:
            body["parameters"]["top_n"] = top_n
        if self.use_instruct:
            body["parameters"]["instruct"] = DEFAULT_INSTRUCT
        if not body["parameters"]:
            del body["parameters"]

        resp = requests.post(
            self._url,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            timeout=self.timeout,
        )
        if resp.status_code != 200:
            raise RuntimeError(
                f"云端重排序失败 HTTP={resp.status_code}: {resp.text[:300]}"
            )
        data = resp.json()
        if data.get("code"):  # 业务错误码（成功时不返回）
            raise RuntimeError(
                f"云端重排序返回错误 code={data.get('code')}: {data.get('message')}"
            )
        results = data.get("output", {}).get("results")
        if results is None and "results" in data:  # qwen3-rerank 兼容兜底
            results = data["results"]
        if not results:
            raise RuntimeError("云端重排序返回空结果")
        return [(int(r["index"]), float(r["relevance_score"])) for r in results]


def _cand_dict(c: Candidate) -> dict:
    return {
        k: getattr(c, k)
        for k in (
            "id", "source", "title", "year", "genres", "country",
            "rating", "rating_count", "doc", "recall_source",
        )
    }


def cloud_rerank(
    cands: list[Candidate], query: str | None, top_k: int | None = None
) -> list[Candidate]:
    """用阿里百炼 qwen3.7-text-rerank 对候选重排，与本地 LIGHT 精排同权重融合。

    融合逻辑保持与 ranker.llm_rerank.llm_rerank 一致（0.7*融合分 + 0.3*相关性分），
    这样切换 provider 时排序风格稳定、可对比。
    """
    if not query or not cands:
        cands = sorted(cands, key=lambda c: c.score, reverse=True)
        return cands[:top_k] if top_k else cands
    client = CloudRerankClient()
    docs = [CloudRerankClient._doc_text(c) for c in cands]
    scored_pairs = client.rerank(query, docs, top_n=len(docs))
    scored: list[Candidate] = []
    for idx, rel in scored_pairs:
        if idx < 0 or idx >= len(cands):
            continue
        c = cands[idx]
        fused = _FUSION_W * c.score + _RERANK_W * rel
        scored.append(Candidate(**{**_cand_dict(c), "score": fused}))
    scored.sort(key=lambda x: x.score, reverse=True)
    return scored[:top_k] if top_k else scored
