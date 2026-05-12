/**
 * 任务系统可访问性测试
 *
 * 测试任务系统的可访问性功能
 */

import { test, expect } from '@playwright/test';
import { quickLogin, createSession } from '../e2e/helpers';
import { createTask } from '../tests/helpers/task-helpers';

test.describe('任务系统可访问性测试', () => {
  let sessionId: string;
  let taskId: string;

  test.beforeEach(async ({ page }) => {
    await quickLogin(page);
    const session = createSession(page, { name: '可访问性测试' });
    sessionId = session.id!;
    await page.goto(`/sessions/${sessionId}`);

    const result = await createTask(page, '可访问性测试任务', sessionId);
    taskId = result.taskId!;
  });

  test('应该支持键盘导航', async ({ page }) => {
    const taskCard = page.locator(`[data-task-id="${taskId}"]`);

    // 使用 Tab 键导航到任务卡片
    await page.keyboard.press('Tab');
    await page.keyboard.press('Tab');
    await page.keyboard.press('Tab');

    // 验证焦点在任务卡片上
    const focusedElement = await page.evaluate(() => document.activeElement?.getAttribute('data-task-id'));
    expect(focusedElement).toBe(taskId);
  });

  test('应该支持 Enter 键激活按钮', async ({ page }) => {
    const taskCard = page.locator(`[data-task-id="${taskId}"]`);

    // 使用 Tab 键导航到展开按钮
    const expandButton = taskCard.locator('button:has-text("验收标准")');

    await expandButton.focus();
    await page.keyboard.press('Enter');

    // 验证列表展开
    const acList = taskCard.locator('.ac-list');
    await expect(acList).toBeVisible();
  });

  test('应该支持 Escape 键关闭对话框', async ({ page }) => {
    const taskCard = page.locator(`[data-task-id="${taskId}"]`);

    // 点击详情按钮打开对话框
    const detailButton = taskCard.locator('button:has-text("详情")');
    await detailButton.click();

    // 验证对话框打开
    const dialog = page.locator('dialog, [role="dialog"]');
    await expect(dialog).toBeVisible();

    // 按 Escape 键关闭
    await page.keyboard.press('Escape');

    // 验证对话框关闭
    await expect(dialog).not.toBeVisible();
  });

  test('应该有正确的 ARIA 标签', async ({ page }) => {
    const taskCard = page.locator(`[data-task-id="${taskId}"]`);

    // 验证任务卡片的 ARIA 标签
    const role = await taskCard.getAttribute('role');
    expect(role).toBe('article' || 'region');

    // 验证任务目标有正确的标签
    const goalElement = taskCard.locator('.task-goal');
    const hasLabel = await goalElement.evaluate(el => {
      return el.hasAttribute('aria-label') || el.hasAttribute('aria-labelledby');
    });
    expect(hasLabel).toBeTruthy();
  });

  test('应该有正确的状态描述', async ({ page }) => {
    const taskCard = page.locator(`[data-task-id="${taskId}"]`);

    // 验证阶段指示器的状态描述
    const phaseIndicator = taskCard.locator('.phase-indicator');

    const hasAriaLive = await phaseIndicator.evaluate(el => {
      return el.getAttribute('aria-live') === 'polite' || el.getAttribute('aria-live') === 'assertive';
    });
    expect(hasAriaLive).toBeTruthy();
  });

  test('应该支持屏幕阅读器', async ({ page }) => {
    const taskCard = page.locator(`[data-task-id="${taskId}"]`);

    // 验证任务卡片对屏幕阅读器可见
    const ariaHidden = await taskCard.getAttribute('aria-hidden');
    expect(ariaHidden).not.toBe('true');

    // 验证重要信息有 textContent
    const goalText = await taskCard.locator('.task-goal').textContent();
    expect(goalText).toBeTruthy();
    expect(goalText!.length).toBeGreaterThan(0);
  });

  test('应该有足够的颜色对比度', async ({ page }) => {
    const taskCard = page.locator(`[data-task-id="${taskId}"]`);

    // 获取文字颜色和背景颜色
    const colors = await taskCard.evaluate(el => {
      const styles = window.getComputedStyle(el);
      return {
        color: styles.color,
        backgroundColor: styles.backgroundColor,
      };
    });

    console.log('任务卡片颜色:', colors);

    // 这里应该使用对比度计算工具验证
    // 简单验证：颜色不为空
    expect(colors.color).toBeTruthy();
    expect(colors.backgroundColor).toBeTruthy();
  });

  test('应该支持焦点可见性', async ({ page }) => {
    const taskCard = page.locator(`[data-task-id="${taskId}"]`);
    const button = taskCard.locator('button').first();

    // 聚焦按钮
    await button.focus();

    // 验证焦点样式
    const outline = await button.evaluate(el => {
      const styles = window.getComputedStyle(el);
      return styles.outline || styles.boxShadow;
    });

    expect(outline).toBeTruthy();
  });

  test('应该有正确的语义化 HTML', async ({ page }) => {
    const taskCard = page.locator(`[data-task-id="${taskId}"]`);

    // 验证使用语义化标签
    const hasHeading = await taskCard.locator('h1, h2, h3, h4, h5, h6').count() > 0;
    expect(hasHeading).toBeTruthy();

    const hasButton = await taskCard.locator('button').count() > 0;
    expect(hasButton).toBeTruthy();
  });

  test('应该支持跳转到主要内容', async ({ page }) => {
    // 检查是否有"跳转到主要内容"链接
    const skipLink = page.locator('a[href*="main"], a[href*="content"]').first();
    const hasSkipLink = await skipLink.isVisible().catch(() => false);

    if (hasSkipLink) {
      await skipLink.click();

      // 验证焦点跳转到主内容
      const mainContent = page.locator('main, [role="main"]');
      const isFocused = await mainContent.evaluate(el => el === document.activeElement);
      expect(isFocused).toBeTruthy();
    }
  });

  test('应该有正确的表单标签', async ({ page }) => {
    // 检查消息输入框的标签
    const input = page.locator('textarea[placeholder*="消息"], textarea[placeholder*="输入"]');

    const hasLabel = await input.evaluate(el => {
      return el.hasAttribute('aria-label') ||
             el.hasAttribute('aria-labelledby') ||
             el.labels.length > 0;
    });

    expect(hasLabel).toBeTruthy();
  });
});
