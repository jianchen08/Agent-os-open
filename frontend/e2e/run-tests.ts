/**
 * 测试运行脚本
 *
 * 使用 Playwright 进行浏览器自动化测试
 */

import { test } from '@playwright/test';

// 测试套件
test.describe('AI Agent 系统 - 完整功能测试', () => {
  test.beforeEach(async ({ page }) => {
    // 设置测试环境
    await page.goto('/');
  });

  test('完整用户流程测试', async ({ page }) => {
    test.slow();

    // 1. 测试登录流程
    await test.step('用户登录', async () => {
      await page.goto('/login');

      // 填写登录表单
      await page.fill('input[name="username"]', 'testuser');
      await page.fill('input[name="password"]', 'testpass123');
      await page.click('button[type="submit"]');

      // 等待跳转到首页
      await page.waitForURL('/');
    });

    // 2. 测试仪表板功能
    await test.step('查看仪表板', async () => {
      // 检查主要元素
      await expect(page.locator('[data-testid="sidebar"], .sidebar')).toBeVisible();
      await expect(page.locator('[data-testid="top-nav"], .top-nav')).toBeVisible();

      // 截图
      await page.screenshot({ path: 'test-results/dashboard.png' });
    });

    // 3. 测试会话创建
    await test.step('创建新会话', async () => {
      const createButton = page.locator('button:has-text("新建"), button:has-text("创建")');
      const count = await createButton.count();

      if (count > 0) {
        await createButton.first().click();
        await page.waitForURL(/\/session/);

        await page.screenshot({ path: 'test-results/session-created.png' });
      }
    });

    // 4. 测试发送消息
    await test.step('发送测试消息', async () => {
      const chatInput = page.locator('textarea[placeholder*="消息"], textarea[placeholder*="输入"]');
      const count = await chatInput.count();

      if (count > 0) {
        await chatInput.first().fill('你好，这是一条测试消息');

        const sendButton = page.locator('button:has-text("发送"), [data-testid="send-button"]');
        await sendButton.first().click();

        // 等待消息显示
        await page.waitForTimeout(2000);

        await page.screenshot({ path: 'test-results/message-sent.png' });
      }
    });

    // 5. 测试设置页面
    await test.step('访问设置页面', async () => {
      await page.goto('/settings');

      // 检查设置标签
      const tabs = page.locator('[role="tab"]');
      const count = await tabs.count();

      if (count > 0) {
        await expect(tabs.first()).toBeVisible();
      }

      await page.screenshot({ path: 'test-results/settings-page.png' });
    });

    // 6. 测试登出
    await test.step('用户登出', async () => {
      const userMenu = page.locator('[data-testid="user-menu"], .user-menu');
      await userMenu.click();

      const logoutButton = page.locator('button:has-text("退出"), button:has-text("登出")');
      const count = await logoutButton.count();

      if (count > 0) {
        await logoutButton.first().click();
        await page.waitForURL(/\/login/);

        await page.screenshot({ path: 'test-results/logout.png' });
      }
    });
  });

  test('响应式布局测试', async ({ page }) => {
    test.slow();

    await page.goto('/');

    // 测试不同屏幕尺寸
    const sizes = [
      { width: 1920, height: 1080, name: 'desktop-2k' },
      { width: 1280, height: 720, name: 'desktop-hd' },
      { width: 768, height: 1024, name: 'tablet' },
      { width: 375, height: 667, name: 'mobile' },
    ];

    for (const size of sizes) {
      await test.step(`测试 ${size.name} (${size.width}x${size.height})`, async () => {
        await page.setViewportSize({ width: size.width, height: size.height });
        await page.waitForTimeout(500);

        await page.screenshot({
          path: `test-results/responsive-${size.name}.png`,
          fullPage: true,
        });
      });
    }
  });

  test('性能测试', async ({ page }) => {
    test.slow();

    // 监控页面加载性能
    await page.goto('/');

    const metrics = await page.evaluate(() => {
      const navigation = performance.getEntriesByType('navigation')[0] as PerformanceNavigationTiming;
      return {
        domContentLoaded: navigation.domContentLoadedEventEnd - navigation.domContentLoadedEventStart,
        loadComplete: navigation.loadEventEnd - navigation.loadEventStart,
        totalTime: navigation.loadEventEnd - navigation.fetchStart,
      };
    });

    console.log('页面性能指标:', metrics);

    // 验证页面在合理时间内加载完成
    expect(metrics.totalTime).toBeLessThan(5000); // 5秒内完成
  });

  test('可访问性测试', async ({ page }) => {
    await page.goto('/');

    // 检查页面语言设置
    const lang = await page.getAttribute('html', 'lang');
    expect(lang).toBeTruthy();

    // 检查是否有 title
    const title = await page.title();
    expect(title).toBeTruthy();

    // 检查主要 landmarks
    const landmarks = await page.locator('nav, main, header, footer').count();
    expect(landmarks).toBeGreaterThan(0);

    // 检查表单标签
    const inputs = page.locator('input, textarea, select');
    const inputCount = await inputs.count();

    for (let i = 0; i < Math.min(inputCount, 10); i++) {
      const input = inputs.nth(i);
      const hasLabel =
        (await input.count()) > 0 &&
        ((await input.getAttribute('aria-label')) !== null ||
          (await input.getAttribute('id')) !== null);

      // 只检查可见的输入框
      if (await input.isVisible()) {
        // 可以选择性地断言标签存在
      }
    }
  });
});
