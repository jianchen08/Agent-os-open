/**
 * WebSocket集成测试配置
 * 专门用于运行WebSocket相关的集成测试
 */

import { defineConfig } from 'vitest/config'
import { resolve } from 'path'

export default defineConfig({
  test: {
    // 测试环境：Node.js（因为需要使用ws库）
    environment: 'node',

    // 测试超时时间（10秒）
    testTimeout: 10000,
    hookTimeout: 10000,

    // 并行执行测试
    threads: true,
    maxThreads: 4,
    minThreads: 1,

    // 测试文件匹配模式
    include: [
      'tests/integration/**/*.test.ts',
      'tests/performance/**/*.test.ts',
    ],

    // 排除的文件
    exclude: [
      'node_modules',
      'dist',
      '.idea',
      '.git',
      '.cache',
    ],

    // 覆盖率配置
    coverage: {
      provider: 'v8',
      reporter: ['text', 'json', 'html', 'lcov'],
      include: [
        'src/services/websocket/**/*.ts',
        'src/constants/websocket.ts',
      ],
      exclude: [
        'src/**/*.test.ts',
        'src/**/*.spec.ts',
        'src/types/**/*.ts',
      ],
      // 覆盖率阈值
      thresholds: {
        lines: 80,
        functions: 80,
        branches: 70,
        statements: 80,
      },
      // 覆盖率输出目录
      reportsDirectory: 'coverage/ws-integration',
    },

    // 报告器配置
    reporters: ['verbose', 'json'],

    // 输出目录
    outputFile: {
      json: 'test-results/ws-integration.json',
      html: 'test-results/ws-integration.html',
    },

    // 全局设置
    globals: true,

    // 设置文件
    setupFiles: [],

    // 监听模式配置
    watch: false,

    // 隔离环境
    isolate: true,

    // 是否在失败时显示完整堆栈
    stackTrace: true,

    // 是否显示详细错误
    showHeapUsage: true,

    // 测试运行器选项
    benchmark: {
      // 是否包含示例
      includeSamples: false,
    },
  },

  // 路径别名
  resolve: {
    alias: {
      '@': resolve(__dirname, './src'),
      '@tests': resolve(__dirname, './tests'),
    },
  },
})
