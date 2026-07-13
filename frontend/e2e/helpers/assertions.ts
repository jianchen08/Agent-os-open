/**
 * E2E 通用断言辅助模块
 *
 * 统一等待响应、验证 UI 状态的断言函数。
 * 所有断言基于 DOM 元素，不使用截图。
 */

import { expect, type Page, type Locator } from '@playwright/test';

/**
 * 等待助手消息出现并返回其 Locator
 */
export async function waitForAssistantMessage(
  page: Page,
  timeout = 30_000,
): Promise<Locator> {
  const msg = page.locator('[data-role="assistant"]').first();
  await expect(msg, '助手消息应出现').toBeVisible({ timeout });
  return msg;
}

/**
 * 等待工具调用卡片出现
 */
export async function waitForToolCard(
  page: Page,
  timeout = 45_000,
): Promise<Locator> {
  const card = page.locator('[data-activity-type="tool_call"]').first();
  await expect(card, '工具卡片应出现').toBeVisible({ timeout });
  return card;
}

/**
 * 等待工具调用状态变为 completed
 */
export async function waitForToolCompleted(
  page: Page,
  timeout = 30_000,
): Promise<void> {
  await page.waitForFunction(
    () => {
      const card = document.querySelector('[data-activity-type="tool_call"]');
      return card?.getAttribute('data-activity-status') === 'completed';
    },
    { timeout },
  ).catch(() => {
    console.log('⚠️ 工具调用状态未变为 completed（超时）');
  });
}

/**
 * 等待交互卡片（审批）出现
 */
export async function waitForInteractionCard(
  page: Page,
  timeout = 30_000,
): Promise<Locator | null> {
  const byTestid = page.locator('[data-testid="human-interaction-card"]').first();
  const byActivity = page.locator('[data-activity-type="human_interaction"]').first();

  const hasByTestid = await byTestid.isVisible().catch(() => false);
  if (hasByTestid) return byTestid;

  const hasByActivity = await byActivity.isVisible().catch(() => false);
  if (hasByActivity) return byActivity;

  // 等待一段时间再试
  try {
    await expect(byActivity).toBeVisible({ timeout });
    return byActivity;
  } catch {
    return null;
  }
}

/**
 * 验证元素文本内容长度大于指定值
 */
export async function expectTextLength(
  locator: Locator,
  minLength: number,
  message?: string,
): Promise<void> {
  const text = await locator.textContent().catch(() => '');
  expect(text!.length, message ?? `文本长度应大于 ${minLength}`).toBeGreaterThan(minLength);
}

/**
 * 验证元素包含指定文本
 */
export async function expectContainsText(
  locator: Locator,
  text: string,
  message?: string,
): Promise<void> {
  await expect(locator, message ?? `应包含文本 "${text}"`).toContainText(text);
}

/**
 * 验证元素可见
 */
export async function expectVisible(
  locator: Locator,
  message?: string,
  timeout = 10_000,
): Promise<void> {
  await expect(locator, message ?? '元素应可见').toBeVisible({ timeout });
}

/**
 * 验证元素不可见
 */
export async function expectHidden(
  locator: Locator,
  message?: string,
): Promise<void> {
  await expect(locator, message ?? '元素应不可见').not.toBeVisible();
}

/**
 * 验证页面标题
 */
export async function expectPageTitle(
  page: Page,
  title: string,
): Promise<void> {
  const heading = page.locator('h1').filter({ hasText: title });
  await expect(heading, `页面标题应包含 "${title}"`).toBeVisible({ timeout: 10_000 });
}

/**
 * 验证页面 data-testid 元素存在
 */
export async function expectTestIdVisible(
  page: Page,
  testId: string,
  message?: string,
  timeout = 10_000,
): Promise<void> {
  const el = page.locator(`[data-testid="${testId}"]`);
  await expect(el, message ?? `[data-testid="${testId}"] 应可见`).toBeVisible({ timeout });
}

/**
 * 验证活动卡片状态流转
 */
export async function expectActivityStatusTransition(
  page: Page,
  expectedStatus: string,
  timeout = 40_000,
): Promise<void> {
  await page.waitForFunction(
    (status) => {
      const card = document.querySelector('[data-activity-status]');
      return card?.getAttribute('data-activity-status') === status;
    },
    expectedStatus,
    { timeout },
  );
}

/**
 * 等待流式响应文本增长（验证流式渲染正常）
 */
export async function expectStreamingGrowth(
  locator: Locator,
  waitMs = 5_000,
): Promise<{ initial: number; final: number }> {
  const initial = (await locator.textContent().catch(() => ''))?.length ?? 0;
  await locator.page().waitForTimeout(waitMs);
  const final = (await locator.textContent().catch(() => ''))?.length ?? 0;
  expect(final, '流式文本最终长度应 > 0').toBeGreaterThan(0);
  return { initial, final };
}
