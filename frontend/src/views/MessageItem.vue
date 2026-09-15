<script setup lang="ts">
import { ref } from 'vue'
import { renderMarkdown } from '../utils/md'
import { doubanUrl, imdbUrl } from '../utils/extlinks'
import type { ChatItem, ChatMessage, ChatStep } from '../types'

defineProps<{ message: ChatMessage }>()

function md(html: string): string {
  return renderMarkdown(html)
}

const showSteps = ref(false)
const showThinking = ref(false)

function fmtCount(n: number | null | undefined): string {
  if (n == null) return '-'
  if (n >= 10000) return (n / 10000).toFixed(1) + '万'
  return String(n)
}

function kindLabel(kind: string | undefined): string {
  switch (kind) {
    case 'recall':
      return '召回'
    case 'rerank':
      return '精排'
    case 'generate':
      return '生成'
    case 'guardrail':
      return '护栏'
    default:
      return kind || '步骤'
  }
}

// 外部详情页链接（仅当对应 id 存在时展示），逻辑见 utils/extlinks.ts
function doubanUrlFor(it: ChatItem): string | null {
  return doubanUrl(it.douban_id)
}
function imdbUrlFor(it: ChatItem): string | null {
  return imdbUrl(it.imdb_id)
}
</script>

<template>
  <div class="msg" :class="message.role">
    <div class="avatar">{{ message.role === 'user' ? '你' : 'AI' }}</div>
    <div class="bubble">
      <!-- 用户消息 -->
      <p v-if="message.role === 'user'" class="text">{{ message.content }}</p>

      <!-- 助手消息 -->
      <template v-else>
        <p v-if="message.error" class="err">{{ message.error }}</p>

        <!-- 步骤时间线 -->
        <div v-if="message.steps && message.steps.length" class="block">
          <button class="toggle" @click="showSteps = !showSteps">
            {{ showSteps ? '收起' : '展开' }} 工具链（{{ message.steps.length }} 步）
          </button>
          <ol v-if="showSteps" class="steps">
            <li v-for="s in message.steps" :key="s.order" class="step">
              <span class="badge">{{ kindLabel(s.kind) }}</span>
              <span class="step-summary">{{ s.summary }}</span>
              <span v-if="s.elapsed_ms != null" class="step-ms">{{ s.elapsed_ms }}ms</span>
              <div v-if="s.detail" class="step-detail">{{ s.detail }}</div>
            </li>
          </ol>
        </div>

        <!-- 深度思考（thinking 可能为对象，已在 store 层归一化为字符串；此处再加一道防卫） -->
        <div v-if="typeof message.thinking === 'string' && message.thinking.trim()" class="block">
          <button class="toggle" @click="showThinking = !showThinking">
            {{ showThinking ? '收起' : '展开' }} 深度思考
          </button>
          <pre v-if="showThinking" class="thinking">{{ message.thinking }}</pre>
        </div>

        <!-- 答案正文 -->
        <div class="text" :class="{ streaming: message.streaming }" v-html="md(message.content)"></div>
        <span v-if="message.streaming" class="cursor">▍</span>

        <!-- 候选影片 -->
        <div v-if="message.items && message.items.length" class="items">
          <div v-for="it in message.items" :key="it.id" class="item-card">
            <div class="item-head">
              <span class="item-title">{{ it.title }}</span>
              <span v-if="it.year" class="item-year">({{ it.year }})</span>
              <span class="src" :class="it.source">{{ it.source_label }}</span>
            </div>
            <div class="item-meta">
              <span class="rating">★ {{ it.rating ? it.rating.toFixed(1) : '-' }}</span>
              <span class="muted">{{ fmtCount(it.rating_count) }} 人</span>
              <span v-if="it.country" class="muted">{{ it.country }}</span>
              <span v-if="it.genres && it.genres.length" class="muted">{{ it.genres.join(' / ') }}</span>
            </div>
            <div v-if="it.recall_source" class="recall">召回：{{ it.recall_source }}</div>
            <div class="links">
              <a v-if="doubanUrlFor(it)" class="ext douban" :href="doubanUrlFor(it)" target="_blank" rel="noopener noreferrer">豆瓣详情 ↗</a>
              <a v-if="imdbUrlFor(it)" class="ext imdb" :href="imdbUrlFor(it)" target="_blank" rel="noopener noreferrer">IMDb 详情 ↗</a>
            </div>
          </div>
        </div>
      </template>
    </div>
  </div>
</template>

<style scoped>
.msg { display: flex; gap: 10px; margin-bottom: 18px; }
.avatar {
  flex: none; width: 30px; height: 30px; border-radius: 50%;
  display: flex; align-items: center; justify-content: center;
  font-size: 12px; color: #fff; background: var(--muted);
}
.msg.user .avatar { background: var(--accent); }
.msg.assistant .avatar { background: #0f766e; }
.bubble { flex: 1; min-width: 0; }
.text { line-height: 1.7; font-size: 14px; }
.text :deep(p) { margin: 0 0 10px; }
.text :deep(p:last-child) { margin-bottom: 0; }
.text :deep(ul), .text :deep(ol) { margin: 8px 0; padding-left: 20px; }
.text :deep(li) { margin: 4px 0; }
.text :deep(code) {
  background: #f3f4f6; padding: 2px 5px; border-radius: 4px; font-size: 13px; color: #c7254e;
}
.text :deep(pre) {
  background: #f8f9fa; padding: 10px 12px; border-radius: 8px; overflow-x: auto; margin: 8px 0;
}
.text :deep(pre code) { background: transparent; padding: 0; color: inherit; }
.streaming::after { content: ''; }
.cursor { color: var(--accent); animation: blink 1s steps(2) infinite; }
@keyframes blink { 0%, 50% { opacity: 1; } 51%, 100% { opacity: 0; } }
.err { color: var(--danger); margin: 0 0 8px; font-size: 14px; }

.block { margin: 8px 0; }
.toggle {
  background: none; color: var(--muted); font-size: 12px; padding: 2px 0;
}
.steps { list-style: none; margin: 6px 0 0; padding: 8px 10px; border-left: 2px solid var(--line); background: #fafafa; }
.step { font-size: 12px; color: var(--muted); margin-bottom: 6px; }
.badge {
  display: inline-block; background: #eef2ff; color: var(--accent);
  border-radius: 4px; padding: 0 6px; font-size: 11px; margin-right: 6px;
}
.step-summary { color: var(--fg); }
.step-ms { margin-left: 6px; color: #9ca3af; }
.step-detail { margin-top: 2px; color: #6b7280; white-space: pre-wrap; }
.thinking {
  margin: 6px 0 0; padding: 10px 12px; background: #faf8f0;
  border-radius: 8px; font-size: 12px; color: #6b5b3a; white-space: pre-wrap;
  max-height: 240px; overflow: auto; line-height: 1.6;
}

.items { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 10px; margin-top: 10px; }
.item-card { border: 1px solid var(--line); border-radius: 10px; padding: 10px 12px; background: #fff; }
.item-head { display: flex; align-items: baseline; gap: 4px; flex-wrap: wrap; }
.item-title { font-weight: 600; font-size: 14px; }
.item-year { color: var(--muted); font-size: 12px; }
.src { margin-left: auto; font-size: 11px; padding: 0 6px; border-radius: 4px; }
.src.douban { background: #fff1e6; color: #c0501a; }
.src.imdb { background: #eaf2ff; color: #1d4ed8; }
.item-meta { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 6px; font-size: 12px; color: var(--muted); }
.rating { color: #d97706; font-weight: 600; }
.muted { color: var(--muted); }
.recall { margin-top: 4px; font-size: 11px; color: #9ca3af; }
.links { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }
.ext {
  display: inline-flex; align-items: center; gap: 2px;
  font-size: 12px; text-decoration: none; line-height: 1;
  padding: 4px 8px; border-radius: 6px; border: 1px solid transparent;
  transition: background 0.15s ease, border-color 0.15s ease;
}
.ext:hover { filter: brightness(0.97); }
.ext.douban { background: #fff1e6; color: #c0501a; border-color: #f6d3b8; }
.ext.imdb { background: #eaf2ff; color: #1d4ed8; border-color: #c7dbff; }
</style>
