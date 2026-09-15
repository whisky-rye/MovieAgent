<script setup lang="ts">
import { ref } from 'vue'
import { register, login } from '../store/auth'

type Mode = 'login' | 'register'
const mode = ref<Mode>('login')
const username = ref('')
const password = ref('')
const error = ref('')
const busy = ref(false)

async function submit() {
  error.value = ''
  const u = username.value.trim()
  if (!u || !password.value) {
    error.value = '请输入用户名和密码'
    return
  }
  busy.value = true
  try {
    if (mode.value === 'register') {
      await register(u, password.value)
    } else {
      await login(u, password.value)
    }
    // 成功后由 store 更新 isLoggedIn，App 自动切换到 HomeView
  } catch (e: unknown) {
    const err = e as { detail?: string; message?: string }
    error.value = err?.detail || err?.message || '操作失败，请重试'
  } finally {
    busy.value = false
  }
}

function toggle() {
  mode.value = mode.value === 'login' ? 'register' : 'login'
  error.value = ''
}
</script>

<template>
  <div class="auth-card">
    <h1 class="brand">影片推荐智能体</h1>
    <p class="sub">{{ mode === 'login' ? '登录后开始对话' : '注册一个新账号' }}</p>

    <form @submit.prevent="submit">
      <label>
        <span>用户名</span>
        <input
          v-model="username"
          type="text"
          autocomplete="username"
          :placeholder="mode === 'register' ? '3–64 位' : '用户名'"
        />
      </label>
      <label>
        <span>密码</span>
        <input
          v-model="password"
          type="password"
          :autocomplete="mode === 'register' ? 'new-password' : 'current-password'"
          placeholder="至少 6 位"
        />
      </label>

      <p v-if="error" class="err">{{ error }}</p>

      <button type="submit" :disabled="busy">
        {{ busy ? '处理中…' : (mode === 'login' ? '登录' : '注册') }}
      </button>
    </form>

    <p class="switch">
      {{ mode === 'login' ? '还没有账号？' : '已有账号？' }}
      <button type="button" class="link" @click="toggle">
        {{ mode === 'login' ? '去注册' : '去登录' }}
      </button>
    </p>
  </div>
</template>

<style scoped>
.auth-card {
  width: 100%;
  max-width: 360px;
  background: #fff;
  border: 1px solid var(--line);
  border-radius: 12px;
  padding: 28px 24px;
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.04);
}
.brand { font-size: 20px; margin: 0 0 4px; }
.sub { color: var(--muted); margin: 0 0 20px; font-size: 13px; }
form { display: flex; flex-direction: column; gap: 14px; }
label { display: flex; flex-direction: column; gap: 6px; font-size: 13px; color: var(--muted); }
.err { color: var(--danger); font-size: 13px; margin: 0; }
button[type='submit'] {
  background: var(--accent);
  color: #fff;
  padding: 10px 12px;
  margin-top: 4px;
}
button[type='submit']:disabled { opacity: 0.6; cursor: default; }
.switch { text-align: center; font-size: 13px; color: var(--muted); margin-top: 16px; }
</style>
