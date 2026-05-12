/**
 * Playwright 配置文件
 *
 * 配置端到端测试的运行环境、浏览器和测试行为
 */

import { defineConfig, devices } from '@playwright/test';

export default defineConfig({
  // 测试文件位置
  testDir: './e2e',

  // 测试超时时间（毫秒）- 增加到120秒以支持工作流测试
  timeout: 120 * 1000,

  // 期望断言超时
  expect: {
    timeout: 10000,
  },

  // 测试失败时是否截图
  use: {
    // 基础 URL
    baseURL: process.env.FRONTEND_URL || 'http://localhost:5188',

    // 收集测试失败时的追踪信息
    trace: 'on-first-retry',

    // 测试失败时截图
    screenshot: 'only-on-failure',

    // 录制视频
    video: 'retain-on-failure',

    // 浏览器视口大小
    viewport: { width: 1280, height: 720 },

    // 忽略 HTTPS 错误
    ignoreHTTPSErrors: true,

    // 等待操作超时 - 增加到30秒
    actionTimeout: 30 * 1000,

    // 导航超时
    navigationTimeout: 60 * 1000,
  },

  // 测试项目配置
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },

    {
      name: 'firefox',
      use: { ...devices['Desktop Firefox'] },
    },

    {
      name: 'webkit',
      use: { ...devices['Desktop Safari'] },
    },

    // 移动端测试
    {
      name: 'Mobile Chrome',
      use: { ...devices['Pixel 5'] },
    },
    {
      name: 'Mobile Safari',
      use: { ...devices['iPhone 12'] },
    },
  ],

  // 测试运行前的全局设置
  globalSetup: './e2e/global-setup.ts',

  // 测试运行后的全局清理
  globalTeardown: './e2e/global-teardown.ts',

  // 失败重试配置
  retries: process.env.CI ? 2 : 0,

  // 并发执行配置
  workers: process.env.CI ? 1 : undefined,

  // 测试报告配置
  reporter: [
    ['html', { open: 'never', outputFolder: 'playwright-report' }],
    ['json', { outputFile: 'test-results.json' }],
    ['junit', { outputFile: 'junit-results.xml' }],
    ['list'],
  ],

  // 输出目录
  outputDir: 'test-results',
});
