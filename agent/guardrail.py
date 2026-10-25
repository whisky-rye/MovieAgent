"""高规格内容安全护栏（P5 补充 + S3 健全）。

分层防御（defense-in-depth），全部本地规则、零额度、零外部依赖：

  第 0 层 · 输入净化   sanitize_query()     —— 去零宽/控制字符、压缩空白、截断超长，防畸形/填充攻击
  第 1 层 · 输入护栏   check_guardrail()   —— 入口闸门（不进 Agent、不耗额度）
        · sensitive  硬拒答：涉政分裂/暴恐、色情、毒品、赌博诈骗、危险武器、自残、仇恨歧视
        · injection  提示注入/越狱检测：命中即硬拒答（更高规格，默认拦截），不触发模型被带偏
        · offtopic   越域引导：非影视且非偏好/记忆 → 引导回推荐域（hybrid 模式可 LLM 复核）
  第 2 层 · 上下文净化 sanitize_context()  —— 喂给 LLM 的检索片段去除零宽/控制字符与强注入标记，
                                             降低 RAG 被检索内容注入的风险
  第 3 层 · 输出护栏   check_output()      —— 生成后闸门：扫描回答中的注入回显 / 策略泄漏 / 有害 how-to，
                                             命中以安全文案兜底替换，避免不安全内容外流

设计原则：
- 风险评分（risk_score / features）可观测，便于后续接专业内容安全服务（阿里云内容安全）做兜底。
- 本模块是轻量第一道闸门，不替代专业审核（见模块底部说明）。
- 所有阈值/开关来自 config.settings，调参无需改代码。
"""
import re
from dataclasses import dataclass, field

from config.settings import settings


# ============================ 基础配置 ============================

# 零宽/控制字符（用于混淆注入、绕过关键词检测）
_ZERO_WIDTH = re.compile(r"[\u200b\u200c\u200d\u2060\ufeff\u00ad\u034f\u061c]")
_CTRL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
# 零宽字符数量阈值：超过视为混淆/编码绕过攻击
_ZERO_WIDTH_BLOCK_THRESHOLD = 20


# ============================ 第 1 层：敏感/违规主题（硬拒答） ============================
# 类别化锚点（非穷举；命中即拒答）。不针对具体实体、不输出敏感词本身。
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


# ============================ 第 1 层：提示注入 / 越狱检测 ============================
# 触发词 + 角色/指令语义，避免误伤正常影视 query（如「扮演」单独出现不拦截，须配合「你是 AI」）。
_INJECTION_PATTERNS: list[tuple[str, "re.Pattern[str]"]] = [
    ("ignore_instruction_cn", re.compile(
        r"(忽略|无视|忘掉|忘记|抛弃|跳过).{0,8}(之前|前面|以上|上述|所有|系统|先前).{0,8}"
        r"(指令|提示词?|要求|规则|设定|限制|约束)", re.I)),
    ("ignore_instruction_en", re.compile(
        r"(ignore|disregard|forget|neglect|skip).{0,20}(previous|above|prior|system|earlier).{0,20}"
        r"(instruction|prompt|rule|directive|constraint)", re.I)),
    ("jailbreak", re.compile(
        r"(developer\s*mode|dev\s*mode|越狱|jailbreak|dan\s*mode|god\s*mode|root\s*mode|无限制模式)", re.I)),
    ("system_prompt", re.compile(r"(system\s*prompt|系统提示词|系统提示|system\s*instruction)", re.I)),
    ("new_instructions", re.compile(r"(新的?指令|new\s*instructions?)\s*(如下|below|是|：|:|below)", re.I)),
    ("role_override_now", re.compile(
        r"(现在开始|从现在起|从今以后|hereafter|from\s*now\s*on).{0,15}(你(必须|只|要|应该|不再|得))", re.I)),
    ("treat_as_instruction", re.compile(
        r"(把.{0,10}(上面|前文|以上|这些|前述).{0,10}(当作|作为|视为|当成).{0,10}(指令|命令|prompt|要求))", re.I)),
    ("ignore_role", re.compile(r"(忽略.{0,8}(你的|以上|前面).{0,8}(角色|设定|身份|限制|规则))", re.I)),
    ("deny_role", re.compile(
        r"(你(现在)?(不再是|不是).{0,12}(推荐|电影|影片|助手|机器人|ai)\b)", re.I)),
    ("impersonate_ai", re.compile(
        r"(role\s*play|扮演|假装|假设|设想|假装成).{0,6}(你是|你是一个|你是个|you\s*are)\s*"
        r"(一个?|an?)\s*(ai|人工智能|助手|assistant|模型|bot)", re.I)),
]


# ============================ 第 3 层：输出泄漏检测（收紧，避免误伤） ============================
# 仅匹配「系统/密钥」类强信号，正常的影片解说不会命中。
_LEAK_PATTERN = re.compile(
    r"(api[_-]?key\s*[:=]|sk-[a-z0-9]{20,}|system\s*prompt\s*[:=]|我的(内部)?(指令|提示词)\s*是\b|"
    r"你(的)?(初始)?(设定|系统)是\b)", re.I)


# ============================ 影视领域信号（越域判定） ============================
_MOVIE_SIGNALS = (
    "电影", "影片", "剧", "电视剧", "综艺", "动漫", "动画", "纪录片", "短片", "番剧",
    "导演", "演员", "主演", "配角", "编剧", "制片", "评分", "口碑", "票房", "豆瓣", "IMDb",
    "类型", "科幻", "悬疑", "喜剧", "爱情", "动作", "恐怖", "战争", "剧情", "犯罪", "奇幻",
    "动画电影", "惊悚", "纪录", "推荐", "类似", "相似", "像", "排行", "榜单", "高分",
    "好看", "值得看", "想看", "剧荒", "影评", "解说", "电影推荐", "剧集", "影视", "大片",
    "经典", "必看", "冷门佳片", "国产", "美剧", "韩剧", "日剧", "英剧", "动画番",
)
_PREF_SIGNALS = ("我喜欢", "我偏好", "记一下", "记住", "我的偏好", "我的喜好", "偏好是", "喜欢看")


# ============================ 文案 ============================
_SENSITIVE_MESSAGE = (
    "抱歉，你的问题涉及内容安全合规限制，我无法为你提供相关内容。"
    "如果你是想了解电影、剧集等影视作品，换一个影视相关的说法，我很乐意为你推荐。"
)
_OFFTOPIC_MESSAGE = (
    "我是影片推荐助手，主要帮你找电影、剧集、动画等影视作品。"
    "换个影视相关的话题（比如「推荐一部悬疑烧脑片」），我来为你推荐吧。"
)
_INJECTION_MESSAGE = (
    "抱歉，你的请求包含可能试图改变系统行为的指令，已被安全护栏拦截。"
    "我只能围绕电影、剧集等影视作品为你提供推荐，换个影视相关的说法试试吧。"
)
_OUTPUT_FALLBACK = (
    "抱歉，生成内容触发了安全校验，已为你重新组织推荐。换个影视相关的说法，我继续为你推荐。"
)


@dataclass
class GuardrailResult:
    action: str      # "allow" | "block" | "guide"
    category: str    # "ok" | "sensitive:<cat>" | "injection" | "offtopic" | "leak"
    reason: str
    message: str     # 返回给用户的文本（allow 时为空）
    risk_score: float = 0.0       # 风险评分（0=安全），便于观测/接专业审核
    features: list = field(default_factory=list)  # 命中的风险特征（可观测）
    sanitized_query: str = ""     # P2-15：净化/截断后的 query。全链路（embed/LLM/
    # 缓存键/日志）必须改用此值——此前 sanitize_query 的截断结果不随结果返回，
    # 原始 query 全额进嵌入与云端调用，GUARDRAIL_MAX_QUERY_LEN 形同虚设。


# ============================ 第 0 层：输入净化 ============================
def sanitize_query(query: str, max_len: int | None = None) -> str:
    """去零宽/控制字符、压缩空白、截断超长。纯字符级清理，不改变语义。"""
    if not query:
        return ""
    q = _ZERO_WIDTH.sub("", query)
    q = _CTRL_CHARS.sub("", q)
    q = re.sub(r"\s+", " ", q).strip()
    cap = max_len if max_len is not None else settings.GUARDRAIL_MAX_QUERY_LEN
    return q[:cap]


# ============================ 第 2 层：上下文净化 ============================
def sanitize_context(text: str, max_len: int = 2000) -> tuple[str, bool]:
    """净化喂给 LLM 的检索片段。

    去除零宽/控制字符、压缩空白、截断，并剔除强注入标记（developer mode / system prompt /
    jailbreak 等绝不出现在合法影视元数据中的子串）。返回 (clean_text, altered)。
    注意：主防御是 EXPLAIN_PROMPT 中的边界指令（把检索内容当数据而非指令）；此处做字符级
    与强标记级清理，作为 RAG 注入的纵深防御，且不会破坏正常影评/简介语义。
    """
    if not text:
        return text, False
    orig = text
    clean = _ZERO_WIDTH.sub("", text)
    clean = _CTRL_CHARS.sub("", clean)
    clean = _INJECTION_PATTERNS[2][1].sub("", clean)   # jailbreak 强标记
    clean = _INJECTION_PATTERNS[3][1].sub("", clean)   # system prompt 强标记
    clean = re.sub(r"\s+", " ", clean).strip()
    clean = clean[:max_len]
    return clean, (clean != orig.strip())


# ============================ 检测辅助 ============================
def _detect_injection(q: str) -> str | None:
    """返回命中的注入特征名；无命中返回 None。"""
    for name, pat in _INJECTION_PATTERNS:
        if pat.search(q):
            return name
    if len(_ZERO_WIDTH.findall(q)) > _ZERO_WIDTH_BLOCK_THRESHOLD:
        return "zero_width_obfuscation"
    return None


def _rule_check(query: str) -> GuardrailResult:
    q = (query or "").strip()
    if not q:
        return GuardrailResult("allow", "ok", "", "")

    # 1) 敏感/违规主题 -> 硬拒答
    for cat, pat in _SENSITIVE_PATTERNS.items():
        if pat.search(q):
            return GuardrailResult(
                "block", f"sensitive:{cat}", f"命中敏感/违规主题: {cat}",
                _SENSITIVE_MESSAGE, risk_score=1.0, features=[f"sensitive:{cat}"])

    # 2) 提示注入 / 越狱 -> 默认硬拒答（更高规格）
    inj = _detect_injection(q)
    if inj:
        feats = [f"injection:{inj}"]
        if settings.GUARDRAIL_BLOCK_INJECTION:
            return GuardrailResult(
                "block", "injection", f"命中提示注入/越狱: {inj}",
                _INJECTION_MESSAGE, risk_score=0.9, features=feats)
        # 不拦截时降级为引导，避免模型被带偏
        return GuardrailResult(
            "guide", "injection", f"疑似提示注入: {inj}",
            _OFFTOPIC_MESSAGE, risk_score=0.6, features=feats)

    # 3) 越域判断（仅当开启）
    if settings.GUARDRAIL_BLOCK_OFFTOPIC:
        is_pref = any(k in q for k in _PREF_SIGNALS)
        is_movie = is_pref or any(s in q for s in _MOVIE_SIGNALS)
        if not is_movie:
            return GuardrailResult(
                "guide", "offtopic", "非影视领域且非偏好/记忆", _OFFTOPIC_MESSAGE,
                risk_score=0.3, features=["offtopic"])

    return GuardrailResult("allow", "ok", "", "", risk_score=0.0)


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


# ============================ 对外入口 ============================
def check_guardrail(query: str) -> GuardrailResult:
    """第 1 层输入网关：返回护栏判定结果。

    - GUARDRAIL_ENABLED=False：直接放行（仍做字符级净化，sanitized_query 可用）。
    - rule 模式：纯本地规则，零额度。
    - hybrid 模式：rule 判 guide 的边界 case 再用 chat_light 复核（保守：复核失败维持引导）。

    P2-15：所有返回路径都带 sanitized_query（净化+截断到 GUARDRAIL_MAX_QUERY_LEN），
    调用方（api/main._run）放行后必须把它作为后续全链路的 query。
    """
    if not settings.GUARDRAIL_ENABLED:
        return GuardrailResult("allow", "ok", "护栏关闭", "", risk_score=0.0,
                               sanitized_query=sanitize_query(query or ""))

    raw = query or ""
    # 零宽/控制字符混淆（编码绕过攻击）：在净化前于原始输入上检测，避免被剔除后漏判
    if len(_ZERO_WIDTH.findall(raw)) > _ZERO_WIDTH_BLOCK_THRESHOLD:
        return GuardrailResult(
            "block", "injection", "零宽字符混淆/编码绕过",
            _INJECTION_MESSAGE, risk_score=0.9, features=["injection:zero_width_obfuscation"],
            sanitized_query=sanitize_query(raw))

    # 先净化输入（字符级），再做规则判定
    q = sanitize_query(raw)
    res = _rule_check(q)

    if settings.GUARDRAIL_MODE == "hybrid" and res.action == "guide" and res.category == "offtopic":
        verdict = _llm_check(q)
        if verdict == "allow":
            return GuardrailResult("allow", "ok", "hybrid 复核放行", "", risk_score=0.0,
                                   sanitized_query=q)

    # P2-15：把净化后的 query 挂到结果上（_rule_check 不感知该字段，在此统一补）
    res.sanitized_query = q
    return res


def check_output(text: str) -> GuardrailResult:
    """第 3 层输出网关：扫描生成文本的注入回显 / 策略泄漏 / 有害 how-to。

    保守策略：仅对强信号（注入回显、系统/密钥泄漏）拦截，避免误伤正常影片解说。
    命中返回 block + 安全兜底文案；否则放行。GUARDRAIL_OUTPUT_GATE=False 时直接放行。
    """
    if not text:
        return GuardrailResult("allow", "ok", "", "", risk_score=0.0)
    if not settings.GUARDRAIL_OUTPUT_GATE:
        return GuardrailResult("allow", "ok", "", "", risk_score=0.0)

    inj = _detect_injection(text)
    if inj:
        return GuardrailResult(
            "block", "injection_output", f"输出含注入回显: {inj}",
            _OUTPUT_FALLBACK, risk_score=0.8, features=[f"injection:{inj}"])

    if _LEAK_PATTERN.search(text):
        return GuardrailResult(
            "block", "leak", "输出疑似泄漏系统/密钥", _OUTPUT_FALLBACK,
            risk_score=0.8, features=["leak"])

    return GuardrailResult("allow", "ok", "", "", risk_score=0.0)
