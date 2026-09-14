"""云端嵌入客户端（阿里百炼 compatible-mode OpenAI 兼容 /v1/embeddings）。

与本地 OllamaEmbedClient 实现同一接口（embed / embed_query），
由 vector/embed_client.py:get_embed_client() 按 EMBED_PROVIDER 开关选择，
业务层无感切换。

接入点：阿里百炼 compatible-mode 网关（与 CLOUD_LLM_BASE_URL 同一地址），
模型 qwen3.7-text-embedding（亦支持 text-embedding-v3/v4 等）。
qwen 嵌入支持动态维度（dimensions 参数），故 EMBED_DIM 可自由配置。

为什么不复用 langchain-openai 的 embeddings：
  1) 该网关对 embeddings 的返回结构与 batch 处理有定制，原生日 urllib/requests
     直连最稳、零额外依赖；
  2) 与项目 embed_client 的 urllib 风格一致，便于排查（失败直接抛 HTTP 状态码）。
"""
import json

import requests
from config.settings import settings


class CloudEmbedClient:
    """OpenAI 兼容的云端文本嵌入客户端（默认接阿里百炼 compatible-mode）。"""

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        dim: int | None = None,
        use_dimensions: bool | None = None,
        batch_size: int = 20,
        timeout: int = 120,
    ):
        # base_url 默认复用云端 LLM 网关（阿里百炼同一 compatible-mode 入口）
        self.base_url = (
            base_url or settings.CLOUD_EMBED_BASE_URL or settings.CLOUD_LLM_BASE_URL
        ).rstrip("/")
        # api_key 默认复用云端 LLM Key（同一供应商）
        self.api_key = (
            api_key or settings.CLOUD_EMBED_API_KEY or settings.CLOUD_LLM_API_KEY
        )
        self.model = model or settings.CLOUD_EMBED_MODEL
        self.dim = dim or settings.EMBED_DIM
        self.use_dimensions = (
            settings.CLOUD_EMBED_USE_DIMENSIONS if use_dimensions is None else use_dimensions
        )
        # 阿里百炼嵌入接口单次批量上限 20（超出报 invalid_parameter_error），统一夹到 20
        self.batch_size = min(batch_size, 20)
        self.timeout = timeout
        self._url = self.base_url + "/embeddings"
        if not self.api_key:
            raise ValueError(
                "CloudEmbedClient 需要 API Key：请在 .env 配置 CLOUD_EMBED_API_KEY "
                "或 CLOUD_LLM_API_KEY（阿里百炼）。"
            )

    def _post(self, texts: list[str]) -> list[list[float]]:
        body: dict = {"model": self.model, "input": texts}
        if self.use_dimensions:
            body["dimensions"] = self.dim
        resp = requests.post(
            self._url,
            data=json.dumps(body).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            timeout=self.timeout,
        )
        if resp.status_code != 200:
            raise RuntimeError(
                f"云端嵌入失败 HTTP={resp.status_code}: {resp.text[:300]}"
            )
        data = resp.json()
        # OpenAI 兼容返回：data[i].embedding，按输入顺序排列；校验长度防止错位
        embs = [d["embedding"] for d in data["data"]]
        if len(embs) != len(texts):
            raise RuntimeError(
                f"云端嵌入返回条数不匹配：期望 {len(texts)}，实得 {len(embs)}"
            )
        return embs

    def embed(self, texts: list[str]) -> list[list[float]]:
        """批量嵌入（内部按 batch_size 分批，控制单次请求体大小）。"""
        out: list[list[float]] = []
        for i in range(0, len(texts), self.batch_size):
            out.extend(self._post(texts[i : i + self.batch_size]))
        return out

    def embed_query(self, text: str) -> list[float]:
        """单条查询嵌入（线上实时召回用）。"""
        return self._post([text])[0]
