import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './e2e',
  // 匹配 e2e 目录下所有 spec 文件（含 journey、page、feature 等测试）
  testMatch: '**/*.spec.ts',
  fullyParallel: false,
  // CI 硬化：CI 上禁 .only、失败重试 2 次抓 flaky、首次重试留 trace 便于定位
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  workers: 1,
  reporter: [['list']],
  timeout: 180_000,
  use: {
    baseURL: 'http://localhost:5188',
    actionTimeout: 15_000,
    navigationTimeout: 30_000,
    screenshot: 'off',
    trace: 'on-first-retry',
  },
  projects: [
    {
      name: 'chromium',
      use: {
        browserName: 'chromium',
        viewport: { width: 1920, height: 1080 },
        launchOptions: { args: ['--no-sandbox', '--disable-setuid-sandbox'] },
      },
    },
  ],
});
