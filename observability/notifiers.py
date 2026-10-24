"""告警通知器框架（P1-11）：多通道推送，预留飞书群机器人。

框架（Notifier 协议：send(alerts, state) -> bool，best-effort 绝不抛异常）：
- WebhookNotifier：通用 JSON webhook（保留原 ALERT_WEBHOOK_URL 行为）；
- FeishuBotNotifier：飞书**自定义群机器人**——凭证走 settings：
    ALERT_FEISHU_WEBHOOK  机器人 webhook 地址（https://open.feishu.cn/open-apis/bot/v2/hook/xxx）
    ALERT_FEISHU_SECRET   机器人「加签」密钥（可选，推荐配；配了则每次请求带
                          timestamp + HMAC-SHA256 签名）
  未配置 webhook = 通道自动禁用（no-op）；用户后续提供凭证填入 .env 即插即用，
  无需改代码。推送走卡片消息（interactive），按 severity 着色 + markdown 明细。

飞书自定义机器人协议要点（官方）：
- POST {webhook}，JSON: {"timestamp": "...", "sign": "...", "msg_type": "text"|"interactive", ...}
- 加签算法：timestamp=int(time.time())；string_to_sign=f"{timestamp}\\n{secret}；
  sign = base64(hmac_sha256(string_to_sign, secret))；timestamp 与 sign 同时下发。
- 频率限制：单机器人 100 次/分钟——生命周期去重（仅 firing/resolved 转换时推送）
  已从源头控量，此处再加失败重试一次后放弃。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
from typing import Any, Optional, Protocol

from config.settings import settings

logger = logging.getLogger("movie_agent.notify")

_HTTP_TIMEOUT = 5
_SEVERITY_COLOR = {"critical": "red", "warning": "orange", "info": "blue"}
_SEVERITY_ICON = {"critical": "🔴", "warning": "🟠", "info": "🔵"}


def _http_post_json(url: str, payload: dict, timeout: int = _HTTP_TIMEOUT) -> bool:
    """POST JSON（独立函数便于测试替换）。成功返回 True；任何失败返回 False。"""
    try:
        import requests

        r = requests.post(url, json=payload, timeout=timeout)
        ok = 200 <= r.status_code < 300
        if not ok:
            # 飞书业务错误码在 body里（如 19021 sign 校验失败），截断留痕
            logger.warning("通知推送 HTTP %s: %s", r.status_code, r.text[:200])
        return ok
    except Exception as e:  # noqa: BLE001 - best-effort，绝不抛异常阻断主链路
        logger.warning("通知推送失败（忽略）：%s", e)
        return False


class Notifier(Protocol):
    name: str

    def enabled(self) -> bool: ...

    def send(self, alerts: list[dict], state: str = "firing") -> bool: ...


class WebhookNotifier:
    """通用 JSON webhook（钉钉/飞书旧版/Slack incoming 等任意接收端）。"""

    name = "webhook"

    def enabled(self) -> bool:
        return bool((settings.ALERT_WEBHOOK_URL or "").strip())

    def send(self, alerts: list[dict], state: str = "firing") -> bool:
        url = (settings.ALERT_WEBHOOK_URL or "").strip()
        if not url or not alerts:
            return False
        payload = {"state": state, "alerts": alerts, "ts": int(time.time())}
        return _http_post_json(url, payload)


class FeishuBotNotifier:
    """飞书自定义群机器人（凭证来自 settings，未配置即禁用）。

    - text 模式：纯文本摘要（恢复通知用，轻量）；
    - interactive 卡片模式：告警明细（标题按最严重的 severity 着色）。
    """

    name = "feishu"

    def enabled(self) -> bool:
        return bool((settings.ALERT_FEISHU_WEBHOOK or "").strip())

    # ---- 协议层 ----

    def _sign(self) -> tuple[str, str]:
        """加签：(timestamp, sign)。未配 secret 时 sign 为空串（机器人未开启加签则合法）。

        官方算法：sign = base64(HMAC-SHA256(key=string_to_sign, msg=""))，
        其中 string_to_sign = f"{timestamp}\\n{secret}"（key 即待签串、消息为空）。
        """
        timestamp = str(int(time.time()))
        secret = (settings.ALERT_FEISHU_SECRET or "").strip()
        if not secret:
            return timestamp, ""
        string_to_sign = f"{timestamp}\n{secret}"
        code = hmac.new(string_to_sign.encode("utf-8"), digestmod=hashlib.sha256)
        sign = base64.b64encode(code.digest()).decode("utf-8")
        return timestamp, sign

    def _post(self, body: dict) -> bool:
        url = (settings.ALERT_FEISHU_WEBHOOK or "").strip()
        if not url:
            return False
        timestamp, sign = self._sign()
        payload = {"timestamp": timestamp, "sign": sign}
        payload.update(body)
        ok = _http_post_json(url, payload)
        if not ok:
            # 重试一次（网络抖动占多数；飞书限频 100 条/分钟，重试无压力）
            ok = _http_post_json(url, payload)
        return ok

    # ---- 消息构造 ----

    def _text_body(self, alerts: list[dict], state: str) -> dict:
        title = "告警恢复" if state == "resolved" else "告警触发"
        lines = [f"【影片推荐智能体 · {title}】"]
        for a in alerts[:10]:
            icon = _SEVERITY_ICON.get(a.get("severity", "warning"), "⚠️")
            lines.append(f"{icon} {a.get('name')}: {a.get('message')}")
        if len(alerts) > 10:
            lines.append(f"…等共 {len(alerts)} 条")
        return {"msg_type": "text", "content": {"text": "\n".join(lines)}}

    def _card_body(self, alerts: list[dict], state: str) -> dict:
        worst = min(
            (a.get("severity", "warning") for a in alerts),
            key=lambda s: {"critical": 0, "warning": 1, "info": 2}.get(s, 1),
        )
        title = "告警恢复" if state == "resolved" else "告警触发"
        elements: list[dict] = []
        for a in alerts[:10]:
            icon = _SEVERITY_ICON.get(a.get("severity", "warning"), "⚠️")
            elements.append({
                "tag": "markdown",
                "content": (f"{icon} **{a.get('name')}**\n"
                            f"{a.get('message')}\n"
                            f"当前值 `{a.get('value')}` · 阈值 `{a.get('threshold')}`"),
            })
        if len(alerts) > 10:
            elements.append({"tag": "markdown", "content": f"…等共 {len(alerts)} 条"})
        return {
            "msg_type": "interactive",
            "card": {
                "config": {"wide_screen_mode": True},
                "header": {
                    "template": _SEVERITY_COLOR.get(worst, "orange"),
                    "title": {
                        "tag": "plain_text",
                        "content": f"影片推荐智能体 · {title}（{len(alerts)} 条）",
                    },
                },
                "elements": elements,
            },
        }

    # ---- 出口 ----

    def send(self, alerts: list[dict], state: str = "firing") -> bool:
        if not alerts:
            return False
        body = self._card_body(alerts, state) if state == "firing" else self._text_body(alerts, state)
        return self._post(body)


def enabled_notifiers() -> list:
    """当前已配置的通知器列表（按配置动态装配，空配置零开销）。"""
    out: list = []
    for n in (WebhookNotifier(), FeishuBotNotifier()):
        try:
            if n.enabled():
                out.append(n)
        except Exception as e:  # noqa: BLE001 - 装配失败不影响其它通道
            logger.warning("通知器 %s 装配失败（忽略）：%s", getattr(n, "name", "?"), e)
    return out


def notify(alerts: list[dict], state: str = "firing") -> dict:
    """向所有已启用通道推送；返回 {notifier_name: bool}（best-effort）。"""
    results: dict[str, bool] = {}
    if not alerts:
        return results
    for n in enabled_notifiers():
        try:
            results[n.name] = bool(n.send(alerts, state))
        except Exception as e:  # noqa: BLE001
            logger.warning("通知器 %s 推送异常（忽略）：%s", n.name, e)
            results[n.name] = False
    return results
