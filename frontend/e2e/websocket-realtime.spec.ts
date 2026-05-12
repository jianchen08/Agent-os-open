/**
 * WebSocket 实时更新测试
 *
 * 测试 WebSocket 事件和实时更新功能
 */

import { test, expect } from '@playwright/test';
import { quickLogin, createSession, verifyWebSocketStatus } from './helpers';
import { createTask } from '../tests/helpers/task-helpers';

test.describe('WebSocket 实时更新', () => {
  let sessionId: string;

  test.beforeEach(async ({ page }) => {
    await quickLogin(page);
    const session = await createSession(page, { name: 'WebSocket 测试' });
    sessionId = session.id!;
    await page.goto(`/sessions/${sessionId}`);
  });

  test('应该显示 WebSocket 连接状态', async ({ page }) => {
    // 验证连接状态指示器显示
    await verifyWebSocketStatus(page, 'connected');

    const statusIndicator = page.locator('[data-testid="websocket-status"]');
    await expect(statusIndicator).toBeVisible();
    await expect(statusIndicator).toContainText(/已连接|在线/);
  });

  test('应该在任务创建时实时显示', async ({ page }) => {
    // 监听 WebSocket 消息
    const wsMessages: string[] = [];
    page.on('websocket', ws => {
      ws.on('framereceived', frame => {
        wsMessages.push(frame.payload.toString());
      });
    });

    // 创建任务
    await createTask(page, '实时测试任务', sessionId);

    // 等待一下让 WebSocket 消息到达
    await page.waitForTimeout(1000);

    // 验证收到任务创建事件
    const taskCreatedEvent = wsMessages.find(msg => msg.includes('task_created'));
    expect(taskCreatedEvent).toBeTruthy();
  });

  test('应该在阶段变更时实时更新', async ({ page }) => {
    const result = await createTask(page, '阶段变更测试', sessionId);
    const taskId = result.taskId!;

    // 等待阶段变更
    await page.waitForFunction(
      ({ tid }) => {
        const taskCard = document.querySelector(`[data-task-id="${tid}"]`);
        if (!taskCard) return false;

        const phase = taskCard.getAttribute('data-current-phase');
        return phase !== 'prepare';
      },
      { tid: taskId },
      { timeout: 60000 }
    );

    // 验证阶段指示器实时更新
    const taskCard = page.locator(`[data-task-id="${taskId}"]`);
    const phaseIndicator = taskCard.locator('.phase-indicator');

    await expect(phaseIndicator).toBeVisible();
  });

  test('应该在 AC 评估时实时更新状态', async ({ page }) => {
    const result = await createTask(page, 'AC 更新测试', sessionId);
    const taskId = result.taskId!;

    // 展开 AC 列表
    const taskCard = page.locator(`[data-task-id="${taskId}"]`);
    await taskCard.locator('button:has-text("验收标准")').click();

    // 等待 AC 评估开始
    await page.waitForFunction(
      ({ tid }) => {
        const acItem = document.querySelector(`[data-task-id="${tid}"] .ac-item`);
        if (!acItem) return false;

        const status = acItem.getAttribute('data-status');
        return status === 'evaluating' || status === 'passed' || status === 'failed';
      },
      { tid: taskId },
      { timeout: 90000 }
    );

    // 验证 AC 状态更新
    const acStatus = taskCard.locator('.ac-item').first().locator('.ac-status');
    const status = await acStatus.getAttribute('data-status');
    expect(['evaluating', 'passed', 'failed']).toContain(status);
  });

  test('应该在断线时显示重连状态', async ({ page }) => {
    // 模拟断线
    await page.context().setOffline(true);

    // 等待状态更新
    await page.waitForTimeout(2000);

    // 验证重连状态显示
    const statusIndicator = page.locator('[data-testid="websocket-status"]');
    await expect(statusIndicator).toContainText(/重连|断线|离线/);

    // 恢复网络
    await page.context().setOffline(false);

    // 等待重连
    await page.waitForTimeout(3000);

    // 验证恢复连接
    await expect(statusIndicator).toContainText(/已连接|在线/);
  });

  test('应该在重连成功后恢复状态', async ({ page }) => {
    // 断线
    await page.context().setOffline(true);
    await page.waitForTimeout(2000);

    // 创建任务（应该失败或排队）
    await page.fill('textarea[placeholder*="消息"]', '离线测试任务');

    // 恢复网络
    await page.context().setOffline(false);

    // 等待重连
    await verifyWebSocketStatus(page, 'connected');

    // 验证任务发送成功
    await page.click('button:has-text("发送")');
    await expect(page.locator('[data-testid="task-card"]')).toBeVisible({ timeout: 10000 });
  });
});
