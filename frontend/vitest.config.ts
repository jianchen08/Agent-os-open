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
  },
})
