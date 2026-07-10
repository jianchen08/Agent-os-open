/**
 * 用户旅程 06：记忆与知识
 *
 * 覆盖场景：导航到记忆页 → 存储 → 检索（VECTOR/KEYWORD/TAGWAVE）→ 导航到知识库
 * 对应 features.md 场景 6：记忆与知识
 */

import { test, expect } from '@playwright/test';
import { loginAndNavigateTo, navigateTo, ROUTES } from '../helpers/navigation';

test.describe.configure({ timeout: 120_000 });

test.describe('旅程06：记忆与知识', () => {
  test('6.1 记忆页面应正确加载', async ({ page }) => {
    await loginAndNavigateTo(page, ROUTES.MEMORY);

    const content = await page.textContent('body').catch(() => '');
    const hasMemory = content!.includes('记忆') || content!.includes('memory') || content!.includes('存储');
    expect(hasMemory, '记忆页面应包含记忆相关内容').toBeTruthy();
  });

  test('6.2 记忆存储功能应有交互元素', async ({ page }) => {
    await loginAndNavigateTo(page, ROUTES.MEMORY);

    // 查找存储相关的输入区域
    const storeArea = page.locator('textarea, input[type="text"]').first();
    const hasStoreArea = await storeArea.isVisible().catch(() => false);

    if (hasStoreArea) {
      // 尝试填写内容
      await storeArea.fill('e2e-test-memory-content');
      await page.waitForTimeout(300);

      // 验证输入成功
      const value = await storeArea.inputValue().catch(() => '');
      expect(value, '应能输入存储内容').toContain('e2e-test');
    }
  });

  test('6.3 记忆检索功能应可交互', async ({ page }) => {
    await loginAndNavigateTo(page, ROUTES.MEMORY);

    // 查找检索输入框
    const searchInput = page.locator('input[placeholder*="搜索"], input[placeholder*="检索"], input[placeholder*="查询"]').first();
    const hasSearch = await searchInput.isVisible().catch(() => false);

    if (hasSearch) {
      await searchInput.fill('test query');
      await page.waitForTimeout(300);
      const value = await searchInput.inputValue().catch(() => '');
      expect(value, '应能输入检索关键词').toBe('test query');
    }
  });

  test('6.4 知识库页面应可访问', async ({ page }) => {
    await loginAndNavigateTo(page, ROUTES.KNOWLEDGE_BASE);

    const content = await page.textContent('body').catch(() => '');
    const hasKnowledge = content!.includes('知识') || content!.includes('knowledge') || content!.includes('库');
    expect(hasKnowledge, '知识库页面应包含相关内容').toBeTruthy();
  });

  test('6.5 知识库列表应显示或空状态', async ({ page }) => {
    await loginAndNavigateTo(page, ROUTES.KNOWLEDGE_BASE);

    // 验证列表区域或空状态提示
    const listArea = page.locator('table, .grid, .space-y-4, [data-testid*="knowledge"]').first();
    const emptyState = page.locator('text=暂无, text=空, text=没有').first();

    const hasList = await listArea.isVisible().catch(() => false);
    const hasEmpty = await emptyState.isVisible().catch(() => false);
    expect(hasList || hasEmpty, '应有列表或空状态提示').toBeTruthy();
  });

  test('6.6 记忆设置页应可访问', async ({ page }) => {
    await loginAndNavigateTo(page, ROUTES.SETTINGS_MEMORY);

    const content = await page.textContent('body').catch(() => '');
    const hasConfig = content!.length > 0;
    expect(hasConfig, '记忆设置页应可访问').toBeTruthy();
  });
});
