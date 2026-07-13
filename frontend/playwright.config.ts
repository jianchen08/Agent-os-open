import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './e2e',
  // 匹配 e2e 目录下所有 spec 文件（含 journey、page、feature 等测试）
  testMatch: '**/*.spec.ts',
  fullyParallel: false,
  forbidOnly: false,
  retries: 0,
  workers: 1,
  reporter: [['list']],
  timeout: 180_000,
  use: {
    baseURL: 'http://localhost:5188',
    actionTimeout: 15_000,
    navigationTimeout: 30_000,
    screenshot: 'off',
    trace: 'off',
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
