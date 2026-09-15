<script setup lang="ts">
import { ref, nextTick, onBeforeUnmount, watch } from 'vue'
import MessageItem from './MessageItem.vue'
import { streamRecommend } from '../api/stream'
import {
  currentSessionId, currentMessages, sessionsLoading, newSession, pushMessage, updateLastAssistant, selectSession,
} from '../store/sessions'
import type { ChatMessage, ChatStep, DonePayload } from '../types'

const input = ref('')
const streaming = ref(false)
const scrollEl = ref<HTMLElement | null>(null)
const textareaEl = ref<HTMLTextAreaElement | null>(null)
const nearBottom = ref(true)
let controller: AbortController | null = null

const prompts = [
  '推荐几部赛博朋克风格的科幻片',
  '适合周末看的轻松喜剧',
  '诺兰导演的烧脑电影',
  '近十年高分悬疑片',
]

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

async function send(customQuery?: string) {
  const q = (customQuery ?? input.value).trim()
  if (!q || streaming.value) return

  const sid = await ensureSession()

  // 立即插入用户消息 + 空的助手占位
  pushMessage({ role: 'user', content: q })
  pushMessage({ role: 'assistant', content: '', steps: [], streaming: true })
  input.value = ''
  if (textareaEl.value) textareaEl.value.style.height = 'auto'
  nearBottom.value = true
  await scrollToBottom(true)

  streaming.value = true
  controller = new AbortController()

  const steps: ChatStep[] = []
  try {
    await streamRecommend(
      q,
      sid,
      {
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
        },
        onError: (msg) => {
          updateLastAssistant((m) => {
            m.error = msg
            m.streaming = false
          })
          scrollToBottom(true)
        },
      },
      controller.signal,
    )
  } finally {
    streaming.value = false
    controller = null
  }
}

function stop() {
  controller?.abort()
  updateLastAssistant((m) => {
    m.streaming = false
  })
  streaming.value = false
}

function retryLast() {
  const msgs = currentMessages.value
  for (let i = msgs.length - 1; i >= 0; i--) {
    if (msgs[i].role === 'user') {
      send(msgs[i].content)
      return
    }
  }
}

function onKey(e: KeyboardEvent) {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault()
    send()
  }
}

onBeforeUnmount(() => controller?.abort())

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
          <button v-for="p in prompts" :key="p" class="chip" @click="send(p)">{{ p }}</button>
        </div>
      </div>
    </div>

    <div v-show="!sessionsLoading && currentMessages.length > 0" ref="scrollEl" class="messages" @scroll="onScroll">
      <MessageItem v-for="(m, i) in currentMessages" :key="i" :message="m" />
      <div v-if="streaming" class="typing">正在生成中…</div>
    </div>

    <div class="composer">
      <textarea
        ref="textareaEl"
        v-model="input"
        rows="1"
        placeholder="描述你想看的电影，Enter 发送 / Shift+Enter 换行"
        @keydown="onKey"
        @input="onInput"
      ></textarea>
      <button v-if="!streaming" class="send" :disabled="!input.trim()" @click="send()">发送</button>
      <button v-else class="stop" @click="stop">停止</button>
    </div>

    <!-- 最后一条助手消息出错时，悬浮重试条 -->
    <button
      v-if="currentMessages.length && currentMessages[currentMessages.length - 1].role === 'assistant' && currentMessages[currentMessages.length - 1].error"
      class="retry-bar"
      @click="retryLast"
    >
      最后一条回复失败，点击重试
    </button>
  </div>
</template>

<style scoped>
.chat { flex: 1; min-height: 0; display: grid; grid-template-rows: 1fr auto; position: relative; }
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
  background: #fff; color: var(--fg); border: 1px solid var(--line);
  padding: 8px 12px; font-size: 13px; border-radius: 999px;
}
.chip:hover { border-color: var(--accent); color: var(--accent); }

.messages { grid-row: 1; min-height: 0; overflow-y: auto; padding: 20px 24px; }
.typing { font-size: 13px; color: var(--muted); margin-top: 8px; }

.composer {
  grid-row: 2; display: flex; gap: 10px; padding: 14px 20px; border-top: 1px solid var(--line); background: #fff;
  align-items: flex-end;
}
.composer textarea {
  flex: 1; resize: none; border: 1px solid var(--line); border-radius: 10px;
  padding: 10px 12px; font-size: 14px; font-family: inherit; line-height: 1.5; max-height: 160px;
  min-height: 40px; overflow-y: auto;
}
.composer textarea:focus { border-color: var(--accent); outline: none; }
.send { background: var(--accent); color: #fff; padding: 0 18px; height: 40px; }
.send:disabled { opacity: 0.5; cursor: default; }
.stop { background: var(--danger); color: #fff; padding: 0 18px; height: 40px; }

.retry-bar {
  position: absolute; bottom: 76px; left: 50%; transform: translateX(-50%);
  background: #fff; color: var(--danger); border: 1px solid var(--danger);
  padding: 8px 14px; border-radius: 999px; font-size: 13px; box-shadow: 0 2px 8px rgba(0,0,0,0.08);
}
</style>
