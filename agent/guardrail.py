"""P5 补充：敏感拒答 / 越域引导护栏（合规第一道闸门）。

设计：
- 零额度优先：默认 GUARDRAIL_MODE=rule，纯本地正则 + 启发式，不调任何 LLM。
- 两类拦截：
  1) sensitive（硬拒答）：涉政分裂/暴恐、色情、毒品、赌博诈骗、危险武器制造、自残自杀、仇恨歧视等
     → 直接拒答，不进入 Agent，不消耗云端额度。
  2) offtopic（软引导）：明显非影视领域且非用户偏好/记忆 → 引导回影视推荐，同样不调 LLM。
- hybrid 模式：rule 判定为 offtopic 但不确信（长句/模糊）时，调用 chat_light 二次判定，
  默认关闭以省额度。

注意：本地护栏是轻量第一道闸门，正式生产环境建议叠加专业内容安全服务（如阿里云内容安全）
做兜底，本模块不替代专业审核。
"""
import re
from dataclasses import dataclass

from config.settings import settings


# ---- 敏感/违规主题（类别化锚点，非穷举；命中即拒答）----
# 说明：以下为通用违规表达锚点，用于本地轻量拦截；不针对具体实体、不输出敏感词本身。
_SENSITIVE_PATTERNS: dict[str, "re.Pattern[str]"] = {
    "political_security": re.compile(
        r"(分裂国家|国家分裂|独立建国|暴恐|恐怖主义|颠覆国家|制造爆炸物|炸弹袭击|恐怖袭击|"
        r"煽动叛乱|反政府武装|极端主义组织)", re.I),
    "porn": re.compile(
        r"(色情网站|黄色网站|成人网站|黄片|裸聊|性虐|援交|约炮平台|色情影片|色情资源)", re.I),
    "drug": re.compile(
        r"(吸毒|制毒|毒品交易|冰毒|海洛因|大麻种植|摇头丸|可卡因|笑气贩卖)", re.I),
    "gambling_fraud": re.compile(
        r"(网络赌博|赌博平台|博彩网站|赌球平台|杀猪盘|诈骗套路|网贷套路|刷单返利诈骗|套路贷)", re.I),
    "weapon": re.compile(
        r"(如何制作炸弹|制作炸药|制枪图纸|枪支制造|制造爆炸物方法|土制炸弹|如何造枪)", re.I),
    "self_harm": re.compile(
        r"(自杀方法|如何自杀|怎样自杀|自残教程|割腕教程)", re.I),
    "hate": re.compile(
        r"(种族灭绝|屠杀平民|仇恨言论|种族歧视言论|煽动民族仇恨)", re.I),
}

# ---- 影视领域信号（命中任一视为影视/偏好域，放行）----
_MOVIE_SIGNALS = (
    "电影", "影片", "剧", "电视剧", "综艺", "动漫", "动画", "纪录片", "短片", "番剧",
    "导演", "演员", "主演", "配角", "编剧", "制片", "评分", "口碑", "票房", "豆瓣", "IMDb",
    "类型", "科幻", "悬疑", "喜剧", "爱情", "动作", "恐怖", "战争", "剧情", "犯罪", "奇幻",
    "动画电影", "惊悚", "纪录", "推荐", "类似", "相似", "像", "排行", "榜单", "高分",
    "好看", "值得看", "想看", "剧荒", "影评", "解说", "电影推荐", "剧集", "影视", "大片",
    "经典", "必看", "冷门佳片", "国产", "美剧", "韩剧", "日剧", "英剧", "动画番",
)

# ---- 用户偏好 / 记忆信号（命中即便无影视词也放行，交给 preference 意图）----
_PREF_SIGNALS = ("我喜欢", "我偏好", "记一下", "记住", "我的偏好", "我的喜好", "偏好是", "喜欢看")


# 合规拒答语（不回显敏感词本身）
_SENSITIVE_MESSAGE = (
    "抱歉，你的问题涉及内容安全合规限制，我无法为你提供相关内容。"
    "如果你是想了解电影、剧集等影视作品，换一个影视相关的说法，我很乐意为你推荐。"
)
_OFFTOPIC_MESSAGE = (
    "我是影片推荐助手，主要帮你找电影、剧集、动画等影视作品。"
    "换个影视相关的话题（比如「推荐一部悬疑烧脑片」），我来为你推荐吧。"
)


@dataclass
class GuardrailResult:
    action: str      # "allow" | "block" | "guide"
    category: str    # "ok" | "sensitive:<cat>" | "offtopic"
    reason: str
    message: str     # 返回给用户的文本（allow 时为空）


def _rule_check(query: str) -> GuardrailResult:
    q = (query or "").strip()
    if not q:
        # 空 query 由 Pydantic 拦截，这里兜底放行
        return GuardrailResult("allow", "ok", "", "")

    # 1) 敏感/违规主题 -> 硬拒答
    for cat, pat in _SENSITIVE_PATTERNS.items():
        if pat.search(q):
            return GuardrailResult(
                "block", f"sensitive:{cat}", f"命中敏感/违规主题: {cat}",
                _SENSITIVE_MESSAGE)

    # 2) 越域判断（仅当开启）
    if settings.GUARDRAIL_BLOCK_OFFTOPIC:
        is_pref = any(k in q for k in _PREF_SIGNALS)
        is_movie = is_pref or any(s in q for s in _MOVIE_SIGNALS)
        if not is_movie:
            return GuardrailResult(
                "guide", "offtopic", "非影视领域且非偏好/记忆", _OFFTOPIC_MESSAGE)

    return GuardrailResult("allow", "ok", "", "")


def _llm_check(query: str) -> "str | None":
    """hybrid 模式：用 chat_light 二次判定 query 是否属于影视推荐意图。

    返回 "allow" / "guide" 之一，或 None（调用失败）。仅 hybrid 模式触发。
    """
    try:
        from langchain_core.messages import HumanMessage
        from llm.client import chat_light

        llm = chat_light()
        prompt = (
            "判断用户问题是否属于「电影/剧集/动画等影视作品的推荐或讨论」，"
            "或是否在表达个人影视偏好。只回答以下之一："
            "ALLOW（影视相关或偏好）、GUIDE（与影视无关的日常闲聊/其他领域问题）。"
            "不要解释。问题：" + query
        )
        resp = llm.invoke([HumanMessage(content=prompt)])
        txt = (resp.content or "").strip().upper()
        if "ALLOW" in txt:
            return "allow"
        if "GUIDE" in txt:
            return "guide"
    except Exception:
        return None
    return None


def check_guardrail(query: str) -> GuardrailResult:
    """对外入口：返回护栏判定结果。

    - GUARDRAIL_ENABLED=False：直接放行。
    - rule 模式：纯本地规则，零额度。
    - hybrid 模式：rule 判 guide 的边界 case 再用 chat_light 复核（保守：复核失败维持引导）。
    """
    if not settings.GUARDRAIL_ENABLED:
        return GuardrailResult("allow", "ok", "护栏关闭", "")

    res = _rule_check(query)

    if settings.GUARDRAIL_MODE == "hybrid" and res.action == "guide":
        verdict = _llm_check(query)
        if verdict == "allow":
            return GuardrailResult("allow", "ok", "hybrid 复核放行", "")
        # verdict 为 guide / block / None -> 维持 rule 的 guide 结论

    return res
