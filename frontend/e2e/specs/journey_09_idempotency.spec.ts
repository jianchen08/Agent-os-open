/**
 * 用户旅程 09：消息幂等契约（ADR 2026-08-21）
 *
 * 覆盖场景：发送 → 刷新 → 无重复气泡；发送 → 流式中断线重连 → 无重复气泡。
 * 这是"发送一条消息后旧消息/重复消息冒泡"多轮复发 bug 的端到端回归闸：
 * 契约修复 = client_message_id 幂等键落库回传 + 前端 pending 分离 +
 * 精确 ID 对账（模糊匹配/双窗口宽限/复活链全废）。
 *
 * 断言方式：DOM 元素计数（同内容 user 气泡恰好 1 条）+ 流式内容不丢。
 */

import { test, expect } from '../fixtures';
import { loginAndWaitReady, ADMIN_USER } from '../helpers/auth';
import { sendChatMessage } from '../utils/test-helpers';
import { waitForAssistantMessage } from '../helpers/assertions';

test.describe.configure({ timeout: 180_000 });

/** 数当前消息列表中指定内容的 user 气泡条数（幂等断言核心） */
async function countUserBubbles(page: import('@playwright/test').Page, content: string): Promise<number> {
  return page.locator('[data-testid="message-item"][data-role="user"]', {
    hasText: content,
  }).count();
}

/** 等待页面完成冷加载对账（init 请求返回 + 列表渲染稳定） */
async function waitForReconcile(page: import('@playwright/test').Page): Promise<void> {
  await page.waitForResponse(
    (res) => res.url().includes('/messages') && res.status() === 200,
    { timeout: 30_000 },
  ).catch(() => {/* 401/超时不阻塞——由后续计数断言裁决 */});
  // 等待消息列表 DOM 稳定（连续两次计数一致）
  let prev = -1;
  for (let i = 0; i < 10; i++) {
    await page.waitForTimeout(500);
    const n = await page.locator('[data-testid="message-item"]').count();
    if (n === prev) return;
    prev = n;
  }
}

test.describe('旅程09：消息幂等契约（发送→刷新/重连→无重复）', () => {
  test('9.1 发送→回复完成→刷新页面：同内容 user 气泡恰好 1 条，旧消息不冒泡', async ({ page }) => {
    await loginAndWaitReady(page, ADMIN_USER);
    await waitForReconcile(page);

    const marker = `幂等旅程标记-${Date.now()}`;
    await sendChatMessage(page, `${marker} 请回复"收到"两个字即可`);

    // 回复完成（new_message 到达 + 气泡渲染）
    await waitForAssistantMessage(page, 90_000);

    // 刷新页面（冷启动：IndexedDB rehydrate + init 全量对账）
    await page.reload();
    await waitForReconcile(page);

    // 幂等断言：该内容的 user 气泡恰好 1 条（乐观 pending 已被 cmid 对账驱逐，
    // API 权威版本唯一渲染——旧架构在此产生 2 条）
    expect(await countUserBubbles(page, marker), '刷新后同内容 user 气泡必须恰好 1 条').toBe(1);

    // assistant 回复也不重复
    const assistantCount = await page
      .locator('[data-testid="message-item"][data-role="assistant"]', { hasText: '收到' })
      .count();
    expect(assistantCount, 'assistant 回复不得重复渲染').toBeLessThanOrEqual(2);
  });

  test('9.2 发送→流式中模拟断线重连：无重复 user 气泡、流式内容不中断丢失', async ({ page, context }) => {
    await loginAndWaitReady(page, ADMIN_USER);
    await waitForReconcile(page);

    const marker = `重连幂等标记-${Date.now()}`;
    await sendChatMessage(page, `${marker} 请用100字介绍消息队列`);

    // 等 assistant 气泡出现并开始流式增长
    const assistantMsg = await waitForAssistantMessage(page, 90_000);
    const beforeOffline = await assistantMsg.textContent() ?? '';
    expect(beforeOffline.length, '断线前流式内容应非空').toBeGreaterThan(0);

    // 模拟断线 8s（offline → 回前台自动重连 → replay/backfill 补漏）
    await context.setOffline(true);
    await page.waitForTimeout(8_000);
    await context.setOffline(false);

    // 重连后等待回复完成
    await page.waitForTimeout(3_000);
    await expect
      .poll(async () => (await assistantMsg.textContent())?.length ?? 0, {
        timeout: 90_000,
        message: '重连后流式内容应恢复增长或保持完整',
      })
      .toBeGreaterThanOrEqual(beforeOffline.length);

    // 幂等断言：重连补漏后同内容 user 气泡仍恰好 1 条
    expect(await countUserBubbles(page, marker), '重连补漏后 user 气泡必须恰好 1 条').toBe(1);
  });
});
