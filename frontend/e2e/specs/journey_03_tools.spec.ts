/**
 * 用户旅程 03：工具调用
 *
 * 覆盖场景：导航到工具页 → 查看工具列表 → 触发工具调用 → 验证工具卡片状态流转
 * 对应 features.md 场景 3：工具调用
 */

import { test, expect } from '@playwright/test';
import { loginAndWaitReady } from '../helpers/auth';
import { sendChatMessage } from '../utils/test-helpers';
import { loginAndNavigateTo, ROUTES } from '../helpers/navigation';
import { waitForToolCard, waitForToolCompleted, waitForAssistantMessage } from '../helpers/assertions';

test.describe.configure({ timeout: 120_000 });

test.describe('旅程03：工具调用', () => {
  test('3.1 工具页面应显示工具列表或空状态', async ({ page }) => {
    await loginAndNavigateTo(page, ROUTES.TOOLS);

    // 验证工具页面加载
    const pageTitle = page.locator('h1').filter({ hasText: '工具管理' });
    const hasTitle = await pageTitle.isVisible().catch(() => false);
    expect(hasTitle, '工具页面应加载').toBeTruthy();

    // 验证工具列表区域存在
    const toolSection = page.locator('[data-testid*="tool"], .border.rounded-xl, .grid').first();
    const hasTools = await toolSection.isVisible().catch(() => false);
    expect(hasTools, '工具列表区域应可见').toBeTruthy();
  });

  test('3.2 工具调用卡片应显示工具名称和状态流转', async ({ page }) => {
    await loginAndWaitReady(page);

    // 发送会触发工具调用的消息
    await sendChatMessage(page, '请读取 package.json 文件的内容');

    // 等待工具卡片出现
    const toolCard = await waitForToolCard(page, 45_000);
    console.log('✅ 工具卡片已出现');

    // 验证工具名称
    const titleEl = toolCard.locator('.font-medium').first();
    const title = await titleEl.textContent().catch(() => '');
    expect(title!.length, '工具名称应非空').toBeGreaterThan(0);
    console.log(`🔧 工具名称: ${title}`);

    // 验证 data-activity-type 属性
    const activityType = await toolCard.getAttribute('data-activity-type');
    expect(activityType, 'data-activity-type 应为 tool_call').toBe('tool_call');

    // 验证初始状态为已知值
    const status = await toolCard.getAttribute('data-activity-status');
    expect(['running', 'completed', 'pending'], `状态应为已知值，实际: ${status}`).toContain(status);

    // 等待 completed
    await waitForToolCompleted(page, 30_000);
    const finalStatus = await toolCard.getAttribute('data-activity-status');
    console.log(`最终状态: ${finalStatus}`);
  });

  test('3.3 工具搜索功能应正确过滤', async ({ page }) => {
    await loginAndNavigateTo(page, ROUTES.TOOLS);

    const searchInput = page.locator('input[placeholder*="搜索"]');
    const hasSearch = await searchInput.isVisible().catch(() => false);

    if (hasSearch) {
      const initialCount = await page.locator('.border.rounded-xl').count();
      await searchInput.fill('read');
      await page.waitForTimeout(500);

      const filteredCount = await page.locator('.border.rounded-xl').count();
      expect(filteredCount, '搜索后数量应 <= 初始数量').toBeLessThanOrEqual(initialCount);

      // 清空搜索恢复
      await searchInput.fill('');
      await page.waitForTimeout(500);
      const restoredCount = await page.locator('.border.rounded-xl').count();
      expect(restoredCount, '清空搜索后应恢复原数量').toBe(initialCount);
    }
  });

  test('3.4 工具启用/禁用切换', async ({ page }) => {
    await loginAndNavigateTo(page, ROUTES.TOOLS);

    const toolCards = page.locator('.border.rounded-xl');
    const count = await toolCards.count();

    if (count > 0) {
      const firstCard = toolCards.first();
      const toggleBtn = firstCard.locator('button').last();
      const hasToggle = await toggleBtn.isVisible().catch(() => false);

      if (hasToggle) {
        await toggleBtn.click();
        await page.waitForTimeout(500);
        // 切换回来
        await toggleBtn.click();
        await page.waitForTimeout(500);
      }
    }
  });
});
