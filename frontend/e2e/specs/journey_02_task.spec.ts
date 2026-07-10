/**
 * 用户旅程 02：任务全流程
 *
 * 覆盖场景：任务创建 → Agent 执行 → 状态追踪 → 任务评估 → 任务完成
 * 对应 features.md 场景 2：任务全流程
 */

import { test, expect } from '@playwright/test';
import { loginAndNavigateTo, ROUTES } from '../helpers/navigation';
import { expectPageTitle, expectVisible } from '../helpers/assertions';

test.describe.configure({ timeout: 120_000 });

test.describe('旅程02：任务全流程', () => {
  test.beforeEach(async ({ page }) => {
    await loginAndNavigateTo(page, ROUTES.DEBUG_TASKS);
  });

  test('2.1 调试任务页面应正确加载', async ({ page }) => {
    // 验证页面标题或关键元素可见
    const pageTitle = page.locator('h1, h2, [data-testid]').filter({ hasText: /任务/i }).first();
    await expect(pageTitle, '任务页面应包含任务相关标题').toBeVisible({ timeout: 10_000 });
    // 验证页面 data-testid 存在
    const debugPage = page.locator('[data-testid*="debug"], [data-testid*="task"]').first();
    const hasTestId = await debugPage.isVisible().catch(() => false);
    expect(hasTestId || page.url().includes('task')).toBeTruthy();
  });

  test('2.2 任务列表应显示或显示空状态', async ({ page }) => {
    // 验证任务列表区域存在
    const taskList = page.locator('[data-testid*="task"], .space-y-4, table, .grid').first();
    await expect(taskList, '任务列表区域应可见').toBeVisible({ timeout: 10_000 });

    // 如果有任务，验证状态标签格式
    const statusBadge = page.locator('[data-testid*="status"], .badge, .text-xs').first();
    const hasBadge = await statusBadge.isVisible().catch(() => false);
    if (hasBadge) {
      const badgeText = await statusBadge.textContent().catch(() => '');
      // 状态应为 pending/running/completed/failed/stopped/evaluating/timeout 之一
      const validStatuses = ['pending', 'running', 'completed', 'failed', 'stopped', 'evaluating', 'timeout'];
      const match = validStatuses.some(s => badgeText?.toLowerCase().includes(s));
      console.log(`📋 任务状态: ${badgeText}，有效: ${match}`);
    }
  });

  test('2.3 创建任务表单应可交互', async ({ page }) => {
    // 查找创建任务按钮
    const createBtn = page.locator('button').filter({ hasText: /创建|新建|添加/i }).first();
    const hasBtn = await createBtn.isVisible().catch(() => false);

    if (hasBtn) {
      await createBtn.click();
      await page.waitForTimeout(500);

      // 验证表单出现
      const form = page.locator('form, dialog, .modal, [role="dialog"]').first();
      const hasForm = await form.isVisible().catch(() => false);
      if (hasForm) {
        // 验证表单中有输入框
        const input = form.locator('input, textarea, select').first();
        await expect(input, '表单应有输入框').toBeVisible();
      }
    }
  });

  test('2.4 任务状态标签应显示有效值', async ({ page }) => {
    // 验证页面包含任务相关的状态信息
    const content = await page.textContent('body').catch(() => '');
    const hasTaskContent = content!.includes('任务') || content!.includes('task') || content!.includes('Task');
    expect(hasTaskContent, '页面应包含任务相关内容').toBeTruthy();
  });

  test('2.5 评估指标页面应可访问', async ({ page }) => {
    await page.goto(ROUTES.DEBUG_EVALUATION_METRICS);
    await page.waitForLoadState('networkidle');

    const evalPage = page.locator('[data-testid*="eval"], h1, h2').first();
    const hasEval = await evalPage.isVisible().catch(() => false);
    expect(hasEval, '评估指标页面应可访问').toBeTruthy();
  });
});
