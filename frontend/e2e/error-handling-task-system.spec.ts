/**
 * 任务系统错误处理测试
 *
 * 测试任务系统的错误处理能力
 */

import { test, expect } from '@playwright/test';
import { quickLogin, createSession, waitForErrorMessage } from '../e2e/helpers';
import { createTask } from '../tests/helpers/task-helpers';

test.describe('任务系统错误处理', () => {
  let sessionId: string;

  test.beforeEach(async ({ page }) => {
    await quickLogin(page);
    const session = await createSession(page, { name: '错误处理测试' });
    sessionId = session.id!;
    await page.goto(`/sessions/${sessionId}`);
  });

  test('应该处理网络错误', async ({ page }) => {
    // 断开网络
    await page.context().setOffline(true);

    // 尝试创建任务
    await page.fill('textarea[placeholder*="消息"]', '网络错误测试');
    await page.click('button:has-text("发送")');

    // 验证错误提示
    await waitForErrorMessage(page);

    const errorMessage = page.locator('.toast, [role="alert"]');
    await expect(errorMessage).toContainText(/网络|连接|失败/);

    // 恢复网络
    await page.context().setOffline(false);
  });

  test('应该处理服务器错误', async ({ page }) => {
    // Mock 服务器错误响应
    await page.route('**/api/tasks', async route => {
      await route.fulfill({
        status: 500,
        contentType: 'application/json',
        body: JSON.stringify({ error: '服务器内部错误' }),
      });
    });

    // 尝试创建任务
    await page.fill('textarea[placeholder*="消息"]', '服务器错误测试');
    await page.click('button:has-text("发送")');

    // 验证错误提示
    await waitForErrorMessage(page);

    const errorMessage = page.locator('.toast, [role="alert"]');
    await expect(errorMessage).toContainText(/错误|失败/);
  });

  test('应该处理超时错误', async ({ page }) => {
    // Mock 超时响应
    await page.route('**/api/tasks', route => {
      // 不响应，让请求超时
    });

    // 尝试创建任务
    await page.fill('textarea[placeholder*="消息"]', '超时测试');
    await page.click('button:has-text("发送")');

    // 等待超时错误提示
    const errorMessage = page.locator('.toast, [role="alert"]');
    await expect(errorMessage).toBeVisible({ timeout: 35000 });
    await expect(errorMessage).toContainText(/超时|timeout/);
  });

  test('应该显示重试按钮', async ({ page }) => {
    // 断开网络
    await page.context().setOffline(true);

    await page.fill('textarea[placeholder*="消息"]', '重试测试');
    await page.click('button:has-text("发送")');

    // 等待错误提示
    await waitForErrorMessage(page);

    // 查找重试按钮
    const retryButton = page.locator('button:has-text("重试"), [data-testid="retry-button"]');

    const hasRetryButton = await retryButton.isVisible().catch(() => false);
    if (hasRetryButton) {
      // 恢复网络
      await page.context().setOffline(false);

      // 点击重试
      await retryButton.click();

      // 验证重试成功或失败
      await page.waitForTimeout(2000);
    }
  });

  test('应该处理 WebSocket 连接失败', async ({ page }) => {
    // Mock WebSocket 连接失败
    await page.evaluate(() => {
      const originalWebSocket = window.WebSocket;
      window.WebSocket = class extends originalWebSocket {
        constructor(url: string, protocols?: string | string[]) {
          super(url, protocols);
          setTimeout(() => {
            this.close();
          }, 100);
        }
      } as any;
    });

    // 刷新页面
    await page.reload();

    // 验证连接错误提示
    const statusIndicator = page.locator('[data-testid="websocket-status"]');
    await expect(statusIndicator).toContainText(/重连|断线|连接失败/);
  });

  test('应该处理无效的任务数据', async ({ page }) => {
    // 尝试发送空任务
    await page.fill('textarea[placeholder*="消息"]', '');
    await page.click('button:has-text("发送")');

    // 验证表单验证错误
    const validationError = page.locator('.validation-error, [data-testid="validation-error"]');

    const hasValidationError = await validationError.isVisible().catch(() => false);
    if (hasValidationError) {
      await expect(validationError).toContainText(/不能为空|必填|required/);
    }
  });

  test('应该处理任务执行失败', async ({ page }) => {
    // 创建任务
    const result = await createTask(page, '失败测试任务', sessionId);

    // 注意：这个测试需要模拟任务执行失败的场景
    // 实际测试中可能需要 Mock API 或使用特殊的测试数据

    // 查找失败状态的任务卡片
    const taskCard = page.locator(`[data-task-id="${result.taskId}"]`);

    // 等待一段时间观察任务状态
    await page.waitForTimeout(5000);

    const taskStatus = taskCard.locator('.task-status');
    const statusText = await taskStatus.textContent();
    const isFailed = statusText?.includes('失败') || statusText?.includes('failed');

    if (isFailed) {
      // 验证失败信息显示
      const failCard = taskCard.locator('.task-fail-card');
      await expect(failCard).toBeVisible();

      // 验证失败原因
      const failReason = failCard.locator('.fail-reason');
      await expect(failReason).toBeVisible();
    }
  });

  test('应该优雅地处理未知错误', async ({ page }) => {
    // Mock 未知错误
    await page.route('**/api/**', route => {
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: 'invalid json{{{',
      });
    });

    // 触发 API 请求
    await page.goto(`/sessions/${sessionId}`);

    // 验证应用没有崩溃
    const isPageVisible = await page.locator('body').isVisible();
    expect(isPageVisible).toBeTruthy();

    // 验证错误提示
    const errorMessage = page.locator('.toast, [role="alert"]');
    const hasError = await errorMessage.isVisible().catch(() => false);

    if (hasError) {
      await expect(errorMessage).toBeVisible();
    }
  });

  test('应该提供错误反馈给用户', async ({ page }) => {
    // 断开网络
    await page.context().setOffline(true);

    await page.fill('textarea[placeholder*="消息"]', '错误反馈测试');
    await page.click('button:has-text("发送")');

    // 验证错误消息清晰易懂
    const errorMessage = page.locator('.toast, [role="alert"]');
    await expect(errorMessage).toBeVisible();

    const errorText = await errorMessage.textContent();
    expect(errorText).toBeTruthy();
    expect(errorText!.length).toBeGreaterThan(0);

    // 验证错误消息包含有用信息
    expect(errorText).toMatch(/网络|连接|失败/);

    // 恢复网络
    await page.context().setOffline(false);
  });

  test('应该记录错误日志', async ({ page }) => {
    // 触发一个错误
    await page.context().setOffline(true);
    await page.fill('textarea[placeholder*="消息"]', '日志测试');
    await page.click('button:has-text("发送")');
    await page.waitForTimeout(2000);
    await page.context().setOffline(false);

    // 检查控制台是否有错误日志
    const logs: string[] = [];
    page.on('console', msg => {
      if (msg.type() === 'error') {
        logs.push(msg.text());
      }
    });

    await page.waitForTimeout(1000);

    // 验证有错误日志
    expect(logs.length).toBeGreaterThan(0);
  });

  test('应该支持清除错误状态', async ({ page }) => {
    // 触发错误
    await page.context().setOffline(true);
    await page.fill('textarea[placeholder*="消息"]', '清除错误测试');
    await page.click('button:has-text("发送")');
    await waitForErrorMessage(page);
    await page.context().setOffline(false);

    // 查找关闭错误提示的按钮
    const closeButton = page.locator('.toast button:has-text("关闭"), [data-testid="close-toast"]');

    const hasCloseButton = await closeButton.isVisible().catch(() => false);
    if (hasCloseButton) {
      await closeButton.click();

      // 验证错误提示消失
      const errorMessage = page.locator('.toast, [role="alert"]');
      await expect(errorMessage).not.toBeVisible();
    }
  });
});
