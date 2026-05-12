/**
 * 任务卡片交互测试
 *
 * 测试任务卡片的各种交互功能
 */

import { test, expect } from '@playwright/test';
import { quickLogin, createSession } from '../e2e/helpers';
import { createTask } from '../tests/helpers/task-helpers';

test.describe('任务卡片交互', () => {
  let sessionId: string;
  let taskId: string;

  test.beforeEach(async ({ page }) => {
    await quickLogin(page);
    const session = await createSession(page, { name: '任务交互测试' });
    sessionId = session.id!;
    await page.goto(`/sessions/${sessionId}`);

    const result = await createTask(page, '测试任务', sessionId);
    taskId = result.taskId!;
  });

  test('应该展开任务详情', async ({ page }) => {
    const taskCard = page.locator(`[data-task-id="${taskId}"]`);
    const expandButton = taskCard.locator('button:has-text("详情"), [data-testid="expand-detail"]');

    await expandButton.click();

    const detailPanel = taskCard.locator('.task-detail-panel, [data-testid="task-detail-panel"]');
    await expect(detailPanel).toBeVisible();
  });

  test('应该折叠任务详情', async ({ page }) => {
    const taskCard = page.locator(`[data-task-id="${taskId}"]`);

    // 先展开
    await taskCard.locator('button:has-text("详情")').click();

    // 再折叠
    const collapseButton = taskCard.locator('button:has-text("收起"), [data-testid="collapse-detail"]');
    await collapseButton.click();

    const detailPanel = taskCard.locator('.task-detail-panel');
    await expect(detailPanel).not.toBeVisible();
  });

  test('应该展开和折叠 AC 列表', async ({ page }) => {
    const taskCard = page.locator(`[data-task-id="${taskId}"]`);
    const acToggleButton = taskCard.locator('button:has-text("验收标准"), [data-testid="toggle-ac"]');

    // 展开
    await acToggleButton.click();
    await expect(taskCard.locator('.ac-list')).toBeVisible();

    // 折叠
    await acToggleButton.click();
    await expect(taskCard.locator('.ac-list')).not.toBeVisible();
  });

  test('应该显示 AC 详情', async ({ page }) => {
    const taskCard = page.locator(`[data-task-id="${taskId}"]`);

    // 展开 AC 列表
    await taskCard.locator('button:has-text("验收标准")').click();

    // 点击第一个 AC
    const firstAC = taskCard.locator('.ac-item').first();
    await firstAC.click();

    // 验证详情显示
    const acDetail = firstAC.locator('.ac-detail');
    await expect(acDetail).toBeVisible();
  });

  test('应该支持复制任务目标', async ({ page }) => {
    const taskCard = page.locator(`[data-task-id="${taskId}"]`);
    const copyButton = taskCard.locator('button:has-text("复制"), [data-testid="copy-goal"]');

    await copyButton.click();

    // 验证复制提示
    const toast = page.locator('.toast, [role="alert"]');
    await expect(toast).toContainText(/复制|已复制/);
  });

  test('应该支持跳转到执行图节点', async ({ page }) => {
    const taskCard = page.locator(`[data-task-id="${taskId}"]`);
    const jumpButton = taskCard.locator('button:has-text("执行图"), [data-testid="jump-to-graph"]');

    const isButtonVisible = await jumpButton.isVisible().catch(() => false);
    if (isButtonVisible) {
      await jumpButton.click();

      // 验证执行图节点高亮
      const graphNode = page.locator('.execution-node.highlighted, [data-highlighted="true"]');
      await expect(graphNode.first()).toBeVisible();
    }
  });
});
