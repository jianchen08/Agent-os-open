/** @feature FP-0.2.四 前端Schema | @ci frontend-e2e */
/**
 * 用户旅程 12：断线重连不丢回复（2026-08-23 重放饥饿回归闸）
 *
 * 背景：用户症状「最后一轮回复不显示，刷新才出现」的确定性复现——turn 恰在
 * 断连窗口内完成时，new_message 落内核重放缓冲；旧实现重放只等首条带
 * thread_id 的入站消息触发（前端重连后不重发），缓冲事件永不补达。
 *
 * 本旅程用 `ws.close()` 强制断开 chat WS（真断连，触发前端 onclose → 4s 退避
 * 重连 → 内核 replay_all_for_user 建连重放），断言：
 *   1. 断连期间完成的回复在重连后自动补达显示（无手动刷新）；
 *   2. 无「输出被中断」假警告（interrupted 宽限重查，2026-08-23）；
 *   3. user 气泡不重复（幂等）。
 *
 * 注意：context.setOffline 对 localhost WS 无效（浏览器不切断同源连接），
 * 旧旅程 09.2 用它是假绿——本旅程是重放链路的真机闸。
 */

import { test, expect } from '../fixtures';
import { registerUser, loginViaAPI, ADMIN_USER, API_BASE, APP_URL } from '../helpers/auth';
import { sendChatMessage } from '../utils/test-helpers';

test.describe.configure({ timeout: 180_000 });

/** 注入 WebSocket 包装：记录所有实例，供测试强制关闭 /ws/chat 连接。 */
async function installWsTracker(page: import('@playwright/test').Page): Promise<void> {
  await page.addInitScript(() => {
    const w = window as unknown as {
      WebSocket: new (url: string, protocols?: string | string[]) => WebSocket
      __wsInstances: Array<{ url: string; ws: WebSocket; readyState: () => number }>
    }
    const OrigWS = w.WebSocket
    w.__wsInstances = []
    w.WebSocket = function (url: string, protocols?: string | string[]) {
      const ws = protocols !== undefined ? new OrigWS(url, protocols) : new OrigWS(url)
      try {
        w.__wsInstances.push({ url: String(url), ws, readyState: () => ws.readyState })
      } catch { /* 忽略 */ }
      return ws
    }
    ;(w.WebSocket as unknown as { prototype: unknown }).prototype = OrigWS.prototype
  })
}

/**
 * 登录 + 建会话 + 就绪（登录界面延迟敏感，不用 helper 的固定 20s：
 * 本机高负载/冷启动时恢复链可能到 10s+，此处轮询 45s）。
 */
async function loginAndCreateSession(page: import('@playwright/test').Page): Promise<void> {
  await registerUser(page, ADMIN_USER);
  const tokens = await loginViaAPI(page, ADMIN_USER);
  await page.goto(APP_URL, { waitUntil: 'domcontentloaded' });
  await page.evaluate((data) => {
    const now = Date.now();
    localStorage.setItem('access_token', data.access_token);
    localStorage.setItem('refresh_token', data.refresh_token);
    localStorage.setItem('access_token_expiry', (now + data.expires_in * 1000).toString());
    localStorage.setItem('auth_user', JSON.stringify({ id: 'e2e', username: data.username }));
  }, { access_token: tokens.access_token, refresh_token: tokens.refresh_token, expires_in: tokens.expires_in, username: 'admin' });
  await page.reload({ waitUntil: 'domcontentloaded' });

  const createResp = await page.request.post(`${API_BASE}/api/v1/sessions`, {
    headers: { Authorization: `Bearer ${tokens.access_token}`, 'X-Main-Agent-Request': 'true' },
    data: { title: `journey12-${Date.now()}` },
  });
  expect(createResp.ok(), '建会话应成功（admin 角色）').toBeTruthy();
  const created = await createResp.json();
  const sid = created.thread_id ?? created.id;
  await page.evaluate((v) => localStorage.setItem('last_active_session', JSON.stringify(v)), sid);
  await page.reload({ waitUntil: 'domcontentloaded' });

  await expect
    .poll(
      async () =>
        await page.locator('[data-testid="chat-input-textarea"]').isVisible().catch(() => false),
      { timeout: 30_000, message: '聊天输入框应在会话恢复后可见' },
    )
    .toBe(true);
}

/** 强制关闭当前打开的 /ws/chat 连接（复刻服务端 idle 踢产生的 onclose）。 */
async function forceCloseChatWs(page: import('@playwright/test').Page): Promise<number> {
  return page.evaluate(() => {
    const w = window as unknown as {
      __wsInstances?: Array<{ url: string; ws: WebSocket; readyState: () => number }>
    }
    const inst = (w.__wsInstances || []).filter(
      (x) => x.url.includes('/ws/chat') && x.readyState() === 1,
    )
    inst.forEach((x) => x.ws.close())
    return inst.length
  })
}

/** 读取页面最后一个 assistant 气泡的正文（去除思考区等噪声）。 */
async function lastAssistantText(page: import('@playwright/test').Page): Promise<string> {
  const bubbles = page.locator('[data-testid="message-item"][data-role="assistant"]');
  const n = await bubbles.count();
  if (n === 0) return '';
  return (await bubbles.last().innerText()) ?? '';
}

test.describe('旅程12：断线重连不丢回复（ws.close 真断连）', () => {
  test('12.1 断连窗口内完成的回复：重连后自动补达显示，无假中断警告', async ({ page }) => {
    await installWsTracker(page);
    await loginAndCreateSession(page);

    // 短问题 + 快回复，保证 turn 大概率在断连窗口内完成（复刻用户时序）
    const marker = `断连重放标记-${Date.now()}`;
    await sendChatMessage(page, `${marker} 请用一句话回答：天空为什么是蓝色的`);

    // 等 stream_start 出现（占位已建）后 1.2s 强断 WS
    await expect(page.locator('[data-role="assistant"]').first()).toBeVisible({ timeout: 30_000 });
    await page.waitForTimeout(1200);
    const closed = await forceCloseChatWs(page);
    expect(closed, '应强制关闭至少一个 chat WS').toBeGreaterThanOrEqual(1);

    // 重连后（4s 退避 + 握手）：最终回复必须自动补达（旧内核在此永久缺失）
    await expect
      .poll(async () => (await lastAssistantText(page)).length, {
        timeout: 90_000,
        message: '断连期间完成的回复应在重连后自动显示（无需刷新）',
      })
      .toBeGreaterThan(20);

    // 无「输出被中断」假警告（宽限重查：20s 内零活动才标真丢失；此处重放补达）
    const body = await page.locator('body').innerText();
    expect(body, '不得出现假「输出被中断」警告').not.toContain('输出被中断');

    // 幂等：同内容 user 气泡恰好 1 条
    const userCount = await page
      .locator('[data-testid="message-item"][data-role="user"]', { hasText: marker })
      .count();
    expect(userCount, '断线重连后 user 气泡不得重复').toBe(1);
  });
});
