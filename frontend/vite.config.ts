import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 极简配置：仅启用 Vue 插件，dev 跑在 5173（与后端 CORS 白名单一致）。
// 后端 API 地址通过 VITE_API_BASE 注入（默认 http://localhost:8000，CORS 已放开）。
export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5173,
    // 若不想依赖 CORS，可改用同源代理：
    // proxy: { '/auth': 'http://localhost:8000', '/sessions': 'http://localhost:8000',
    //          '/memory': 'http://localhost:8000', '/recommend': 'http://localhost:8000' }
  },
})
