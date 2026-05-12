/**
 * 任务系统主题切换测试
 *
 * 测试任务系统在不同主题下的显示效果
 */

import { test, expect } from '@playwright/test';
import { quickLogin, createSession, switchTheme, verifyTheme } from '../e2e/helpers';
import { createTask } from '../tests/helpers/task-helpers';

test.describe('任务系统主题切换', () => {
  let sessionId: string;
  let taskId: string;

  test.beforeEach(async ({ page }) => {
    await quickLogin(page);
    const session = await createSession(page, { name: '主题测试' });
    sessionId = session.id!;
    await page.goto(`/sessions/${sessionId}`);

    const result = await createTask(page, '主题测试任务', sessionId);
    taskId = result.taskId!;
  });

  test('应该在深色主题下正确显示任务卡片', async ({ page }) => {
    // 切换到深色主题
    await switchTheme(page, 'dark');
    await verifyTheme(page, 'dark');

    // 验证任务卡片颜色
    const taskCard = page.locator(`[data-task-id="${taskId}"]`);
    await expect(taskCard).toBeVisible();

    const cardBackground = await taskCard.evaluate(el => {
      return window.getComputedStyle(el).backgroundColor;
    });

    // 深色主题背景应该较暗
    expect(cardBackground).not.toBe('rgb(255, 255, 255)');
  });

  test('应该在浅色主题下正确显示任务卡片', async ({ page }) => {
    // 切换到浅色主题
    await switchTheme(page, 'light');
    await verifyTheme(page, 'light');

    // 验证任务卡片颜色
    const taskCard = page.locator(`[data-task-id="${taskId}"]`);
    await expect(taskCard).toBeVisible();

    const cardBackground = await taskCard.evaluate(el => {
      return window.getComputedStyle(el).backgroundColor;
    });

    // 浅色主题背景应该较亮
    const isLight = cardBackground.includes('255') || cardBackground.includes('254');
    expect(isLight).toBeTruthy();
  });

  test('应该在深色主题下正确显示阶段指示器', async ({ page }) => {
    await switchTheme(page, 'dark');

    const taskCard = page.locator(`[data-task-id="${taskId}"]`);
    const phaseIndicator = taskCard.locator('.phase-indicator');

    await expect(phaseIndicator).toBeVisible();

    // 验证对比度
    const indicatorColor = await phaseIndicator.evaluate(el => {
      return window.getComputedStyle(el).color;
    });

    expect(indicatorColor).toBeTruthy();
  });

  test('应该在浅色主题下正确显示阶段指示器', async ({ page }) => {
    await switchTheme(page, 'light');

    const taskCard = page.locator(`[data-task-id="${taskId}"]`);
    const phaseIndicator = taskCard.locator('.phase-indicator');

    await expect(phaseIndicator).toBeVisible();

    const indicatorColor = await phaseIndicator.evaluate(el => {
      return window.getComputedStyle(el).color;
    });

    expect(indicatorColor).toBeTruthy();
  });

  test('应该在主题切换时保持任务状态', async ({ page }) => {
    // 深色主题
    await switchTheme(page, 'dark');
    const taskCardDark = page.locator(`[data-task-id="${taskId}"]`);
    await expect(taskCardDark).toBeVisible();

    // 浅色主题
    await switchTheme(page, 'light');
    const taskCardLight = page.locator(`[data-task-id="${taskId}"]`);
    await expect(taskCardLight).toBeVisible();

    // 验证任务 ID 相同
    const darkId = await taskCardDark.getAttribute('data-task-id');
    const lightId = await taskCardLight.getAttribute('data-task-id');
    expect(darkId).toBe(lightId);
  });

  test('应该正确显示 AC 状态颜色', async ({ page }) => {
    // 深色主题
    await switchTheme(page, 'dark');

    const taskCard = page.locator(`[data-task-id="${taskId}"]`);
    await taskCard.locator('button:has-text("验收标准")').click();

    const acStatus = taskCard.locator('.ac-status').first();

    // 验证状态颜色可见
    const statusColor = await acStatus.evaluate(el => {
      return window.getComputedStyle(el).color || el.getAttribute('style');
    });

    expect(statusColor).toBeTruthy();

    // 切换到浅色主题
    await switchTheme(page, 'light');

    const lightStatusColor = await acStatus.evaluate(el => {
      return window.getComputedStyle(el).color || el.getAttribute('style');
    });

    expect(lightStatusColor).toBeTruthy();
  });
});
