/**
 * 管道配置设置页专用 Playwright 配置
 *
 * 关键点：用 playwright 内置 webServer 管理 vite dev server 生命周期——
 * playwright 在测试前 spawn vite，测试后自动清理，避免 bash_execute
 * 跨调用回收后台进程导致 vite 中途被终止的问题。
 *
 * 运行：npx playwright test --config=playwright.pipeline.config.ts --reporter=list
 */
import { defineConfig } from '@playwright/test'

export default defineConfig({
  testDir: './e2e',
  // 仅运行管道配置验证 spec
  testMatch: '**/zz_pipeline_settings_verify.spec.ts',
  fullyParallel: false,
  workers: 1,
  retries: 0,
  forbidOnly: false,
  reporter: [['list']],
  timeout: 90_000,



  use: {
    baseURL: 'http://localhost:5290',
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
        // 使用已修复权限的 chromium-1234（playwright 1.61.1 默认期望 1228 不存在）
        launchOptions: {
          executablePath: '/opt/ms-playwright/chromium-1234/chrome-linux64/chrome',
          args: ['--no-sandbox', '--disable-setuid-sandbox', '--headless=new'],
        } as any,
      },
    },
  ],
})
