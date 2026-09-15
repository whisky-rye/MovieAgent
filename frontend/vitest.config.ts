import { defineConfig } from 'vitest/config'

// 前端单元测试配置：纯 Node 环境（SSE 解析与 normalize 均为纯函数，无 DOM 依赖）。
export default defineConfig({
  test: {
    environment: 'node',
    include: ['tests/**/*.test.ts'],
  },
})
