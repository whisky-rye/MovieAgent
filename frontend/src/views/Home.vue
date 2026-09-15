<script setup lang="ts">
import { authUser, logout } from '../store/auth'
import { currentView, setView } from '../store/ui'
import Sidebar from './Sidebar.vue'
import ChatView from './ChatView.vue'
import MemoryView from './MemoryView.vue'
</script>

<template>
  <div class="home">
    <header class="topbar">
      <span class="title">影片推荐智能体</span>
      <nav class="nav">
        <button :class="{ active: currentView === 'chat' }" @click="setView('chat')">对话</button>
        <button :class="{ active: currentView === 'memory' }" @click="setView('memory')">记忆</button>
      </nav>
      <div class="user">
        <span class="uname">{{ authUser?.username }}</span>
        <button class="link" @click="logout">退出</button>
      </div>
    </header>

    <div class="body">
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
  padding: 12px 20px; border-bottom: 1px solid var(--line); background: #fff; flex: none;
}
.title { font-weight: 600; min-width: 120px; }
.nav { display: flex; gap: 4px; background: var(--bg); padding: 3px; border-radius: 10px; }
.nav button { background: none; color: var(--muted); padding: 6px 16px; font-size: 14px; }
.nav button.active { background: #fff; color: var(--fg); box-shadow: 0 1px 2px rgba(0,0,0,0.06); }
.user { display: flex; align-items: center; gap: 12px; font-size: 14px; }
.uname { color: var(--muted); }
.body { flex: 1; min-height: 0; display: flex; overflow: hidden; }
.main { flex: 1; min-width: 0; min-height: 0; display: flex; flex-direction: column; overflow: hidden; }
</style>
