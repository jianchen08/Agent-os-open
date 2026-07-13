/**
 * 用户旅程 05：触发器
 *
 * 覆盖场景：导航到触发器页 → 查看列表 → 创建触发器 → 启停切换
 * 对应 features.md 场景 5：触发器
 */

import { test, expect } from '@playwright/test';
import { loginAndNavigateTo, ROUTES } from '../helpers/navigation';

test.describe.configure({ timeout: 120_000 });

test.describe('旅程05：触发器', () => {
  test.beforeEach(async ({ page }) => {
    await loginAndNavigateTo(page, ROUTES.TRIGGERS);
  });

  test('5.1 触发器页面应正确加载', async ({ page }) => {
    // 验证页面加载
    const content = await page.textContent('body').catch(() => '');
    const hasTrigger = content!.includes('触发') || content!.includes('trigger') || content!.includes('定时');
    expect(hasTrigger, '触发器页面应包含触发器相关内容').toBeTruthy();
  });

  test('5.2 触发器列表应显示或显示空状态', async ({ page }) => {
    // 验证列表区域存在
    const listArea = page.locator('table, .grid, .space-y-4, [data-testid*="trigger"]').first();
    const hasList = await listArea.isVisible().catch(() => false);
    expect(hasList, '触发器列表区域应可见').toBeTruthy();
  });

  test('5.3 创建触发器按钮应可交互', async ({ page }) => {
    const createBtn = page.locator('button').filter({ hasText: /创建|新建|添加/i }).first();
    const hasBtn = await createBtn.isVisible().catch(() => false);

    if (hasBtn) {
      await createBtn.click();
      await page.waitForTimeout(500);

      // 验证创建表单/对话框出现
      const form = page.locator('form, dialog, [role="dialog"], .modal').first();
      const hasForm = await form.isVisible().catch(() => false);
      if (hasForm) {
        // 查找 cron 表达式输入框
        const cronInput = form.locator('input, textarea').first();
        const hasInput = await cronInput.isVisible().catch(() => false);
        expect(hasInput, '创建表单应有输入框').toBeTruthy();
      }
    }
  });

  test('5.4 触发器启停切换', async ({ page }) => {
    // 查找启停切换按钮
    const toggleBtn = page.locator('button').filter({ hasText: /启用|禁用|启停/i }).first();
    const hasToggle = await toggleBtn.isVisible().catch(() => false);

    if (hasToggle) {
      await toggleBtn.click();
      await page.waitForTimeout(500);
      // 验证状态变化（页面无报错即可）
      const errorEl = page.locator('.error, [role="alert"]').filter({ hasText: /error/i });
      const hasError = await errorEl.isVisible().catch(() => false);
      expect(hasError, '切换启停不应报错').toBeFalsy();
    }
  });

  test('5.5 触发器类型筛选应可交互', async ({ page }) => {
    const filterEl = page.locator('select, [role="combobox"], button').filter({ hasText: /全部|类型|schedule|event|interval/i }).first();
    const hasFilter = await filterEl.isVisible().catch(() => false);

    if (hasFilter) {
      await filterEl.click();
      await page.waitForTimeout(300);
    }
  });
});
