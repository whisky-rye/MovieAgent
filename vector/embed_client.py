"""本地 Ollama 嵌入客户端（urllib 直连 /api/embed）。

为什么不用 langchain-ollama / ollama 官方 Client：
在本机实测，long-text 嵌入时 ollama 客户端库内部会发起 /api/tokenize 子请求，
该子请求走 OLLAMA_HOST 环境变量（本机指向 10305 的空实例），与 embed 主路径（11434）不一致而失败。
改用标准库 urllib 直连 OLLAMA_BASE_URL 的 /api/embed，服务端 truncate=True 自动截断超长文本，
完全绕开 tokenize 子调用的端口坑。
"""
import json
import urllib.request
from config.settings import settings


class OllamaEmbedClient:
    def __init__(self, base_url: str | None = None, model: str | None = None,
                 batch_size: int = 64, timeout: int = 600):
        self.base_url = (base_url or settings.OLLAMA_BASE_URL).rstrip("/")
        self.model = model or settings.EMBED_MODEL
        self.batch_size = batch_size
        self.timeout = timeout
        self._url = self.base_url + "/api/embed"

    def _post(self, texts):
        payload = json.dumps(
            {"model": self.model, "input": texts, "truncate": True}
        ).encode()
        req = urllib.request.Request(
            self._url, data=payload,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read())

    def embed(self, texts: list[str]) -> list[list[float]]:
        """批量嵌入（内部按 batch_size 分批，规避单次 /api/embed 的总 token 上限）。"""
        out: list[list[float]] = []
        for i in range(0, len(texts), self.batch_size):
            batch = texts[i: i + self.batch_size]
            r = self._post(batch)
            out.extend(r["embeddings"])
        return out

    def embed_query(self, text: str) -> list[float]:
        """单条查询嵌入（线上实时召回用，模型常驻热延迟约 300ms）。"""
        r = self._post([text])
        return r["embeddings"][0]

    @property
    def dim(self) -> int:
        """本地 bge-m3 默认 1024 维（与 EMBED_DIM 对齐）。"""
        return settings.EMBED_DIM


def get_embed_client():
    """嵌入客户端工厂：按 settings.EMBED_PROVIDER 切换本地/云端。

    - ollama（默认）：OllamaEmbedClient，本地 bge-m3，零成本零联网。
    - cloud        ：CloudEmbedClient，阿里百炼 qwen 嵌入（需联网+额度）。

    两客户端接口一致（embed / embed_query / model / dim），业务层（建库、召回）
    统一经本工厂获取，切换 Provider 无需改业务代码。
    """
    if settings.EMBED_PROVIDER == "cloud":
        # 延迟导入，避免未装 requests 时连 ollama 路径也报错
        from vector.cloud_embed import CloudEmbedClient
        return CloudEmbedClient()
    return OllamaEmbedClient()
