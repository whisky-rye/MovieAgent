<script setup lang="ts">
import { onMounted } from 'vue'
import { initAuth, authReady, isLoggedIn } from './store/auth'
import LoginView from './views/Login.vue'
import HomeView from './views/Home.vue'

onMounted(() => {
  initAuth()
})
</script>

<template>
  <div class="app-shell" :class="{ center: !isLoggedIn }">
    <LoginView v-if="authReady && !isLoggedIn" />
    <HomeView v-else-if="isLoggedIn" />
    <div v-else class="boot">加载中…</div>
  </div>
</template>

<style scoped>
/* 登录后主界面：高度锁定视口，让 .home→.chat→.messages 逐级 flex:1/min-height:0，
   聊天区内部滚动、composer 固定底部；整页不出现外层滚动条。 */
.app-shell {
  height: 100vh;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
/* 仅在登录态居中卡片；登录卡片可超出视口时允许自身滚动 */
.app-shell.center {
  min-height: 100vh;
  height: auto;
  align-items: center;
  justify-content: center;
  padding: 24px;
  overflow: auto;
}
.boot {
  color: var(--muted);
  font-size: 14px;
}
</style>
