/// <reference types="vitest/config" />
import react from '@vitejs/plugin-react'
import path from 'path'
import { defineConfig } from 'vitest/config'

/**
 * Vitest 组件测试配置
 *
 * 使用 jsdom 模拟浏览器环境，配置 @testing-library/jest-dom 扩展匹配器
 * 用于 React 组件级功能测试
 */
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
      '@/components': path.resolve(__dirname, './src/components'),
      '@/pages': path.resolve(__dirname, './src/pages'),
      '@/stores': path.resolve(__dirname, './src/stores'),
      '@/services': path.resolve(__dirname, './src/services'),
      '@/types': path.resolve(__dirname, './src/types'),
      '@/utils': path.resolve(__dirname, './src/utils'),
      '@/hooks': path.resolve(__dirname, './src/hooks'),
      '@/constants': path.resolve(__dirname, './src/constants'),
      '@/assets': path.resolve(__dirname, './src/assets'),
    },
  },
  test: {
    // 使用 jsdom 模拟浏览器 DOM 环境
    environment: 'jsdom',
    // 引入 @testing-library/jest-dom 扩展匹配器
    setupFiles: ['./src/test/setup.ts'],
    // 测试文件匹配模式
    include: ['src/**/*.test.{ts,tsx}', 'tests/**/*.test.{ts,tsx}'],
    exclude: ['node_modules', 'dist'],
    // 全局 API（describe, it, expect 等）
    globals: true,
    // 超时配置
    testTimeout: 10000,
    hookTimeout: 10000,
    // 不监听，单次运行
    watch: false,
    // 关闭 CSS 处理
    css: false,
    // 覆盖率（阶段 2.2）：include 从 4 个文件放开到全量 src，产出真实全量基线。
    // thresholds 从 1 起步（非阻塞地板），CI（Node20）跑出基线后按表 D 逐级上调。
    // ⚠️ v8 provider 的 remapCoverage 在 Node25 本地会抛错——覆盖率请在 Node20 下运行
    // （CI frontend-test 已固定 node-version 20；本地用 nvm use 20 后再 npm run test:coverage）。
    coverage: {
      provider: 'v8',
      // json-summary / lcov 供阶段 5.3 矩阵覆盖率列自动回填消费
      reporter: ['text', 'text-summary', 'json-summary', 'lcov'],
      include: ['src/**/*.{ts,tsx}'],
      exclude: [
        'src/**/__tests__/**',
        'src/**/*.test.{ts,tsx}',
        'src/test/**',
        'src/**/*.d.ts',
        'src/main.tsx',
        'src/vite-env.d.ts',
      ],
      thresholds: {
        lines: 1,
        functions: 1,
        statements: 1,
        branches: 1,
      },
    },
  },
})
