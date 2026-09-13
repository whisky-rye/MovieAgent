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
