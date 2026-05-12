/**
 * 任务评估流程 E2E 测试
 *
 * 测试任务评估阶段的完整流程
 */

import { test, expect } from '@playwright/test';
import { quickLogin, createSession } from '../e2e/helpers';
import { createTask, waitForTaskPhase, waitForACEvaluation, verifyACList, verifyTaskCompletion } from '../tests/helpers/task-helpers';

test.describe('任务评估流程', () => {
  let sessionId: string;
  let taskId: string;

  test.beforeEach(async ({ page }) => {
    await quickLogin(page);

    const session = await createSession(page, {
      name: '任务评估测试会话',
    });
    sessionId = session.id!;

    await page.goto(`/sessions/${sessionId}`);

    // 创建任务
    const result = await createTask(page, '实现用户认证功能', sessionId);
    taskId = result.taskId!;

    // 等待任务进入评估阶段
    await waitForTaskPhase(page, taskId, 'evaluate', 120000);
  });

  test('应该进入评估阶段', async ({ page }) => {
    // 验证评估阶段状态
    const taskCard = page.locator(`[data-task-id="${taskId}"]`);
    const phaseIndicator = taskCard.locator('.phase-indicator, [data-testid="phase-indicator"]');

    await expect(phaseIndicator).toBeVisible();

    const evaluatePhase = phaseIndicator.locator('[data-phase="evaluate"]');
    await expect(evaluatePhase).toHaveAttribute('data-status', 'running');
  });

  test('应该开始 AC 评估', async ({ page }) => {
    // 验证 AC 列表显示
    await verifyACList(page, taskId, 3);

    // 验证第一个 AC 开始评估
    const taskCard = page.locator(`[data-task-id="${taskId}"]`);
    const firstAC = taskCard.locator('.ac-item, [data-testid="ac-item"]').first();

    const acStatus = firstAC.locator('.ac-status, [data-testid="ac-status"]');
    await expect(acStatus).toHaveAttribute('data-status', 'evaluating');

    // 验证评估动画或指示器
    const evaluatingIndicator = firstAC.locator('.evaluating-indicator, [data-testid="evaluating"]');
    await expect(evaluatingIndicator).toBeVisible();
  });

  test('应该逐个评估 AC', async ({ page }) => {
    const taskCard = page.locator(`[data-task-id="${taskId}"]`);
    const acItems = taskCard.locator('.ac-item, [data-testid="ac-item"]');
    const acCount = await acItems.count();

    // 验证所有 AC 逐个开始评估
    for (let i = 0; i < acCount; i++) {
      const acItem = acItems.nth(i);
      const acId = await acItem.getAttribute('data-ac-id');

      if (acId) {
        // 等待 AC 评估完成
        await waitForACEvaluation(page, taskId, acId, 'passed', 30000);

        // 验证 AC 状态更新
        const acStatus = acItem.locator('.ac-status, [data-testid="ac-status"]');
        await expect(acStatus).toHaveAttribute('data-status', 'passed');
      }
    }
  });

  test('应该显示 AC 评估结果', async ({ page }) => {
    const taskCard = page.locator(`[data-task-id="${taskId}"]`);
    const firstAC = taskCard.locator('.ac-item, [data-testid="ac-item"]').first();
    const acId = await firstAC.getAttribute('data-ac-id')!;

    // 等待 AC 评估完成
    await waitForACEvaluation(page, taskId, acId, 'passed', 30000);

    // 验证评估结果显示
    const acResult = firstAC.locator('.ac-result, [data-testid="ac-result"]');
    await expect(acResult).toBeVisible();

    // 验证通过/失败图标
    const resultIcon = acResult.locator('.result-icon, [data-testid="result-icon"]');
    await expect(resultIcon).toHaveClass(/passed|success/);

    // 点击展开详细结果
    await firstAC.click();

    // 验证详细结果显示
    const detailResult = firstAC.locator('.ac-detail-result, [data-testid="ac-detail-result"]');
    await expect(detailResult).toBeVisible();
  });

  test('应该处理 AC 评估失败的情况', async ({ page }) => {
    // 注意：这个测试需要 Mock 一个会失败的 AC
    // 实际测试中可能需要特殊的测试数据

    const taskCard = page.locator(`[data-task-id="${taskId}"]`);
    const acItems = taskCard.locator('.ac-item, [data-testid="ac-item"]');

    // 查找可能失败的 AC（示例）
    for (let i = 0; i < await acItems.count(); i++) {
      const acItem = acItems.nth(i);
      const acId = await acItem.getAttribute('data-ac-id');

      if (acId) {
        const acStatus = acItem.locator('.ac-status, [data-testid="ac-status"]');
        const currentStatus = await acStatus.getAttribute('data-status');

        if (currentStatus === 'failed') {
          // 验证失败样式
          await expect(acStatus).toHaveAttribute('data-status', 'failed');

          // 验证失败原因显示
          const failReason = acItem.locator('.fail-reason, [data-testid="fail-reason"]');
          await expect(failReason).toBeVisible();

          // 验证重试按钮显示
          const retryButton = acItem.locator('button:has-text("重试"), [data-testid="retry-ac"]');
          await expect(retryButton).toBeVisible();

          break;
        }
      }
    }
  });

  test('应该在所有 AC 评估完成后显示任务完成', async ({ page }) => {
    // 等待所有 AC 评估完成
    const taskCard = page.locator(`[data-task-id="${taskId}"]`);
    const acItems = taskCard.locator('.ac-item, [data-testid="ac-item"]');

    // 等待所有 AC 都完成评估
    await page.waitForFunction(
      () => {
        const items = document.querySelectorAll('.ac-item, [data-testid="ac-item"]');
        return Array.from(items).every(item => {
          const status = item.getAttribute('data-status');
          return status === 'passed' || status === 'failed';
        });
      },
      { timeout: 120000 }
    );

    // 验证任务完成卡片显示
    await verifyTaskCompletion(page, taskId, {
      success: true,
    });
  });

  test('应该显示任务执行总结', async ({ page }) => {
    // 等待任务完成
    const resultCard = await verifyTaskCompletion(page, taskId);

    // 验证执行总结显示
    const summary = resultCard.locator('.task-summary, [data-testid="task-summary"]');
    await expect(summary).toBeVisible();

    // 验证总结内容不为空
    const summaryText = await summary.textContent();
    expect(summaryText?.trim()).toBeTruthy();
    expect(summaryText!.length).toBeGreaterThan(10);
  });

  test('应该显示 AC 通过率统计', async ({ page }) => {
    // 等待任务完成
    const resultCard = await verifyTaskCompletion(page, taskId);

    // 验证通过率显示
    const passRate = resultCard.locator('.pass-rate, [data-testid="pass-rate"]');
    await expect(passRate).toBeVisible();

    // 验证格式（例如："3/3 通过" 或 "100%"）
    const passRateText = await passRate.textContent();
    expect(passRateText).toMatch(/\d+\/\d+|%\d+/);
  });

  test('应该显示执行时间统计', async ({ page }) => {
    // 等待任务完成
    const resultCard = await verifyTaskCompletion(page, taskId);

    // 验证执行时间显示
    const executionTime = resultCard.locator('.execution-time, [data-testid="execution-time"]');
    await expect(executionTime).toBeVisible();

    // 验证时间格式
    const timeText = await executionTime.textContent();
    expect(timeText).toMatch(/\d+秒|\d+分钟|秒|分钟/);
  });

  test('应该支持重新评估失败的 AC', async ({ page }) => {
    // 注意：这个测试需要有失败 AC 的场景
    // 这里先提供测试框架

    const taskCard = page.locator(`[data-task-id="${taskId}"]`);
    const acItems = taskCard.locator('.ac-item, [data-testid="ac-item"]');

    // 查找失败的 AC
    for (let i = 0; i < await acItems.count(); i++) {
      const acItem = acItems.nth(i);
      const acStatus = acItem.locator('.ac-status, [data-testid="ac-status"]');
      const status = await acStatus.getAttribute('data-status');

      if (status === 'failed') {
        // 点击重试按钮
        const retryButton = acItem.locator('button:has-text("重试"), [data-testid="retry-ac"]');
        await retryButton.click();

        // 等待重新评估
        await waitForACEvaluation(page, taskId, await acItem.getAttribute('data-ac-id')!, 'passed', 30000);

        // 验证状态更新
        await expect(acStatus).toHaveAttribute('data-status', 'passed');

        break;
      }
    }
  });

  test('应该在任务失败时显示失败详情', async ({ page }) => {
    // 注意：这个测试需要模拟任务失败的场景
    // 这里先提供测试框架

    const taskCard = page.locator(`[data-task-id="${taskId}"]`);

    // 检查是否有失败状态
    const taskStatus = taskCard.locator('.task-status, [data-testid="task-status"]');
    const statusText = await taskStatus.textContent();
    const isFailed = statusText?.includes('失败') || statusText?.includes('failed');

    if (isFailed) {
      // 验证失败卡片显示
      const failCard = taskCard.locator('.task-fail-card, [data-testid="task-fail-card"]');
      await expect(failCard).toBeVisible();

      // 验证失败原因
      const failReason = failCard.locator('.fail-reason, [data-testid="fail-reason"]');
      await expect(failReason).toBeVisible();

      // 验证重试按钮
      const retryButton = failCard.locator('button:has-text("重试"), [data-testid="retry-task"]');
      await expect(retryButton).toBeVisible();
    }
  });

  test('应该支持下载评估报告', async ({ page }) => {
    // 等待任务完成
    await verifyTaskCompletion(page, taskId);

    // 查找下载报告按钮
    const downloadButton = page.locator('button:has-text("下载报告"), [data-testid="download-report"]');
    const isButtonVisible = await downloadButton.isVisible().catch(() => false);

    if (isButtonVisible) {
      // 设置下载监听
      const downloadPromise = page.waitForEvent('download');

      // 点击下载按钮
      await downloadButton.click();

      // 等待下载开始
      const download = await downloadPromise;

      // 验证下载文件名
      expect(download.suggestedFilename()).toContain('report');
      expect(download.suggestedFilename()).toMatch(/\.pdf|\.json|\.txt/);
    }
  });

  test.afterEach(async ({ page }) => {
    await page.screenshot({
      path: `test-results/task-evaluation-${test.info().title.replace(/\s+/g, '-')}.png`,
      fullPage: true,
    });
  });
});
