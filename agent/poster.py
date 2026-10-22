"""推荐海报生成（G-Poster）：每次对话完成后，调用阿里百炼视觉模型为推荐列表 Top3 生成宣传海报。

设计要点：
- 随机触发文生图模型（POSTER_MODELS）：qwen-image-3.0-pro / qwen-image-3.0 / qwen-image-2.0-pro-2026-06-22。
  （说明：qwen-mt-image-2.0 是图片翻译、wan*/happyhorse* 是文生视频，均非"文生图"，不用于海报。）
- 提示词结合用户长期记忆/偏好（agent.memory 的 preferences：偏好类型、备注）做个性化适配。
- 与项目既有「零额外依赖、requests + Bearer 直连百炼」范式一致（镜像 ranker.cloud_rerank）。
- 统一走北京 MaaS 网关的 multimodal-generation/generation 接口（文生图/文生视频共用此路径），
  为异步任务，轮询 tasks 接口取结果。
- 结果媒体（png/jpg/webp）下载落盘到 settings.POSTER_DATA_DIR，状态写入 manifest.json 供前端轮询。
- 每次调用详情（提示词、输入/输出 token、模型、耗时、产物 URL）写入专用日志 log/bailian_calls.log
  （logger=movie_agent.bailian，复用 config.logging_setup 的 JsonFormatter/EXTRA_KEYS）。
- 全程异常安全：任一环节失败只记日志并把 manifest 标为 error，绝不阻塞主推荐链路。
"""
from __future__ import annotations

import json
import logging
import os
import random
import re
import threading
import time
from typing import Any, Optional

import requests

from config.settings import settings
from observability.metrics import metrics

logger = logging.getLogger("movie_agent.bailian")

# 随机触发的文生图模型（北京 MaaS 实际可用）；图片类（image）异步合成后轮询取结果。
# 若后续想加入已部署的 wan*/happyhorse* 文生视频模型做"视频海报"，追加到本列表并在 _model_family 识别即可。
POSTER_MODELS: list[str] = [
    "qwen-image-3.0-pro",
    "qwen-image-3.0",
    "qwen-image-2.0-pro-2026-06-22",
]


def _model_family(model: str) -> str:
    """图片类 or 视频类。"""
    m = (model or "").lower()
    if "image" in m:
        return "image"
    return "video"


def pick_model() -> str:
    """随机选一款文生图模型。"""
    return random.choice(POSTER_MODELS)


def resolve_base_url() -> str:
    """解析北京 MaaS 网关 base：优先显式 CLOUD_POSTER_BASE_URL，其次用 WorkspaceId 拼专属域名，最后回退 LLM base。"""
    if settings.CLOUD_POSTER_BASE_URL:
        return settings.CLOUD_POSTER_BASE_URL.rstrip("/")
    if settings.POSTER_WORKSPACE_ID:
        region = settings.POSTER_REGION or "cn-beijing"
        return f"https://{settings.POSTER_WORKSPACE_ID}.{region}.maas.aliyuncs.com/api/v1"
    return settings.CLOUD_LLM_BASE_URL.rstrip("/")


# —— 提示词构造 ——
def build_prompt(
    top_items: list[dict],
    prefs: Optional[dict] = None,
    query: str = "",
) -> str:
    """为中文视觉模型构造「Top3 合一张」宣传海报提示词。

    top_items：每个含 title/year/genres/country/rating 等；prefs：用户长期偏好（genres/note）。
    风格适配：优先贴合用户偏好类型；否则从作品类型推导视觉基调。
    """
    prefs = prefs or {}
    fav_genres = prefs.get("genres") or []
    note = prefs.get("note") or ""

    lines = []
    for i, it in enumerate(top_items[:3], 1):
        title = it.get("title", "")
        year = it.get("year") or ""
        genres = "/".join(it.get("genres") or [])
        country = it.get("country") or ""
        rating = it.get("rating") or ""
        seg = f"{i}.《{title}》"
        if year:
            seg += f"（{year}）"
        if genres:
            seg += f"——{genres}"
        if country:
            seg += f"，{country}"
        if rating:
            seg += f"，评分 {rating}"
        lines.append(seg)

    works = "\n".join(lines)

    # 个性化风格基调：优先用用户偏好类型，否则泛化
    if fav_genres:
        style_hint = "、".join(fav_genres[:4])
        personal = (
            f"整体视觉风格请贴合该用户长期偏好的「{style_hint}」基调，"
            "在构图与色彩上呼应其口味，使海报一眼即合其审美。"
        )
    else:
        personal = "整体视觉风格请呼应这三部作品共通的情绪与类型，形成统一而有张力的基调。"

    if note:
        personal += f" 额外偏好提示：{note}。"

    prompt = (
        "创作一张电影推荐宣传海报，聚焦以下三部高分作品的精选合集（仅展示这三部，无需罗列全部）：\n"
        f"{works}\n\n"
        "海报要求：\n"
        "1. 呈现三部作品共同的总体氛围场景，或采用契合此类影视剧情内容的视觉风格；\n"
        "2. 画面为电影感巨幅海报构图，主体醒目，留白处可用艺术字呈现作品名；\n"
        "3. 色调与光影服务于情绪表达，具有强烈的沉浸感与传播力；\n"
        "4. 避免杂乱文字堆砌，仅保留作品名与少量氛围关键词。\n"
        f"{personal}\n"
    )
    if query:
        prompt += f"本次用户的观影诉求参考：「{query}」。"
    return prompt.strip()


class BaiLianVisualClient:
    """阿里百炼视觉模型（图像/视频合成）客户端，零额外依赖直连。"""

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: int | None = None,
    ):
        self.api_key = api_key or settings.CLOUD_POSTER_API_KEY or settings.CLOUD_LLM_API_KEY
        self.base_url = (base_url or resolve_base_url()).rstrip("/")
        # 视觉模型为阻塞式长轮询（一次 POST 直出结果，常需 30–120s+），超时须留足余量
        self.timeout = timeout or 300
        if not self.api_key:
            raise ValueError(
                "BaiLianVisualClient 需要 API Key：请在 .env 配置 CLOUD_POSTER_API_KEY "
                "或 CLOUD_LLM_API_KEY（阿里百炼，与云端 LLM/嵌入同源同 Key）。"
            )

    # —— 端点拼接 ——
    def _endpoint_url(self) -> str:
        """百炼模型服务统一生成接口（北京 MaaS 网关）；文生图/文生视频共用此路径。"""
        return self.base_url + settings.POSTER_ENDPOINT_PATH

    def _tasks_url(self, task_id: str) -> str:
        return self.base_url + f"/tasks/{task_id}"

    # —— 调用 ——
    def generate(self, model: str, prompt: str, session_id: str | None = None) -> dict:
        """调用百炼视觉模型，返回结果 dict（含落盘 file_path / media_type / token 用量等）。"""
        family = _model_family(model)
        t0 = time.time()
        out: dict[str, Any] = {
            "model": model,
            "model_family": family,
            "prompt": prompt,
            "session_id": session_id,
            "input_tokens": 0,
            "output_tokens": 0,
            "latency_ms": 0,
            "media_type": None,
            "file_path": None,
            "poster_url_public": None,
            "error": None,
        }
        try:
            url = self._endpoint_url()
            # 百炼「模型服务」统一生成接口（multimodal-generation/generation）采用 chat 风格
            # input.messages 结构；纯文生图时 content 为一个 {"text": 提示词} 片段。
            body = {
                "model": model,
                "input": {
                    "messages": [
                        {"role": "user", "content": [{"text": prompt}]}
                    ]
                },
            }
            if family == "image":
                body["parameters"] = {"size": "1024*1024", "n": 1}
            else:
                body["parameters"] = {"duration": 5, "size": "1280*720"}
            resp = requests.post(
                url,
                data=json.dumps(body).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self.api_key}",
                },
                timeout=self.timeout,
            )
            if resp.status_code != 200:
                raise RuntimeError(f"百炼视觉模型 HTTP={resp.status_code}: {resp.text[:400]}")
            data = resp.json()
            if data.get("code"):
                raise RuntimeError(f"百炼视觉模型错误 code={data.get('code')}: {data.get('message')}")

            self._extract_usage(data, out)

            # 统一生成接口多为「同步 chat 风格」响应：output.choices[].message.content[].{image|video}
            result_url = self._extract_result_url(data)
            if not result_url:
                # 视频类模型可能为异步任务，返回 task_id → 轮询 tasks 接口
                task_id = data.get("output", {}).get("task_id") or data.get("task_id")
                if not task_id:
                    raise RuntimeError("百炼未返回结果媒体 URL 也未返回 task_id，无法继续")
                out["task_id"] = task_id
                result_url = self._poll_task(task_id, family)

            out["poster_url_public"] = result_url
            out["file_path"] = self._download(result_url, out, session_id)
        except Exception as e:  # 任意失败：记录但不上抛，交由调度层标 error
            out["error"] = f"{type(e).__name__}: {e}"
            logger.warning("海报生成异常 model=%s: %s", model, out["error"])
        out["latency_ms"] = int((time.time() - t0) * 1000)
        self._log_call(out)
        return out

    @staticmethod
    def _extract_usage(data: dict, out: dict) -> None:
        usage = data.get("usage") or {}
        if isinstance(usage, dict):
            out["input_tokens"] = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
            out["output_tokens"] = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
            if usage.get("image_count") is not None:
                out["image_count"] = int(usage["image_count"])

    def _extract_result_url(self, data: dict) -> Optional[str]:
        """从统一生成接口的响应中抽取结果媒体 URL（兼容同步 chat 风格与旧 results 风格）。"""
        output = data.get("output", {})
        if not isinstance(output, dict):
            return None
        # 1) 同步 chat 风格：choices[].message.content[].{image|video|url}
        choices = output.get("choices") or []
        for ch in choices:
            msg = ch.get("message", {})
            content = msg.get("content") if isinstance(msg, dict) else None
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict):
                        for key in ("image", "video", "url", "image_url"):
                            if part.get(key):
                                return part[key]
            elif isinstance(content, str) and content.startswith("http"):
                return content
        # 2) 旧式 results[].{url|image_url}
        results = output.get("results")
        if isinstance(results, list) and results:
            for r in results:
                if isinstance(r, dict):
                    for key in ("url", "image_url", "video_url"):
                        if r.get(key):
                            return r[key]
        return None

    def _poll_task(self, task_id: str, family: str) -> str:
        """轮询异步任务直到 SUCCEEDED，返回结果媒体 URL。"""
        deadline = time.time() + settings.POSTER_TASK_TIMEOUT
        interval = max(1, settings.POSTER_TASK_POLL)
        while time.time() < deadline:
            time.sleep(interval)
            resp = requests.get(
                self._tasks_url(task_id),
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=self.timeout,
            )
            if resp.status_code != 200:
                raise RuntimeError(f"轮询任务 HTTP={resp.status_code}: {resp.text[:300]}")
            data = resp.json()
            output = data.get("output", {})
            status = output.get("task_status") or data.get("task_status")
            if status == "SUCCEEDED":
                url = self._extract_result_url(data)
                if url:
                    return url
                raise RuntimeError("任务成功但未携带结果媒体 URL")
            if status in ("FAILED", "CANCELED"):
                raise RuntimeError(f"任务终止 status={status}: {output.get('message') or data.get('message')}")
        raise RuntimeError(f"海报任务超时未结束（>{settings.POSTER_TASK_TIMEOUT}s）")

    @staticmethod
    def _apply_media_type(out: dict) -> None:
        """根据结果/下载的实际 content-type 校准 media_type 与扩展名。"""
        ct = (out.get("media_type") or "").lower()
        if "jpg" in ct or "jpeg" in ct:
            out["_ext"] = "jpg"
        elif "png" in ct:
            out["_ext"] = "png"
        elif "webp" in ct:
            out["_ext"] = "webp"
        elif "gif" in ct:
            out["_ext"] = "gif"
        elif "mp4" in ct:
            out["_ext"] = "mp4"
        elif "webm" in ct:
            out["_ext"] = "webm"
        else:
            out["_ext"] = "png" if out.get("model_family") == "image" else "mp4"

    def _download(self, url: str, out: dict, session_id: str | None = None) -> str:
        """把百炼返回的临时 URL 下载到本地落盘，返回本地路径。

        文件名确定性地按 session_id 命名（{sid}.{ext}），保证 manifest.file 与磁盘始终一致，
        避免旧实现用时间戳命名时 manifest 指向已被清理文件、下载路由 404 把 HTML 当图片存的缺陷。
        无 session_id（兜底调用）时回退时间戳命名。
        """
        os.makedirs(settings.POSTER_DATA_DIR, exist_ok=True)
        resp = requests.get(url, timeout=self.timeout * 2, stream=True)
        resp.raise_for_status()
        ct = resp.headers.get("Content-Type", "")
        if ct:
            out["media_type"] = ct
        self._apply_media_type(out)
        ext = out.pop("_ext", "png")
        if session_id:
            safe = re.sub(r"[^A-Za-z0-9._-]", "_", str(session_id))[:120]
            dest = os.path.join(settings.POSTER_DATA_DIR, f"{safe}.{ext}")
            # 同名先删，避免覆盖时的残留/锁问题
            if os.path.isfile(dest):
                try:
                    os.remove(dest)
                except OSError:
                    pass
        else:
            dest = os.path.join(settings.POSTER_DATA_DIR, f"{int(time.time()*1000)}.{ext}")
        with open(dest, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
        return dest

    def _log_call(self, out: dict) -> None:
        """写入专用日志 movie_agent.bailian（log/bailian_calls.log）。"""
        logger.info(
            "poster_generate model=%s family=%s in=%s out=%s latency=%sms %s",
            out.get("model"), out.get("model_family"),
            out.get("input_tokens"), out.get("output_tokens"), out.get("latency_ms"),
            "OK" if out.get("file_path") else f"FAIL:{out.get('error')}",
            extra={
                "event": "poster_generate",
                "provider": "bailian",
                "model": out.get("model"),
                "model_family": out.get("model_family"),
                "input_text": out.get("prompt"),
                "input_tokens": out.get("input_tokens"),
                "output_tokens": out.get("output_tokens"),
                "latency_ms": out.get("latency_ms"),
                "poster_url": out.get("poster_url_public"),
                "media_type": out.get("media_type"),
                "task_id": out.get("task_id"),
                "image_size": "1024*1024" if out.get("model_family") == "image" else None,
                "endpoint": settings.POSTER_ENDPOINT_PATH,
                "base_url": self.base_url,
            },
        )


# —— 状态持久化（manifest）——
# 注意：manifest 每次都从磁盘读取，不使用进程内长期缓存。原因：
# schedule_poster 在后台线程写盘，而多 worker(uvicorn --workers N) 时各 worker 进程独立，
# 若缓存则其它 worker 的轮询请求读不到新海报 → 404。manifest 体积极小，每次读盘开销可忽略。
_manifest_lock = threading.Lock()


def _manifest_path() -> str:
    return os.path.join(settings.POSTER_DATA_DIR, "manifest.json")


def _load_manifest() -> dict:
    with _manifest_lock:
        p = _manifest_path()
        if os.path.isfile(p):
            try:
                return json.load(open(p, encoding="utf-8"))
            except Exception:
                return {}
        return {}


def _save_manifest(m: dict) -> None:
    with _manifest_lock:
        os.makedirs(settings.POSTER_DATA_DIR, exist_ok=True)
        json.dump(m, open(_manifest_path(), "w", encoding="utf-8"), ensure_ascii=False, indent=2)


def get_poster_status(session_id: str) -> dict:
    """前端轮询：返回该会话最新海报状态。"""
    m = _load_manifest()
    return m.get(session_id, {"status": "none"})


def _update_status(session_id: str, patch: dict) -> None:
    m = _load_manifest()
    cur = m.get(session_id, {})
    cur.update(patch)
    cur["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    m[session_id] = cur
    _save_manifest(m)


def _placeholder(session_id: str, prompt: str, model: str) -> Optional[str]:
    """断网/失败时的占位图（仅当 POSTER_FALLBACK_PLACEHOLDER 开启且 PIL 可用）。"""
    if not settings.POSTER_FALLBACK_PLACEHOLDER:
        return None
    try:
        from PIL import Image, ImageDraw
    except Exception:
        return None
    os.makedirs(settings.POSTER_DATA_DIR, exist_ok=True)
    dest = os.path.join(settings.POSTER_DATA_DIR, f"{session_id}.png")
    img = Image.new("RGB", (1024, 1024), (30, 33, 38))
    d = ImageDraw.Draw(img)
    d.text((40, 480), "海报生成暂不可用（演示占位）", fill=(230, 231, 234))
    img.save(dest)
    return dest


def schedule_poster(
    session_id: str,
    user_id: str,
    request_id: str,
    items: list[dict],
    query: str = "",
) -> None:
    """在对话 done 后调用：异步（守护线程）生成 Top3 海报，写入 manifest 供前端轮询。"""
    if not items:
        return
    top3 = items[:3]
    # 先把状态置为 pending，让前端立刻进入轮询
    _update_status(session_id, {"status": "pending", "request_id": request_id, "created_at": time.strftime("%Y-%m-%dT%H:%M:%S")})
    metrics.incr("poster_generations_total", 1, {"status": "started"})

    def worker() -> None:
        try:
            from agent.memory import get_memory_store

            prefs = {}
            try:
                prefs = get_memory_store().get_prefs(user_id) or {}
            except Exception as e:
                logger.warning("读取用户偏好失败（海报个性化跳过）：%s", e)

            prompt = build_prompt(top3, prefs, query)
            model = pick_model()
            client = BaiLianVisualClient()
            res = client.generate(model, prompt, session_id)

            if res.get("file_path"):
                rel = os.path.basename(res["file_path"])
                _update_status(session_id, {
                    "status": "ready",
                    "model": model,
                    "model_family": res.get("model_family"),
                    "media_type": res.get("media_type"),
                    "file": rel,
                    "url": f"/poster/download/{session_id}",
                    "download_url": f"/poster/download/{session_id}?download=1",
                    "prompt": prompt,
                })
                metrics.incr("poster_generations_total", 1, {"status": "ready"})
            else:
                ph = _placeholder(session_id, prompt, model)
                if ph:
                    rel = os.path.basename(ph)
                    _update_status(session_id, {
                        "status": "ready", "placeholder": True, "model": model,
                        "model_family": res.get("model_family"), "media_type": "image/png",
                        "file": rel, "url": f"/poster/download/{session_id}",
                        "download_url": f"/poster/download/{session_id}?download=1",
                        "prompt": prompt,
                    })
                    metrics.incr("poster_generations_total", 1, {"status": "placeholder"})
                else:
                    _update_status(session_id, {"status": "error", "error": res.get("error") or "生成失败"})
                    metrics.incr("poster_generations_total", 1, {"status": "error"})
        except Exception as e:
            logger.warning("schedule_poster 线程异常 session=%s: %s", session_id, e)
            _update_status(session_id, {"status": "error", "error": f"{type(e).__name__}: {e}"})
            metrics.incr("poster_generations_total", 1, {"status": "error"})

    t = threading.Thread(target=worker, name=f"poster-{session_id}", daemon=True)
    t.start()
