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
  <div class="app-shell">
    <LoginView v-if="authReady && !isLoggedIn" />
    <HomeView v-else-if="isLoggedIn" />
    <div v-else class="boot">加载中…</div>
  </div>
</template>

<style scoped>
.app-shell {
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
}
.boot {
  color: var(--muted);
  font-size: 14px;
}
</style>
