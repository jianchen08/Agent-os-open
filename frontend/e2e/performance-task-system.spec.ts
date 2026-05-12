/**
 * 任务系统性能测试
 *
 * 测试任务系统的性能表现
 */

import { test, expect } from '@playwright/test';
import { quickLogin, createSession } from '../e2e/helpers';
import { createTask, measureTaskCardRenderTime, getPagePerformanceMetrics } from '../tests/helpers/task-helpers';

test.describe('任务系统性能测试', () => {
  let sessionId: string;

  test.beforeEach(async ({ page }) => {
    await quickLogin(page);
    const session = await createSession(page, { name: '性能测试' });
    sessionId = session.id!;
  });

  test('应该在合理时间内加载会话页面', async ({ page }) => {
    const startTime = Date.now();

    await page.goto(`/sessions/${sessionId}`, { waitUntil: 'networkidle' });

    const loadTime = Date.now() - startTime;

    // 页面加载时间应该小于 3 秒
    expect(loadTime).toBeLessThan(3000);

    // 获取详细性能指标
    const metrics = await getPagePerformanceMetrics(page);

    console.log('页面性能指标:', metrics);

    // FCP 应该小于 1.5 秒
    expect(metrics.firstContentfulPaint).toBeLessThan(1500);

    // DOM 加载应该小于 1 秒
    expect(metrics.domContentLoaded).toBeLessThan(1000);
  });

  test('应该在合理时间内渲染任务卡片', async ({ page }) => {
    await page.goto(`/sessions/${sessionId}`);

    // 创建任务并测量渲染时间
    const result = await createTask(page, '性能测试任务', sessionId);
    const renderTime = await measureTaskCardRenderTime(page, result.taskId!);

    console.log('任务卡片渲染时间:', renderTime, 'ms');

    // 渲染时间应该小于 500ms
    expect(renderTime).toBeGreaterThan(0);
    expect(renderTime).toBeLessThan(500);
  });

  test('应该支持大量消息流畅滚动', async ({ page }) => {
    await page.goto(`/sessions/${sessionId}`);

    // 创建多个任务
    const taskCount = 10;
    for (let i = 0; i < taskCount; i++) {
      await createTask(page, `性能测试任务 #${i + 1}`, sessionId);
      await page.waitForTimeout(100);
    }

    // 测量滚动性能
    const startTime = Date.now();

    await page.evaluate(() => {
      const messageList = document.querySelector('.message-list, [data-testid="message-list"]');
      if (messageList) {
        messageList.scrollTop = messageList.scrollHeight;
      }
    });

    const scrollTime = Date.now() - startTime;

    console.log('滚动时间:', scrollTime, 'ms');

    // 滚动应该很快
    expect(scrollTime).toBeLessThan(100);
  });

  test('应该在切换主题时保持流畅', async ({ page }) => {
    await page.goto(`/sessions/${sessionId}`);

    // 创建任务
    await createTask(page, '主题切换测试', sessionId);

    // 测量主题切换时间
    const startTime = Date.now();

    const themeButton = page.locator('[data-testid="theme-toggle"]');
    await themeButton.click();

    // 等待主题切换完成
    await page.waitForTimeout(500);

    const switchTime = Date.now() - startTime;

    console.log('主题切换时间:', switchTime, 'ms');

    // 主题切换应该在 1 秒内完成
    expect(switchTime).toBeLessThan(1000);
  });

  test('应该快速响应交互操作', async ({ page }) => {
    await page.goto(`/sessions/${sessionId}`);

    const result = await createTask(page, '交互响应测试', sessionId);
    const taskCard = page.locator(`[data-task-id="${result.taskId}"]`);

    // 测量展开/折叠时间
    const expandButton = taskCard.locator('button:has-text("验收标准")');

    const startTime = Date.now();
    await expandButton.click();

    await page.waitForFunction(
      (taskId) => {
        const card = document.querySelector(`[data-task-id="${taskId}"]`);
        return card && card.querySelector('.ac-list')?.getAttribute('data-visible') === 'true';
      },
      result.taskId,
      { timeout: 1000 }
    );

    const responseTime = Date.now() - startTime;

    console.log('交互响应时间:', responseTime, 'ms');

    // 响应时间应该小于 200ms
    expect(responseTime).toBeLessThan(200);
  });

  test('应该在页面空闲时释放资源', async ({ page }) => {
    await page.goto(`/sessions/${sessionId}`);

    // 创建多个任务
    for (let i = 0; i < 5; i++) {
      await createTask(page, `资源测试任务 #${i + 1}`, sessionId);
    }

    // 等待页面稳定
    await page.waitForTimeout(2000);

    // 获取内存使用情况
    const metrics = await page.metrics();

    console.log('页面性能指标:', {
      JSHeapUsedSize: metrics.JSHeapUsedSize / 1024 / 1024, // MB
      JSHeapTotalSize: metrics.JSHeapTotalSize / 1024 / 1024, // MB
    });

    // 内存使用应该在合理范围内（小于 200MB）
    expect(metrics.JSHeapUsedSize).toBeLessThan(200 * 1024 * 1024);
  });
});
