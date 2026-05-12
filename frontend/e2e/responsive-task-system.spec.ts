/**
 * 任务系统响应式设计测试
 *
 * 测试任务系统在不同屏幕尺寸下的显示效果
 */

import { test, expect } from '@playwright/test';
import { quickLogin, createSession } from '../e2e/helpers';
import { createTask } from '../tests/helpers/task-helpers';

test.describe('任务系统响应式设计', () => {
  let sessionId: string;
  let taskId: string;

  test.beforeEach(async ({ page }) => {
    await quickLogin(page);
    const session = await createSession(page, { name: '响应式测试' });
    sessionId = session.id!;
    await page.goto(`/sessions/${sessionId}`);

    const result = await createTask(page, '响应式测试任务', sessionId);
    taskId = result.taskId!;
  });

  test('应该在桌面视图下正常显示', async ({ page }) => {
    // 桌面尺寸
    await page.setViewportSize({ width: 1920, height: 1080 });

    const taskCard = page.locator(`[data-task-id="${taskId}"]`);
    await expect(taskCard).toBeVisible();

    // 验证布局完整
    const sidebar = page.locator('.sidebar, [data-testid="sidebar"]');
    const executionPanel = page.locator('.execution-panel, [data-testid="execution-panel"]');

    await expect(sidebar).toBeVisible();
    await expect(executionPanel).toBeVisible();
  });

  test('应该在平板视图下适配显示', async ({ page }) => {
    // 平板尺寸
    await page.setViewportSize({ width: 768, height: 1024 });

    const taskCard = page.locator(`[data-task-id="${taskId}"]`);
    await expect(taskCard).toBeVisible();

    // 验证侧边栏默认折叠
    const sidebar = page.locator('.sidebar');
    const sidebarCollapsed = await sidebar.getAttribute('data-collapsed');

    expect(sidebarCollapsed).toBe('true');

    // 验证任务面板宽度适配
    const taskPanel = page.locator('.task-panel, [data-testid="task-panel"]');
    await expect(taskPanel).toBeVisible();
  });

  test('应该在移动视图下精简显示', async ({ page }) => {
    // 移动尺寸
    await page.setViewportSize({ width: 375, height: 667 });

    const taskCard = page.locator(`[data-task-id="${taskId}"]`);
    await expect(taskCard).toBeVisible();

    // 验证侧边栏隐藏
    const sidebar = page.locator('.sidebar');
    const isSidebarVisible = await sidebar.isVisible().catch(() => false);
    expect(isSidebarVisible).toBeFalsy();

    // 验证执行图面板默认隐藏
    const executionPanel = page.locator('.execution-panel');
    const isPanelVisible = await executionPanel.isVisible().catch(() => false);
    expect(isPanelVisible).toBeFalsy();

    // 验证任务卡片精简模式
    const compactMode = await taskCard.getAttribute('data-compact');
    expect(compactMode).toBe('true');
  });

  test('应该在移动视图下支持菜单切换', async ({ page }) => {
    await page.setViewportSize({ width: 375, height: 667 });

    // 点击菜单按钮
    const menuButton = page.locator('button:has-text("菜单"), [data-testid="menu-button"]');
    await menuButton.click();

    // 验证侧边栏打开
    const sidebar = page.locator('.sidebar');
    await expect(sidebar).toBeVisible();

    // 点击关闭
    const closeButton = sidebar.locator('button:has-text("关闭")');
    await closeButton.click();

    // 验证侧边栏关闭
    await expect(sidebar).not.toBeVisible();
  });

  test('应该支持横屏模式', async ({ page }) => {
    // 横屏移动设备
    await page.setViewportSize({ width: 667, height: 375 });

    const taskCard = page.locator(`[data-task-id="${taskId}"]`);
    await expect(taskCard).toBeVisible();

    // 验证布局适配横屏
    const layout = page.locator('.layout, [data-testid="layout"]');
    const orientation = await layout.getAttribute('data-orientation');

    expect(orientation).toBe('landscape');
  });

  test('应该支持大屏幕分辨率', async ({ page }) => {
    // 4K 分辨率
    await page.setViewportSize({ width: 3840, height: 2160 });

    const taskCard = page.locator(`[data-task-id="${taskId}"]`);
    await expect(taskCard).toBeVisible();

    // 验证内容不会过度拉伸
    const maxWidth = await taskCard.evaluate(el => {
      return window.getComputedStyle(el).maxWidth;
    });

    expect(maxWidth).toBeTruthy();
  });

  test('应该在窗口缩放时重新布局', async ({ page }) => {
    // 桌面尺寸
    await page.setViewportSize({ width: 1920, height: 1080 });
    await expect(page.locator('.sidebar')).toBeVisible();

    // 缩小到移动尺寸
    await page.setViewportSize({ width: 375, height: 667 });

    // 等待布局调整
    await page.waitForTimeout(500);

    const sidebar = page.locator('.sidebar');
    const isSidebarVisible = await sidebar.isVisible().catch(() => false);
    expect(isSidebarVisible).toBeFalsy();

    // 放大回桌面尺寸
    await page.setViewportSize({ width: 1920, height: 1080 });
    await page.waitForTimeout(500);

    await expect(sidebar).toBeVisible();
  });
});
