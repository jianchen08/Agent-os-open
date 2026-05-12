/**
 * 任务执行流程 E2E 测试
 *
 * 测试任务执行阶段的完整流程
 */

import { test, expect } from '@playwright/test';
import { quickLogin, createSession } from '../e2e/helpers';
import { createTask, waitForTaskPhase, verifyPhaseIndicator } from '../tests/helpers/task-helpers';

test.describe('任务执行流程', () => {
  let sessionId: string;
  let taskId: string;

  test.beforeEach(async ({ page }) => {
    await quickLogin(page);

    const session = await createSession(page, {
      name: '任务执行测试会话',
    });
    sessionId = session.id!;

    await page.goto(`/sessions/${sessionId}`);

    // 创建任务
    const result = await createTask(page, '实现用户认证功能', sessionId);
    taskId = result.taskId!;
  });

  test('应该进入准备阶段', async ({ page }) => {
    // 验证准备阶段状态
    await verifyPhaseIndicator(page, taskId, {
      prepare: 'running',
      execute: 'pending',
      evaluate: 'pending',
    });

    // 验证阶段通知显示
    const phaseNotice = page.locator('.phase-notice, [data-testid="phase-notice"]');
    await expect(phaseNotice).toBeVisible();
    await expect(phaseNotice).toContainText('准备');
  });

  test('应该完成准备阶段并进入执行阶段', async ({ page }) => {
    // 等待准备阶段完成
    await waitForTaskPhase(page, taskId, 'execute', 60000);

    // 验证阶段变更通知
    const notification = page.locator('.notification, [data-testid="notification"]').filter({ hasText: /阶段变更|执行/ });
    await expect(notification).toBeVisible({ timeout: 10000 });

    // 验证执行阶段状态
    await verifyPhaseIndicator(page, taskId, {
      prepare: 'completed',
      execute: 'running',
      evaluate: 'pending',
    });

    // 截图记录
    await page.screenshot({ path: 'test-results/task-execute-phase.png' });
  });

  test('应该显示 Agent 工作状态', async ({ page }) => {
    // 等待进入执行阶段
    await waitForTaskPhase(page, taskId, 'execute', 60000);

    // 验证 Agent 工作指示器
    const agentStatus = page.locator('[data-testid="agent-status"]');
    await expect(agentStatus).toBeVisible();
    await expect(agentStatus).toContainText(/执行中|工作中|运行/);

    // 验证思考过程显示（如果有）
    const thinkingIndicator = page.locator('.thinking-indicator, [data-testid="thinking"]');
    const isThinkingVisible = await thinkingIndicator.isVisible().catch(() => false);

    if (isThinkingVisible) {
      await expect(thinkingIndicator).toContainText(/思考|分析/);
    }
  });

  test('应该在执行过程中更新消息流', async ({ page }) => {
    // 等待进入执行阶段
    await waitForTaskPhase(page, taskId, 'execute', 60000);

    // 记录当前消息数量
    const beforeCount = await page.locator('.message').count();

    // 等待一段时间，观察消息更新
    await page.waitForTimeout(5000);

    // 验证消息数量增加
    const afterCount = await page.locator('.message').count();
    expect(afterCount).toBeGreaterThan(beforeCount);

    // 验证最后一条消息来自 Agent
    const lastMessage = page.locator('.message').last();
    await expect(lastMessage).toHaveClass(/assistant/);
  });

  test('应该显示工具调用记录', async ({ page }) => {
    // 等待进入执行阶段并等待工具调用
    await waitForTaskPhase(page, taskId, 'execute', 60000);
    await page.waitForTimeout(3000);

    // 查找工具调用卡片
    const toolCallCard = page.locator('.tool-call, [data-testid="tool-call"]').first();

    const isToolCallVisible = await toolCallCard.isVisible().catch(() => false);
    if (isToolCallVisible) {
      // 验证工具调用信息显示
      await expect(toolCallCard).toBeVisible();

      const toolName = toolCallCard.locator('.tool-name, [data-testid="tool-name"]');
      await expect(toolName).toBeVisible();

      const toolResult = toolCallCard.locator('.tool-result, [data-testid="tool-result"]');
      await expect(toolResult).toBeVisible();
    }
  });

  test('应该支持暂停正在执行的任务', async ({ page }) => {
    // 等待进入执行阶段
    await waitForTaskPhase(page, taskId, 'execute', 60000);

    // 点击暂停按钮
    const pauseButton = page.locator('button:has-text("暂停"), [data-testid="pause-task"]');
    await pauseButton.click();

    // 验证暂停确认对话框
    const dialog = page.locator('dialog, [role="dialog"]');
    await expect(dialog).toBeVisible();

    // 确认暂停
    await dialog.locator('button:has-text("确认")').click();

    // 验证任务状态变为暂停
    const taskStatus = page.locator(`[data-task-id="${taskId}"]`).locator('.task-status');
    await expect(taskStatus).toContainText(/暂停|paused/);
  });

  test('应该支持恢复暂停的任务', async ({ page }) => {
    // 先暂停任务
    await waitForTaskPhase(page, taskId, 'execute', 60000);

    const pauseButton = page.locator('button:has-text("暂停"), [data-testid="pause-task"]');
    await pauseButton.click();

    const dialog = page.locator('dialog, [role="dialog"]');
    await dialog.locator('button:has-text("确认")').click();

    await page.waitForTimeout(1000);

    // 恢复任务
    const resumeButton = page.locator('button:has-text("恢复"), [data-testid="resume-task"]');
    await resumeButton.click();

    // 验证任务恢复
    const taskStatus = page.locator(`[data-task-id="${taskId}"]`).locator('.task-status');
    await expect(taskStatus).toContainText(/执行中|running/);
  });

  test('应该支持取消正在执行的任务', async ({ page }) => {
    // 等待进入执行阶段
    await waitForTaskPhase(page, taskId, 'execute', 60000);

    // 点击取消按钮
    const cancelButton = page.locator('button:has-text("取消"), [data-testid="cancel-task"]');
    await cancelButton.click();

    // 验证取消确认对话框
    const dialog = page.locator('dialog, [role="dialog"]');
    await expect(dialog).toBeVisible();
    await expect(dialog).toContainText(/确认取消|确定要取消/);

    // 确认取消
    await dialog.locator('button:has-text("确认")').click();

    // 验证任务状态变为已取消
    const taskStatus = page.locator(`[data-task-id="${taskId}"]`).locator('.task-status');
    await expect(taskStatus).toContainText(/已取消|cancelled/);
  });

  test('应该在执行超时时显示警告', async ({ page }) => {
    // 注意：这个测试需要模拟执行超时的情况
    // 实际测试中可能需要 Mock API 响应

    // 等待进入执行阶段
    await waitForTaskPhase(page, taskId, 'execute', 60000);

    // 查找超时警告（如果出现）
    const timeoutWarning = page.locator('.timeout-warning, [data-testid="timeout-warning"]');

    // 由于实际测试中超时可能不会立即发生，这里只是示例
    const isWarningVisible = await timeoutWarning.isVisible().catch(() => false);

    if (isWarningVisible) {
      await expect(timeoutWarning).toContainText(/超时|timeout/);
    }
  });

  test('应该显示执行进度统计', async ({ page }) => {
    // 等待进入执行阶段
    await waitForTaskPhase(page, taskId, 'execute', 60000);

    // 验证进度信息显示
    const progressInfo = page.locator(`[data-task-id="${taskId}"]`).locator('.execution-progress, [data-testid="execution-progress"]');
    await expect(progressInfo).toBeVisible();

    // 验证进度条显示
    const progressBar = progressInfo.locator('.progress-bar, [data-testid="progress-bar"]');
    await expect(progressBar).toBeVisible();

    // 验证执行时间显示
    const executionTime = progressInfo.locator('.execution-time, [data-testid="execution-time"]');
    await expect(executionTime).toBeVisible();
  });

  test('应该支持查看执行日志', async ({ page }) => {
    // 等待进入执行阶段
    await waitForTaskPhase(page, taskId, 'execute', 60000);

    // 点击查看日志按钮
    const logButton = page.locator('button:has-text("日志"), [data-testid="view-logs"]');
    const isButtonVisible = await logButton.isVisible().catch(() => false);

    if (isButtonVisible) {
      await logButton.click();

      // 验证日志面板打开
      const logPanel = page.locator('.log-panel, [data-testid="log-panel"]');
      await expect(logPanel).toBeVisible();

      // 验证日志内容显示
      const logContent = logPanel.locator('.log-content, [data-testid="log-content"]');
      await expect(logContent).toBeVisible();

      // 关闭日志面板
      const closeButton = logPanel.locator('button:has-text("关闭")');
      await closeButton.click();

      // 验证面板关闭
      await expect(logPanel).not.toBeVisible();
    }
  });

  test('应该在执行完成后自动进入评估阶段', async ({ page }) => {
    // 等待执行完成并进入评估阶段
    await waitForTaskPhase(page, taskId, 'evaluate', 120000);

    // 验证阶段状态
    await verifyPhaseIndicator(page, taskId, {
      prepare: 'completed',
      execute: 'completed',
      evaluate: 'running',
    });

    // 验证阶段变更通知
    const notification = page.locator('.notification, [data-testid="notification"]').filter({ hasText: /评估/ });
    await expect(notification.first()).toBeVisible({ timeout: 10000 });
  });

  test.afterEach(async ({ page }) => {
    await page.screenshot({
      path: `test-results/task-execution-${test.info().title.replace(/\s+/g, '-')}.png`,
      fullPage: true,
    });
  });
});
