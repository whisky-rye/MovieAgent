<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { initAuth, authReady, isLoggedIn } from './store/auth'
import LoginView from './views/Login.vue'
import HomeView from './views/Home.vue'
import ShareView from './views/ShareView.vue'

// 公开只读分享态：若直接访问 /share/{token}（外部访客，无需登录），则跳过鉴权，
// 直接进入 ShareView；否则正常走登录/主页流程。
const shareToken = ref<string | null>(null)

onMounted(() => {
  const m = window.location.pathname.match(/^\/share\/([^/]+)/)
  if (m && m[1]) {
    shareToken.value = m[1]
    return
  }
  initAuth()
})

function exitShare() {
  // 不改变 URL hash，仅把路径还原为根，回到主应用（按当前登录态决定登录或主页）
  window.history.pushState({}, '', '/')
  shareToken.value = null
  initAuth()
}
</script>

<template>
  <div class="app-shell" :class="{ center: !shareToken && !isLoggedIn }">
    <!-- 公开只读分享：免登录 -->
    <ShareView v-if="shareToken" :token="shareToken" @exit="exitShare" />
    <template v-else>
      <LoginView v-if="authReady && !isLoggedIn" />
      <HomeView v-else-if="isLoggedIn" />
      <div v-else class="boot">加载中…</div>
    </template>
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
