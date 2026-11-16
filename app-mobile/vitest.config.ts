import { defineConfig } from 'vitest/config'

export default defineConfig({
  test: {
    include: ['src/**/*.test.ts'],
    environment: 'node',
    // 面板测试用 jsdom（文件头 // @vitest-environment jsdom 单文件声明）
    testTimeout: 15000,
  },
})
