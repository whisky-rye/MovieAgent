<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { authUser, logout } from '../store/auth'
import { currentView, setView, sidebarOpen, toggleSidebar, closeSidebar, theme, toggleTheme } from '../store/ui'
import { loadSessions, selectSession, sessionList } from '../store/sessions'
import { loadMyFeedback, resetFeedback } from '../store/feedback'
import { readUrlState } from '../urlState'
import { exportSessionsZip } from '../api/export'
import Sidebar from './Sidebar.vue'
import ChatView from './ChatView.vue'
import MemoryView from './MemoryView.vue'

// —— 会话导出（仿 DeepSeek）：点击 -> 后端打包 zip -> 自动触发浏览器下载 ——
const exporting = ref(false)
const exportTip = ref('')
let tipTimer: ReturnType<typeof setTimeout> | null = null

async function onExport(): Promise<void> {
  if (exporting.value) return
  exporting.value = true
  try {
    const r = await exportSessionsZip()
    const kb = r.sizeBytes >= 1024 ? `${(r.sizeBytes / 1024).toFixed(1)} KB` : `${r.sizeBytes} B`
    const n = r.sessionCount === null ? '' : `（${r.sessionCount} 个会话，${kb}）`
    showTip(`已导出 ${r.filename} ${n}`)
  } catch (e) {
    showTip(e instanceof Error ? e.message : '导出失败，请重试')
  } finally {
    exporting.value = false
  }
}

function showTip(msg: string): void {
  exportTip.value = msg
  if (tipTimer) clearTimeout(tipTimer)
  tipTimer = setTimeout(() => (exportTip.value = ''), 4000)
}

// 会话列表固定链接：进入主页时读取 URL hash，恢复「视图 + 选中会话」，
// 使 F5 刷新后停留在会话列表当前状态而非跳回首页。
onMounted(async () => {
  // 反馈状态（O11）：与列表加载并行拉取，不阻塞首屏；恢复「已喜欢/已不喜欢」标记。
  // 显式带上当前用户身份，确保刷新/重进会话后恢复的是「同一用户」的历史（不落到 default）。
  loadMyFeedback(authUser.value?.user_id)
  const { view, sid } = readUrlState()
  setView(view)
  if (sid) {
    await loadSessions()
    // 仅当该会话确实存在于当前用户列表时才恢复选中，避免选中已删除的会话
    if (sessionList.value.some((s) => s.session_id === sid)) {
      await selectSession(sid)
    }
  }
})

// 退出登录：清掉内存中的反馈标记，避免下一个账号看到上一个账号的操作痕迹
function onLogout(): void {
  resetFeedback()
  logout()
}
</script>

<template>
  <div class="home" :class="{ 'drawer-open': sidebarOpen }">
    <header class="topbar">
      <button class="hamburger" aria-label="会话列表" @click="toggleSidebar">
        <span></span><span></span><span></span>
      </button>
      <span class="title">影片推荐智能体</span>
      <nav class="nav">
        <button :class="{ active: currentView === 'chat' }" @click="setView('chat')">对话</button>
        <button :class="{ active: currentView === 'memory' }" @click="setView('memory')">记忆</button>
      </nav>
      <div class="user">
        <transition name="tipfade">
          <span v-if="exportTip" class="export-tip" role="status">{{ exportTip }}</span>
        </transition>
        <button
          class="theme-toggle"
          :disabled="exporting"
          title="导出所有历史对话（zip）"
          aria-label="导出所有历史对话"
          @click="onExport"
        >
          <svg :class="['icon', { spinning: exporting }]" viewBox="0 0 24 24" fill="none" stroke="currentColor"
               stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
            <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
            <polyline points="7 10 12 15 17 10" />
            <line x1="12" y1="15" x2="12" y2="3" />
          </svg>
        </button>
        <button
          class="theme-toggle"
          :title="theme === 'light' ? '切换到暗色模式' : '切换到明亮模式'"
          :aria-label="theme === 'light' ? '切换到暗色模式' : '切换到明亮模式'"
          @click="toggleTheme"
        >
          <!-- 明亮态：显示月亮（点击去暗色） -->
          <svg v-if="theme === 'light'" class="icon" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
            <path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z" />
          </svg>
          <!-- 暗色态：显示太阳（点击去明亮） -->
          <svg v-else class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
               stroke-linecap="round" aria-hidden="true">
            <circle cx="12" cy="12" r="4" />
            <line x1="12" y1="2" x2="12" y2="4" />
            <line x1="12" y1="20" x2="12" y2="22" />
            <line x1="2" y1="12" x2="4" y2="12" />
            <line x1="20" y1="12" x2="22" y2="12" />
            <line x1="4.9" y1="4.9" x2="6.3" y2="6.3" />
            <line x1="17.7" y1="17.7" x2="19.1" y2="19.1" />
            <line x1="4.9" y1="19.1" x2="6.3" y2="17.7" />
            <line x1="17.7" y1="6.3" x2="19.1" y2="4.9" />
          </svg>
        </button>
        <span class="uname">{{ authUser?.username }}</span>
        <button class="link" @click="onLogout">退出</button>
      </div>
    </header>

    <div class="body">
      <div v-if="sidebarOpen" class="backdrop" @click="closeSidebar"></div>
      <Sidebar />
      <section class="main">
        <ChatView v-show="currentView === 'chat'" />
        <MemoryView v-show="currentView === 'memory'" />
      </section>
    </div>
  </div>
</template>

<style scoped>
.home { flex: 1; min-height: 0; display: flex; flex-direction: column; overflow: hidden; }
.topbar {
  display: flex; justify-content: space-between; align-items: center;
  padding: 12px 20px; border-bottom: 1px solid var(--line); background: var(--surface); flex: none; gap: 10px;
}
.title { font-weight: 600; min-width: 120px; }
.nav { display: flex; gap: 4px; background: var(--bg); padding: 3px; border-radius: 10px; }
.nav button { background: none; color: var(--muted); padding: 6px 16px; font-size: 14px; }
.nav button.active { background: var(--surface); color: var(--fg); box-shadow: var(--shadow-sm); }
.user { display: flex; align-items: center; gap: 12px; font-size: 14px; }
.uname { color: var(--muted); }
.theme-toggle {
  display: inline-flex; align-items: center; justify-content: center;
  width: 34px; height: 34px; padding: 0; flex: none;
  background: var(--surface-2); color: var(--fg); border-radius: 8px;
}
.theme-toggle:hover { background: var(--chip-bg); color: var(--accent); }
.theme-toggle:disabled { opacity: 0.6; cursor: default; }
.theme-toggle .icon { width: 18px; height: 18px; display: block; }
.icon.spinning { animation: spin 0.9s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }

/* 导出结果提示：顶栏右侧浮出，数秒后自动消失 */
.export-tip {
  font-size: 12.5px; color: var(--accent); background: var(--chip-bg);
  border: 1px solid var(--line); border-radius: 8px; padding: 4px 10px;
  max-width: 420px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.tipfade-enter-active, .tipfade-leave-active { transition: opacity 0.25s ease, transform 0.25s ease; }
.tipfade-enter-from, .tipfade-leave-to { opacity: 0; transform: translateY(-4px); }
.body { flex: 1; min-height: 0; display: flex; overflow: hidden; position: relative; }
.main { flex: 1; min-width: 0; min-height: 0; display: flex; flex-direction: column; overflow: hidden; }

/* 汉堡按钮：默认隐藏，移动端显示 */
.hamburger { display: none; flex-direction: column; justify-content: center; gap: 4px; width: 36px; height: 36px; padding: 8px; background: none; }
.hamburger span { display: block; height: 2px; background: var(--fg); border-radius: 2px; }

@media (max-width: 720px) {
  .hamburger { display: flex; }
  .title { min-width: 0; font-size: 15px; }
  .nav button { padding: 6px 12px; }
  /* 侧边栏变抽屉：默认移出屏幕，drawer-open 时滑入；背景遮罩 */
  .backdrop {
    position: absolute; inset: 0; background: var(--overlay); z-index: 40;
  }
}
</style>
