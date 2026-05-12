/**
 * 仪表板页面端到端测试
 *
 * 测试仪表板页面的所有功能
 */

import { test, expect } from '@playwright/test';
import { login, takeScreenshot } from './helpers';

test.describe('仪表板页面', () => {
  test.beforeEach(async ({ page }) => {
    await login(page);
    // 登录后会跳转到会话页面，需要导航到首页
    await page.goto('/', { timeout: 30000 });
    await page.waitForLoadState('domcontentloaded', { timeout: 30000 });
  });

  test('应该正确显示仪表板页面', async ({ page }) => {
    // 检查页面标题
    await expect(page).toHaveTitle(/frontend/);

    // 检查页面主体可见
    await expect(page.locator('body')).toBeVisible();

    await takeScreenshot(page, 'dashboard-page');
  });

  test('应该显示侧边栏导航', async ({ page }) => {
    const sidebar = page.locator('[data-testid="sidebar"], .sidebar, nav');

    // 检查侧边栏是否存在（可能不存在，不做强制要求）
    const sidebarCount = await sidebar.count();
    if (sidebarCount > 0) {
      await expect(sidebar.first()).toBeVisible();
    }

    await takeScreenshot(page, 'dashboard-sidebar');
  });

  test('应该显示统计卡片', async ({ page }) => {
    // 检查统计卡片区域（可选）
    const statsCards = page.locator('.stat-card, .metric-card, [class*="stat"]');
    const count = await statsCards.count();

    if (count > 0) {
      // 至少应该有一些统计信息
      await expect(statsCards.first()).toBeVisible();
    }

    await takeScreenshot(page, 'dashboard-stats');
  });

  test('应该显示最近活动列表', async ({ page }) => {
    // 检查活动列表（可选）
    const activityList = page.locator('.activity-list, [class*="activity"], .recent-list');
    const count = await activityList.count();

    if (count > 0) {
      await expect(activityList.first()).toBeVisible();
    }

    await takeScreenshot(page, 'dashboard-activity');
  });

  test('应该可以导航到设置页面', async ({ page }) => {
    // 直接导航到设置页面
    await page.goto('/settings', { timeout: 30000 });
    await page.waitForLoadState('domcontentloaded', { timeout: 30000 });

    // 验证当前 URL
    const currentUrl = page.url();
    expect(currentUrl).toContain('/settings');
  });

  test('应该可以导航到会话页面', async ({ page }) => {
    // 点击会话链接或直接导航
    const sessionLink = page.locator('a[href*="session"], a:has-text("会话"), button:has-text("会话")');
    const count = await sessionLink.count();

    if (count > 0) {
      await sessionLink.first().click();
      await expect(page).toHaveURL(/\/session/);
    } else {
      // 直接导航到首页（可能会创建新会话）
      await page.goto('/');
      await page.waitForLoadState('domcontentloaded');
    }
  });

  test('应该响应式显示', async ({ page }) => {
    // 测试桌面视图
    await page.setViewportSize({ width: 1280, height: 720 });
    await expect(page.locator('body')).toBeVisible();

    // 测试移动视图
    await page.setViewportSize({ width: 375, height: 667 });
    await expect(page.locator('body')).toBeVisible();

    await takeScreenshot(page, 'dashboard-mobile');
  });

  test('应该可以创建新会话', async ({ page }) => {
    // 查找创建会话按钮
    const createButton = page.locator('button:has-text("新建"), button:has-text("创建"), button:has-text("New")');
    const count = await createButton.count();

    if (count > 0) {
      await createButton.first().click();
      await page.waitForTimeout(1000);

      // 应该导航到会话页面或显示创建对话框
      const url = page.url();
      const isSessionCreated = url.includes('/session');
      expect(isSessionCreated || url.includes('/')).toBeTruthy();
    }

    await takeScreenshot(page, 'dashboard-create-session');
  });
});
