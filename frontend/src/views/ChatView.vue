<script setup lang="ts">
import { ref, nextTick, onBeforeUnmount, watch, computed } from 'vue'
import MessageItem from './MessageItem.vue'
import { streamRecommend } from '../api/stream'
import { fetchPoster, API_BASE } from '../api/client'
import suggestionsData from '../data/suggestions.json'
import {
  currentSessionId, currentMessages, sessionsLoading, newSession, pushMessage,
  updateLastAssistant, selectSession, truncateAfter,
} from '../store/sessions'
import type { ChatMessage, ChatStep, DonePayload, StreamError } from '../types'

const input = ref('')
const streaming = ref(false)
const connecting = ref(false)
const scrollEl = ref<HTMLElement | null>(null)
const textareaEl = ref<HTMLTextAreaElement | null>(null)
const nearBottom = ref(true)
let controller: AbortController | null = null

// M6：结构化错误 + 限流倒计时 + 断线重连
const lastError = ref<StreamError | null>(null)
const cooldown = ref(0)
let cooldownTimer: number | null = null
let autoRetryUsed = false
let autoRetryTimer: number | null = null

// 首页空状态推荐语句：300 条语料（scripts/gen_prompt_suggestions.py 生成），每次随机取 4 条展示，
// 点"换一批"按钮从不重复于当前批次的剩余语句里再抽 4 条。
const ALL_PROMPTS: string[] = suggestionsData.suggestions
const BATCH_SIZE = 4

function pickBatch(exclude: Set<string>): string[] {
  const pool = ALL_PROMPTS.filter((p) => !exclude.has(p))
  const source = pool.length >= BATCH_SIZE ? pool : ALL_PROMPTS // 语料足够时排除当前批次，避免连点重复
  const shuffled = [...source]
  for (let i = shuffled.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1))
    ;[shuffled[i], shuffled[j]] = [shuffled[j], shuffled[i]]
  }
  return shuffled.slice(0, BATCH_SIZE)
}

const prompts = ref<string[]>(pickBatch(new Set()))

function refreshPrompts() {
  prompts.value = pickBatch(new Set(prompts.value))
}

const busy = computed(() => connecting.value || streaming.value)

// 底部告警条文案：限流倒计时内给出「Ns 后可重试」提示
const alertText = computed(() => {
  if (!lastError.value) return ''
  if (lastError.value.kind === 'rate_limited' && cooldown.value > 0) {
    return `${lastError.value.message}（${cooldown.value}s 后可重试）`
  }
  return lastError.value.message
})
const alertRetryLabel = computed(() =>
  lastError.value && (lastError.value.kind === 'network' || lastError.value.kind === 'stream')
    ? '重新连接'
    : '重试',
)
// 护栏是策略性拦截，重试相同内容无意义，仅展示引导文案、不提供重试按钮
const showRetry = computed(() => !!lastError.value && lastError.value.kind !== 'guardrail')

function autoResize() {
  const el = textareaEl.value
  if (!el) return
  el.style.height = 'auto'
  const h = Math.min(el.scrollHeight, 160)
  el.style.height = `${h}px`
}

function onInput() {
  autoResize()
}

async function scrollToBottom(force = false) {
  await nextTick()
  const el = scrollEl.value
  if (!el) return
  if (force || nearBottom.value) {
    el.scrollTop = el.scrollHeight
  }
}

function onScroll() {
  const el = scrollEl.value
  if (!el) return
  const threshold = 60
  nearBottom.value = el.scrollHeight - el.scrollTop - el.clientHeight < threshold
}

async function ensureSession(): Promise<string> {
  let sid = currentSessionId.value
  if (!sid) sid = await newSession()
  return sid
}

function clearCooldown() {
  if (cooldownTimer !== null) {
    clearInterval(cooldownTimer)
    cooldownTimer = null
  }
  cooldown.value = 0
}

function startCooldown(sec?: number) {
  clearCooldown()
  if (!sec || sec <= 0) return
  cooldown.value = Math.ceil(sec)
  cooldownTimer = window.setInterval(() => {
    cooldown.value -= 1
    if (cooldown.value <= 0) clearCooldown()
  }, 1000)
}

async function startStream(q: string, sid: string, pushUser: boolean) {
  if (busy.value) return
  const query = q.trim()
  if (!query) return

  if (pushUser) pushMessage({ role: 'user', content: query })
  pushMessage({ role: 'assistant', content: '', steps: [], streaming: true })

  input.value = ''
  if (textareaEl.value) textareaEl.value.style.height = 'auto'
  nearBottom.value = true
  await scrollToBottom(true)

  lastError.value = null
  clearCooldown()
  streaming.value = true
  connecting.value = true
  controller = new AbortController()

  const steps: ChatStep[] = []
  try {
    await streamRecommend(
      query,
      sid,
      {
        onConnecting: () => {
          connecting.value = true
        },
        onFirstToken: () => {
          connecting.value = false
        },
        onStep: (s) => {
          steps.push(s)
          updateLastAssistant((m) => {
            m.steps = [...steps]
          })
        },
        onThinking: (delta) => {
          updateLastAssistant((m) => {
            m.thinking = (m.thinking ?? '') + delta
          })
        },
        onToken: (delta) => {
          updateLastAssistant((m) => {
            m.content += delta
          })
          scrollToBottom()
        },
        onDone: (p: DonePayload) => {
          updateLastAssistant((m) => {
            m.content = p.answer
            m.items = p.items
            m.steps = p.steps
            m.thinking = typeof p.thinking === 'string' ? p.thinking : m.thinking
            m.intent = p.intent
            m.sources = p.sources
            m.streaming = false
          })
          scrollToBottom(true)
          // G-Poster：对话完成后异步生成 Top3 海报，轮询回填到本条助手消息
          if (p.items && p.items.length && currentSessionId.value) {
            pollPoster(currentSessionId.value)
          }
        },
        onError: (err: StreamError) => {
          updateLastAssistant((m) => {
            m.error = err.message
            m.errorKind = err.kind
            m.retryAfter = err.retry_after
            m.streaming = false
          })
          lastError.value = err
          if (err.kind === 'rate_limited') {
            startCooldown(err.retry_after)
          } else if (err.kind === 'network' || err.kind === 'stream') {
            // 断线/流中断：1.5s 后自动重连一次（仅一次，避免风暴）
            if (!autoRetryUsed) {
              autoRetryUsed = true
              autoRetryTimer = window.setTimeout(() => {
                if (lastError.value && (lastError.value.kind === 'network' || lastError.value.kind === 'stream')) {
                  doRetry()
                }
              }, 1500)
            }
          }
          scrollToBottom(true)
        },
      },
      controller.signal,
    )
  } finally {
    streaming.value = false
    connecting.value = false
    controller = null
  }
}

async function send(customQuery?: string) {
  if (busy.value) return
  const q = (customQuery ?? input.value).trim()
  if (!q) return
  let sid: string
  try {
    sid = await ensureSession()
  } catch (e) {
    // 创建会话失败（多为断网）：同样走优雅告警，而非静默失败
    surfaceError(e, '创建会话失败')
    return
  }
  autoRetryUsed = false
  await startStream(q, sid, true)
}

// 把任意异常转成顶层告警（M6：断网/创建会话失败可见且优雅）
function surfaceError(e: unknown, ctx: string) {
  streaming.value = false
  connecting.value = false
  const msg = e instanceof Error ? e.message : String(e)
  const err: StreamError = { kind: 'network', message: `${ctx}：${msg}` }
  lastError.value = err
  updateLastAssistant((m) => {
    m.error = err.message
    m.errorKind = 'network'
    m.streaming = false
  })
  startCooldown(0)
  scrollToBottom(true)
}

// 重试：去掉上一次用户消息之后的失败占位，再重发（不重复推送用户气泡）
function doRetry() {
  if (autoRetryTimer !== null) {
    clearTimeout(autoRetryTimer)
    autoRetryTimer = null
  }
  const msgs = currentMessages.value
  let ui = -1
  for (let i = msgs.length - 1; i >= 0; i--) {
    if (msgs[i].role === 'user') {
      ui = i
      break
    }
  }
  if (ui < 0) return
  truncateAfter(ui)
  const sid = currentSessionId.value ?? ''
  startStream(msgs[ui].content, sid, false)
}

function retryLast() {
  autoRetryUsed = false
  doRetry()
}

// G-Poster：轮询会话海报状态，就绪后回填到「当前最后一条助手消息」。
// 用自增 token 防止重叠对话时把旧轮询错误地挂到新消息上；新对话开始即作废旧轮询。
let posterPollToken = 0
let posterTimer: ReturnType<typeof setTimeout> | null = null

// 后端返回的海报 url/download_url 是 host 相对路径（如 /poster/download/{sid}）。
// 开发环境前端跑在 :5173、后端在 :8000，若直接作为 <img src>/<a href> 会被解析到 Vite(5173)，
// 拿到的是 SPA 的 HTML 而非图片——既导致预览裂图、又导致"下载到格式异常的文件"。
// 因此一律拼上 API_BASE，确保指向真实后端。
function absUrl(p?: string): string | undefined {
  if (!p) return p
  if (/^https?:\/\//i.test(p)) return p
  return API_BASE + p
}

function applyPoster(m: ChatMessage, st: { url?: string; download_url?: string; model?: string; model_family?: 'image' | 'video'; media_type?: string; placeholder?: boolean }) {
  m.poster = {
    url: absUrl(st.url) ?? '',
    download_url: absUrl(st.download_url) ?? '',
    model: st.model ?? '',
    model_family: st.model_family ?? 'image',
    media_type: st.media_type,
    placeholder: st.placeholder,
  }
}

function pollPoster(sid: string, maxAttempts = 90) {
  const token = ++posterPollToken
  let attempts = 0
  const tick = async () => {
    if (token !== posterPollToken) return // 已被新对话取代
    try {
      const st = await fetchPoster(sid)
      if (st.status === 'ready' && st.url) {
        updateLastAssistant((m) => applyPoster(m, st))
        return
      }
      // 仅 pending（生成进行中）继续轮询；none（从未生成）/ error 均为终态，立即停止，
      // 避免对不存在的海报空转 3 分钟（与 store/sessions.ts restorePoster 同规则）。
      if (st.status !== 'pending') return
    } catch {
      // 网络抖动：继续轮询
    }
    attempts += 1
    if (attempts < maxAttempts) {
      posterTimer = setTimeout(tick, 2000)
    }
  }
  posterTimer = setTimeout(tick, 1500)
}

function stop() {
  controller?.abort()
  updateLastAssistant((m) => {
    m.streaming = false
  })
  streaming.value = false
  connecting.value = false
}

function onKey(e: KeyboardEvent) {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault()
    send()
  }
}

onBeforeUnmount(() => {
  controller?.abort()
  clearCooldown()
  if (autoRetryTimer !== null) clearTimeout(autoRetryTimer)
})

// 切换会话后自动滚到底
watch(currentSessionId, () => {
  nearBottom.value = true
  scrollToBottom(true)
})
</script>

<template>
  <div class="chat">
    <div v-show="sessionsLoading" class="loading-state">
      <span class="spinner"></span>
      <span>正在加载会话…</span>
    </div>

    <div v-show="!sessionsLoading && currentMessages.length === 0" class="empty">
      <div class="empty-inner">
        <h2>想看点什么？</h2>
        <p>描述你的观影心情、类型偏好或导演演员，AI 会推荐合适的影片。</p>
        <div class="chips">
          <button v-for="p in prompts" :key="p" class="chip" :disabled="busy" @click="send(p)">{{ p }}</button>
          <button
            class="chip refresh"
            :disabled="busy"
            title="换一批推荐"
            aria-label="换一批推荐"
            @click="refreshPrompts"
          >
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"
                 stroke-linejoin="round" aria-hidden="true">
              <polyline points="23 4 23 10 17 10" />
              <path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10" />
            </svg>
          </button>
        </div>
      </div>
    </div>

    <div v-show="!sessionsLoading && currentMessages.length > 0" ref="scrollEl" class="messages" @scroll="onScroll">
      <MessageItem v-for="(m, i) in currentMessages" :key="i" :message="m" />
      <div v-if="connecting && !streaming" class="typing">正在连接…</div>
      <div v-else-if="streaming" class="typing">正在生成中…</div>
    </div>

    <!-- M6：断线 / 限流 / 护栏 优雅告警条 -->
    <div v-if="lastError" class="alert" :class="lastError.kind">
      <span class="alert-dot"></span>
      <span class="alert-msg">{{ alertText }}</span>
      <button v-if="showRetry" class="alert-retry" :disabled="cooldown > 0" @click="doRetry">
        {{ cooldown > 0 ? `${cooldown}s` : alertRetryLabel }}
      </button>
    </div>

    <div class="composer">
      <textarea
        ref="textareaEl"
        v-model="input"
        rows="1"
        :placeholder="busy ? '正在生成，请稍候…' : '描述你想看的电影，Enter 发送 / Shift+Enter 换行'"
        :disabled="busy"
        @keydown="onKey"
        @input="onInput"
      ></textarea>
      <button v-if="!streaming" class="send" :disabled="!input.trim() || connecting" @click="send()">发送</button>
      <button v-else class="stop" @click="stop">停止</button>
    </div>
  </div>
</template>

<style scoped>
.chat { flex: 1; min-height: 0; display: grid; grid-template-rows: 1fr auto auto; position: relative; }
.loading-state {
  grid-row: 1; min-height: 0; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 12px;
  color: var(--muted); overflow-y: auto;
}
.spinner {
  width: 24px; height: 24px; border: 3px solid var(--line); border-top-color: var(--accent);
  border-radius: 50%; animation: spin 1s linear infinite;
}
@keyframes spin { to { transform: rotate(360deg); } }

.empty { grid-row: 1; min-height: 0; display: flex; align-items: center; justify-content: center; color: var(--muted); overflow-y: auto; }
.empty-inner { max-width: 520px; text-align: center; padding: 24px; }
.empty-inner h2 { margin: 0 0 10px; color: var(--fg); }
.empty-inner p { font-size: 14px; line-height: 1.7; margin: 0 0 18px; }
.chips { display: flex; flex-wrap: wrap; justify-content: center; gap: 8px; }
.chip {
  background: var(--surface); color: var(--fg); border: 1px solid var(--line);
  padding: 8px 12px; font-size: 13px; border-radius: 999px;
}
.chip:hover:not(:disabled) { border-color: var(--accent); color: var(--accent); }
.chip:disabled { opacity: 0.5; cursor: default; }
/* "换一批"按钮：与语句气泡同高的圆形图标钮 */
.chip.refresh {
  width: 34px; height: 34px; padding: 0; flex: none;
  display: inline-flex; align-items: center; justify-content: center;
  border-radius: 50%; color: var(--muted);
}
.chip.refresh svg { width: 15px; height: 15px; display: block; }
.chip.refresh:hover:not(:disabled) { transform: rotate(60deg); }
.chip.refresh { transition: transform 0.3s ease, border-color 0.2s ease, color 0.2s ease; }

.messages { grid-row: 1; min-height: 0; overflow-y: auto; padding: 20px 24px; }
.typing { font-size: 13px; color: var(--muted); margin-top: 8px; }

/* M6 告警条 */
.alert {
  grid-row: 2;
  display: flex; align-items: center; gap: 10px;
  margin: 0 16px; padding: 8px 12px; border-radius: 10px;
  font-size: 13px; background: var(--alert-warn-bg); border: 1px solid var(--alert-warn-border); color: var(--alert-warn-fg);
}
.alert.rate_limited { background: var(--alert-warn-bg); border-color: var(--alert-warn-border); color: var(--alert-warn-fg); }
.alert.guardrail { background: var(--alert-info-bg); border-color: var(--alert-info-border); color: var(--alert-info-fg); }
.alert.network, .alert.stream { background: var(--alert-err-bg); border-color: var(--alert-err-border); color: var(--alert-err-fg); }
.alert.unknown, .alert.http, .alert.server { background: var(--alert-err-bg); border-color: var(--alert-err-border); color: var(--alert-err-fg); }
.alert-dot { width: 8px; height: 8px; border-radius: 50%; background: currentColor; flex: none; }
.alert-msg { flex: 1; min-width: 0; line-height: 1.5; }
.alert-retry {
  flex: none; background: currentColor; color: var(--accent-fg); padding: 5px 12px; font-size: 12px;
  border-radius: 8px; min-width: 56px;
}
.alert-retry:disabled { opacity: 0.55; cursor: default; }

.composer {
  grid-row: 3; display: flex; gap: 10px; padding: 14px 20px; border-top: 1px solid var(--line); background: var(--surface);
  align-items: flex-end;
}
.composer textarea {
  flex: 1; resize: none; border: 1px solid var(--line); border-radius: 10px;
  padding: 10px 12px; font-size: 14px; font-family: inherit; line-height: 1.5; max-height: 160px;
  min-height: 40px; overflow-y: auto;
}
.composer textarea:focus { border-color: var(--accent); outline: none; }
.composer textarea:disabled { background: var(--surface-disabled); }
.send { background: var(--accent); color: var(--accent-fg); padding: 0 18px; height: 40px; }
.send:disabled { opacity: 0.5; cursor: default; }
.stop { background: var(--danger); color: var(--accent-fg); padding: 0 18px; height: 40px; }

/* 移动端：输入框与告警更紧凑 */
@media (max-width: 720px) {
  .messages { padding: 16px 14px; }
  .composer { padding: 10px 12px; }
  .send, .stop { padding: 0 14px; }
  .alert { margin: 0 10px; }
}
</style>
