/**
 * 功能矩阵 Spec — API 契约冒烟 + Agent 页锚点
 *
 * 页面可达/交互类功能点由 journey_* spec 持有同流程更强断言（设置页←
 * journey_04、任务/评估←journey_02、工具←journey_03、触发器←journey_05、
 * 记忆/知识库←journey_06、认证←journey_07、监控/调试←journey_09_monitoring、
 * Agent/管理员←journey_10、对话流式/工具卡←journey_01 1.2 + 03 3.2）。
 * 本文件只持有两条线：
 *   1. FP-11.x 核心登录态 API 端点契约冒烟（agents/config/tasks/tools）；
 *   2. FP-3.1 Agent 页可达锚点（矩阵侧代表）。
 */

import { test, expect } from '@playwright/test';
import { login, API_BASE, registerUser, loginViaAPI } from '../helpers/auth';
import { navigateTo, ROUTES } from '../helpers/navigation';

test.describe.configure({ timeout: 120_000 });

// ═══════════════════════════════════════════════════════════════
// 3. Agent 管理（矩阵侧锚点）
// ═══════════════════════════════════════════════════════════════

test.describe('功能矩阵 - 3.Agent管理', () => {
  test('FP-3.1 Agent 列表显示', async ({ page }) => {
    await login(page);
    await navigateTo(page, ROUTES.AGENTS);
    const content = await page.textContent('body').catch(() => '');
    expect(content!.includes('Agent') || content!.includes('智能体'), '应有 Agent 信息').toBeTruthy();
  });
});

// ═══════════════════════════════════════════════════════════════
// 11. 多通道接入（API 端点契约冒烟；负例 401 由 journey_07 7.5 持有）
// ═══════════════════════════════════════════════════════════════

test.describe('功能矩阵 - 11.多通道接入', () => {
  test('FP-11.2 HTTP API 端点可达', async ({ page }) => {
    await registerUser(page);
    const tokens = await loginViaAPI(page);
    const resp = await page.request.get(`${API_BASE}/api/v1/agents`, {
      headers: { Authorization: `Bearer ${tokens.access_token}` },
    });
    expect(resp.ok(), 'HTTP API 应可达').toBeTruthy();
  });

  test('FP-11.3 配置读写 API', async ({ page }) => {
    await registerUser(page);
    const tokens = await loginViaAPI(page);
    const resp = await page.request.get(`${API_BASE}/api/v1/config/llm`, {
      headers: { Authorization: `Bearer ${tokens.access_token}` },
    });
    expect(resp.ok(), '配置读 API 应可达').toBeTruthy();
  });

  test('FP-11.4 任务 API', async ({ page }) => {
    await registerUser(page);
    const tokens = await loginViaAPI(page);
    const resp = await page.request.get(`${API_BASE}/api/v1/tasks`, {
      headers: { Authorization: `Bearer ${tokens.access_token}` },
    });
    expect(resp.status(), '任务 API 应响应').toBeLessThan(500);
  });

  test('FP-11.5 工具 API', async ({ page }) => {
    await registerUser(page);
    const tokens = await loginViaAPI(page);
    const resp = await page.request.get(`${API_BASE}/api/v1/tools`, {
      headers: { Authorization: `Bearer ${tokens.access_token}` },
    });
    expect(resp.ok(), '工具 API 应可达').toBeTruthy();
  });
});
