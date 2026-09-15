<script setup lang="ts">
import { defineAsyncComponent } from 'vue'
import { authUser, logout } from '../store/auth'
import Sidebar from './Sidebar.vue'

// M3 按需加载：对话界面独立成 chunk，进入 Home 后再异步拉取（最少按需加载）
const ChatView = defineAsyncComponent(() => import('./ChatView.vue'))
</script>

<template>
  <div class="home">
    <header class="topbar">
      <span class="title">影片推荐智能体</span>
      <div class="user">
        <span class="uname">{{ authUser?.username }}</span>
        <button class="link" @click="logout">退出</button>
      </div>
    </header>

    <div class="body">
      <Sidebar />
      <section class="main">
        <ChatView />
      </section>
    </div>
  </div>
</template>

<style scoped>
.home { height: 100vh; display: flex; flex-direction: column; }
.topbar {
  display: flex; justify-content: space-between; align-items: center;
  padding: 12px 20px; border-bottom: 1px solid var(--line); background: #fff; flex: none;
}
.title { font-weight: 600; }
.user { display: flex; align-items: center; gap: 12px; font-size: 14px; }
.uname { color: var(--muted); }
.body { flex: 1; min-height: 0; display: flex; }
.main { flex: 1; min-width: 0; display: flex; }
</style>
