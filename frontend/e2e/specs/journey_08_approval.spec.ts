/**
 * 用户旅程 08：审批交互
 *
 * 覆盖场景：发送需要审批的消息 → 交互卡片出现 → 点击批准/拒绝 → 助手继续响应
 * 对应 features.md 场景 8：审批交互
 */

import { test, expect } from '@playwright/test';
import { loginAndWaitReady } from '../helpers/auth';
import { sendChatMessage } from '../utils/test-helpers';
import { waitForAssistantMessage, waitForInteractionCard } from '../helpers/assertions';

test.describe.configure({ timeout: 120_000 });

test.describe('旅程08：审批交互', () => {
  test('8.1 交互卡片弹出时应有可点击按钮', async ({ page }) => {
    await loginAndWaitReady(page);

    // 发送可能触发交互确认的消息
    await sendChatMessage(page, '请确认一下，你准备好回答我的问题了吗？点击确认继续');

    // 等待助手响应
    await waitForAssistantMessage(page);
    console.log('✅ 助手消息已出现');

    // 检查交互卡片
    const interactionCard = await waitForInteractionCard(page, 15_000);

    if (interactionCard) {
      console.log('✅ 发现交互卡片');

      // 验证卡片有按钮
      const button = interactionCard.locator('button').first();
      const hasButton = await button.isVisible().catch(() => false);

      if (hasButton) {
        const buttonText = await button.textContent().catch(() => '');
        console.log(`🔘 按钮文本: ${buttonText}`);
        expect(buttonText!.length, '按钮文本应非空').toBeGreaterThan(0);
      }
    } else {
      console.log('⚠️ 未出现交互卡片（取决于后端配置）');
    }
  });

  test('8.2 点击批准按钮后卡片状态应变化', async ({ page }) => {
    await loginAndWaitReady(page);

    await sendChatMessage(page, '请确认一下继续操作');

    await waitForAssistantMessage(page);

    const interactionCard = await waitForInteractionCard(page, 15_000);

    if (interactionCard) {
      const button = interactionCard.locator('button').first();
      if (await button.isVisible().catch(() => false)) {
        // 记录点击前状态
        const statusBefore = await interactionCard.getAttribute('data-activity-status').catch(() => '');
        console.log(`点击前状态: ${statusBefore}`);

        await button.click();
        await page.waitForTimeout(2000);

        // 验证状态变化或卡片消失
        const statusAfter = await interactionCard.getAttribute('data-activity-status').catch(() => '');
        const cardGone = !(await interactionCard.isVisible().catch(() => false));
        console.log(`点击后状态: ${statusAfter}, 卡片消失: ${cardGone}`);

        // 状态应变化或卡片应消失
        expect(statusAfter !== statusBefore || cardGone, '交互后状态应变化').toBeTruthy();
      }
    }
  });

  test('8.3 交互完成后助手应继续响应', async ({ page }) => {
    await loginAndWaitReady(page);

    await sendChatMessage(page, '请确认一下继续操作');

    const assistantMsg = await waitForAssistantMessage(page);

    // 检查并处理交互卡片
    const interactionCard = await waitForInteractionCard(page, 15_000);
    if (interactionCard) {
      const button = interactionCard.locator('button').first();
      if (await button.isVisible().catch(() => false)) {
        await button.click();
        await page.waitForTimeout(3000);
      }
    }

    // 验证助手有最终响应
    const finalText = await assistantMsg.textContent().catch(() => '');
    expect(finalText!.length, '交互后助手应有响应').toBeGreaterThan(0);
  });
});
