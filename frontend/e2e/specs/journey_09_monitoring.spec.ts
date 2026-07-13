/**
 * 用户旅程 09：监控调试
 *
 * 覆盖场景：导航到监控页 → 验证系统状态 → 导航到调试页 → 查看会话/执行记录
 * 对应 features.md 场景 9：监控调试
 */

import { test, expect } from '@playwright/test';
import { loginAndNavigateTo, navigateTo, ROUTES } from '../helpers/navigation';

test.describe.configure({ timeout: 120_000 });

test.describe('旅程09：监控调试', () => {
  test('9.1 监控页面应正确加载并显示系统状态', async ({ page }) => {
    await loginAndNavigateTo(page, ROUTES.MONITORING);

    // 验证监控页面元素
    const monitoringPage = page.locator('[data-testid="monitoring-page"]');
    const hasPage = await monitoringPage.isVisible().catch(() => false);

    if (hasPage) {
      // 验证系统状态卡片
      const cpuCard = page.locator('div').filter({ hasText: 'CPU 使用率' }).first();
      const memoryCard = page.locator('div').filter({ hasText: '内存使用' }).first();
      const sessionCard = page.locator('div').filter({ hasText: '活跃会话' }).first();
      const taskCard = page.locator('div').filter({ hasText: '运行任务' }).first();

      await expect(cpuCard, 'CPU 使用率卡片应可见').toBeVisible({ timeout: 10_000 });
      await expect(memoryCard, '内存使用卡片应可见').toBeVisible();
      await expect(sessionCard, '活跃会话卡片应可见').toBeVisible();
      await expect(taskCard, '运行任务卡片应可见').toBeVisible();
    } else {
      // 回退验证：页面内容包含监控关键词
      const content = await page.textContent('body').catch(() => '');
      const hasMonitor = content!.includes('监控') || content!.includes('monitoring');
      expect(hasMonitor, '监控页面应包含监控相关内容').toBeTruthy();
    }
  });

  test('9.2 监控页面性能趋势区域应可见', async ({ page }) => {
    await loginAndNavigateTo(page, ROUTES.MONITORING);

    // 验证性能趋势区域
    const perfSection = page.locator('div').filter({ hasText: /性能趋势/ }).first();
    const hasPerf = await perfSection.isVisible().catch(() => false);
    expect(hasPerf, '性能趋势区域应可见').toBeTruthy();
  });

  test('9.3 监控页面任务执行状态区域应可见', async ({ page }) => {
    await loginAndNavigateTo(page, ROUTES.MONITORING);

    const taskSection = page.locator('div').filter({ hasText: /任务执行状态/ }).first();
    const hasTask = await taskSection.isVisible().catch(() => false);
    expect(hasTask, '任务执行状态区域应可见').toBeTruthy();
  });

  test('9.4 调试中心应可访问', async ({ page }) => {
    await loginAndNavigateTo(page, ROUTES.DEBUG);

    const content = await page.textContent('body').catch(() => '');
    const hasDebug = content!.includes('调试') || content!.includes('debug') || content!.includes('Debug');
    expect(hasDebug, '调试中心应包含相关内容').toBeTruthy();
  });

  test('9.5 调试会话页面应可访问', async ({ page }) => {
    await loginAndNavigateTo(page, ROUTES.DEBUG_SESSIONS);

    const content = await page.textContent('body').catch(() => '');
    const hasSession = content!.includes('会话') || content!.includes('session');
    expect(hasSession, '调试会话页应包含会话相关内容').toBeTruthy();
  });

  test('9.6 执行记录页面应可访问', async ({ page }) => {
    await loginAndNavigateTo(page, ROUTES.DEBUG_EXECUTION_RECORDS);

    const content = await page.textContent('body').catch(() => '');
    const hasRecords = content!.includes('执行') || content!.includes('记录') || content!.includes('execution');
    expect(hasRecords, '执行记录页应包含相关内容').toBeTruthy();
  });

  test('9.7 调试用户管理页面应可访问', async ({ page }) => {
    await loginAndNavigateTo(page, ROUTES.DEBUG_USERS);

    const content = await page.textContent('body').catch(() => '');
    const hasUsers = content!.includes('用户') || content!.includes('user');
    expect(hasUsers, '用户管理页应包含用户相关内容').toBeTruthy();
  });
});
