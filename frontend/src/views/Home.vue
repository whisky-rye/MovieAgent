<script setup lang="ts">
import { authUser, logout } from '../store/auth'
import { currentView, setView, sidebarOpen, toggleSidebar, closeSidebar, theme, toggleTheme } from '../store/ui'
import Sidebar from './Sidebar.vue'
import ChatView from './ChatView.vue'
import MemoryView from './MemoryView.vue'
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
        <button class="link" @click="logout">退出</button>
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
.theme-toggle .icon { width: 18px; height: 18px; display: block; }
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
