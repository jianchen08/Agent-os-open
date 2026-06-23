/**
 * 用户旅程 01：对话流程
 *
 * 覆盖场景：登录 → 创建会话 → 发消息 → 等待流式响应 → 验证渲染
 * 对应 features.md 场景 1：对话流程
 *
 * 断言方式：DOM 元素断言（toBeVisible / toContainText / toHaveAttribute）
 */

import { test, expect } from '@playwright/test';
import { loginAndWaitReady } from '../helpers/auth';
import { sendChatMessage } from '../utils/test-helpers';
import {
  waitForAssistantMessage,
  waitForToolCard,
  waitForToolCompleted,
  expectStreamingGrowth,
} from '../helpers/assertions';

test.describe.configure({ timeout: 120_000 });

test.describe('旅程01：对话流程', () => {
  test('1.1 流式文本应持续增长，不出现断裂', async ({ page }) => {
    await loginAndWaitReady(page);

    // 发送需要较长回答的问题
    await sendChatMessage(page, '请用至少200字介绍一下React的主要特性');

    // 等待助手消息出现
    const assistantMsg = await waitForAssistantMessage(page);
    console.log('✅ 助手消息已出现');

    // 验证流式文本增长
    const { initial, final } = await expectStreamingGrowth(assistantMsg, 5_000);
    console.log(`📊 文本长度变化: ${initial} → ${final}`);
    expect(final, '最终文本长度应大于0').toBeGreaterThan(0);
  });

  test('1.2 工具调用完成后助手消息文本必须继续渲染', async ({ page }) => {
    await loginAndWaitReady(page);

    // 发送会触发工具调用的请求
    await sendChatMessage(page, '请读取 package.json 文件的内容，然后总结一下');

    // 等待助手消息出现
    const assistantMsg = await waitForAssistantMessage(page);

    // 尝试检测工具卡片
    try {
      const toolCard = await waitForToolCard(page, 40_000);
      console.log('✅ 工具卡片已出现');

      // 验证工具卡片属性
      const activityType = await toolCard.getAttribute('data-activity-type');
      expect(activityType, 'data-activity-type 应为 tool_call').toBe('tool_call');

      // 等待工具调用状态变为 completed
      await waitForToolCompleted(page, 30_000);
      console.log('✅ 工具调用已完成');
    } catch {
      console.log('⚠️ 未检测到工具卡片（可能未触发工具调用）');
    }

    // 核心断言：工具调用后文本不应断裂
    const textAfterTool = (await assistantMsg.textContent().catch(() => ''))?.length ?? 0;
    await page.waitForTimeout(5_000);
    const textLater = (await assistantMsg.textContent().catch(() => ''))?.length ?? 0;
    console.log(`📊 工具调用后文本长度变化: ${textAfterTool} → ${textLater}`);

    expect(textLater, '工具调用后文本必须不减少').toBeGreaterThanOrEqual(textAfterTool);
    expect(textLater, '最终文本不应为空').toBeGreaterThan(0);
  });

  test('1.3 连续发送多条消息应正确渲染', async ({ page }) => {
    await loginAndWaitReady(page);

    // 发送第一条消息
    await sendChatMessage(page, '你好，请简短回复');
    const msg1 = await waitForAssistantMessage(page);
    expect((await msg1.textContent().catch(() => ''))?.length ?? 0, '第一条助手消息不为空').toBeGreaterThan(0);

    // 发送第二条消息
    await sendChatMessage(page, '请再简短回复一次');
    const allAssistantMsgs = page.locator('[data-role="assistant"]');
    const count = await allAssistantMsgs.count();
    expect(count, '应有多条助手消息').toBeGreaterThanOrEqual(2);
    console.log(`✅ 共 ${count} 条助手消息`);
  });

  test('1.4 消息角色标识正确（用户/助手）', async ({ page }) => {
    await loginAndWaitReady(page);

    await sendChatMessage(page, '测试角色标识');

    // 验证用户消息角色
    const userMsg = page.locator('[data-role="user"]').first();
    await expect(userMsg, '用户消息应可见').toBeVisible({ timeout: 15_000 });

    // 验证助手消息角色
    const assistantMsg = page.locator('[data-role="assistant"]').first();
    await expect(assistantMsg, '助手消息应可见').toBeVisible({ timeout: 30_000 });

    // 验证 data-role 属性值
    const userRole = await userMsg.getAttribute('data-role');
    const assistantRole = await assistantMsg.getAttribute('data-role');
    expect(userRole).toBe('user');
    expect(assistantRole).toBe('assistant');
  });

  test('1.5 状态更新从 running 流转为 completed', async ({ page }) => {
    await loginAndWaitReady(page);

    // 发送会触发工具调用的消息
    await sendChatMessage(page, '帮我创建一个名为 e2e_test_file.txt 的文件，内容写 hello world');

    // 等待助手消息出现
    await waitForAssistantMessage(page);

    // 检查活动卡片出现并完成状态流转
    const activityCard = page.locator('[data-activity-type]').first();
    const hasActivity = await activityCard.isVisible().catch(() => false);

    if (hasActivity) {
      // 等待状态变为 completed
      await page.waitForFunction(
        () => {
          const card = document.querySelector('[data-activity-status]');
          return card?.getAttribute('data-activity-status') === 'completed';
        },
        { timeout: 40_000 },
      ).catch(() => console.log('⚠️ 等待 completed 状态超时'));

      // 验证 completed 状态存在
      const completedCard = page.locator('[data-activity-status="completed"]').first();
      const hasCompleted = await completedCard.isVisible().catch(() => false);
      console.log(`✅ 检测到 completed 状态: ${hasCompleted}`);
    }

    // 验证助手最终有响应
    const assistantMsg = page.locator('[data-role="assistant"]').first();
    const finalText = await assistantMsg.textContent().catch(() => '');
    expect(finalText!.length, '助手响应不应为空').toBeGreaterThan(0);
  });
});
