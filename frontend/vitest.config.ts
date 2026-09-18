import { defineConfig } from 'vitest/config'
import vue from '@vitejs/plugin-vue'

// 前端单元测试配置：
// - 默认 Node 环境（SSE 解析与 normalize 等纯函数无 DOM 依赖）；
// - 引入 @vitejs/plugin-vue，使组件测试可 import .vue 文件（组件测试文件用
//   `// @vitest-environment happy-dom` 切换到 DOM 环境）。
export default defineConfig({
  plugins: [vue()],
  test: {
    environment: 'node',
    include: ['tests/**/*.test.ts'],
  },
})
