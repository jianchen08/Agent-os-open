/**
 * E2E 认证辅助模块
 *
 * 统一登录/注册/Token 注入逻辑，沿用 comprehensive-e2e.spec.ts 的 loginAndWaitReady 模式：
 * 1. 尝试注册测试用户（已存在则忽略）
 * 2. POST /api/v1/auth/login → 获取 token
 * 3. page.goto(APP_URL) → page.evaluate 注入 localStorage
 * 4. page.reload() → 等待聊天输入框就绪
 */

import { expect, type Page } from '@playwright/test';

/** 后端 API 基础地址 */
// 内核 API 直连地址：默认对齐 vite proxy 目标 9100；E2E_API_BASE 可覆盖
// （原硬编码 8988 与运行内核端口不符，注册/登录恒 ECONNREFUSED）。
export const API_BASE = process.env.E2E_API_BASE ?? 'http://localhost:9100';
/** 前端应用地址 */
export const APP_URL = 'http://localhost:5188';

/** 默认测试用户凭据 */
export const TEST_USER = {
  username: 'e2euser',
  password: 'Test123456!',
  email: 'e2e@test.com',
} as const;

/** 管理员用户：会话创建等写面端点要求 admin 角色（write_surface_auth），
 *  普通测试用户 POST /api/v1/sessions 恒 403——登录就绪需建会话的旅程用此账号。 */
export const ADMIN_USER = {
  username: 'admin',
  password: 'admin12345',
  email: 'admin@agentos.local',
} as const;

/** 备用测试用户（用于权限验证） */
export const VIEWER_USER = {
  username: 'e2e_viewer',
  password: 'Viewer123456!',
  email: 'e2e_viewer@test.com',
} as const;

/**
 * 注册用户（如已存在则忽略）
 */
export async function registerUser(
  page: Page,
  user: { username: string; password: string; email: string } = TEST_USER,
): Promise<void> {
  const resp = await page.request.post(`${API_BASE}/api/v1/auth/register`, {
    data: {
      username: user.username,
      password: user.password,
      email: user.email,
    },
  });
  // 注册成功或用户已存在均视为正常
  if (resp.ok()) {
    console.log(`✅ 用户 ${user.username} 注册成功`);
  } else {
    console.log(`ℹ️ 用户 ${user.username} 注册返回 ${resp.status()}（可能已存在）`);
  }
}

/**
 * 通过 API 登录并获取 token
 *
 * @returns 登录响应中的 token 数据
 */
export async function loginViaAPI(
  page: Page,
  user: { username: string; password: string } = TEST_USER,
): Promise<{ access_token: string; refresh_token: string; expires_in: number }> {
  const loginResp = await page.request.post(`${API_BASE}/api/v1/auth/login`, {
    data: { username: user.username, password: user.password },
  });

  if (!loginResp.ok()) {
    throw new Error(`登录失败: status=${loginResp.status()}`);
  }

  const tokens = await loginResp.json();
  console.log(`✅ 登录成功, access_token 长度: ${tokens.access_token?.length ?? 0}`);
  return tokens;
}

/**
 * 将 Token 注入 localStorage 并刷新页面
 */
export async function injectTokensAndReload(
  page: Page,
  tokens: { access_token: string; refresh_token: string; expires_in: number },
  username: string = TEST_USER.username,
  email: string = TEST_USER.email,
): Promise<void> {
  await page.goto(APP_URL);
  await page.evaluate(
    (data) => {
      const now = Date.now();
      localStorage.setItem('access_token', data.access_token);
      localStorage.setItem('refresh_token', data.refresh_token);
      localStorage.setItem('access_token_expiry', (now + data.expires_in * 1000).toString());
      localStorage.setItem(
        'auth_user',
        JSON.stringify({
          id: 'e2e',
          username: data.username,
          email: data.email,
          created_at: new Date().toISOString(),
        }),
      );
    },
    {
      access_token: tokens.access_token,
      refresh_token: tokens.refresh_token,
      expires_in: tokens.expires_in,
      username,
      email,
    },
  );

  await page.reload({ waitUntil: 'domcontentloaded' });
}

/**
 * 完整登录流程：注册 → 登录 → 注入 → 刷新 → 等待就绪
 *
 * 与 comprehensive-e2e.spec.ts 的 loginAndWaitReady 模式一致。
 * 登录后进入聊天主页，点击"新会话"按钮，等待聊天输入框可见。
 */
export async function loginAndWaitReady(
  page: Page,
  user: { username: string; password: string; email: string } = TEST_USER,
): Promise<void> {
  // 注册（忽略已存在的情况）
  await registerUser(page, user);

  // 登录获取 token
  const tokens = await loginViaAPI(page, user);

  // 注入并刷新
  await injectTokensAndReload(page, tokens, user.username, user.email);

  // 建会话优先走 API（快且稳）；普通角色 403（写面端点要求 admin）时回退
  // UI 弹窗流程（Sidebar"新建会话"→填标题→"创建"），两类账号都能就绪。
  const title = `e2e-${user.username}-${Date.now()}`;
  const createResp = await page.request.post(`${API_BASE}/api/v1/sessions`, {
    headers: {
      Authorization: `Bearer ${tokens.access_token}`,
      // 与前端 createSession 同款标记头（内核主代理请求鉴权）
      'X-Main-Agent-Request': 'true',
    },
    data: { title },
  });
  if (!createResp.ok()) {
    // 内核写面 RBAC：POST /api/v1/sessions 要求 admin 角色——普通测试用户
    // UI 弹窗建会话同样 403（无回退路径）。需要登录就绪（建会话）的旅程
    // 必须传 ADMIN_USER。
    throw new Error(
      `API 建会话失败: status=${createResp.status()}（写面端点要求 admin 角色，请传 ADMIN_USER）`,
    );
  }
  const created = await createResp.json();
  const sessionId = created.thread_id ?? created.id;
  // StorageService.setItem 走 JSON 序列化——键值必须是 JSON 串（裸字符串
  // 会被 getItem 的 JSON.parse 分支判为无效值而丢弃，恢复失效）。
  await page.evaluate((sid) => {
    localStorage.setItem('last_active_session', JSON.stringify(sid));
  }, sessionId);
  await page.reload();

  // 等待聊天输入框就绪——轮询而非硬超时：本机高负载/冷启动/长会话列表下
  // 会话恢复链（query 缓存 → restoreActiveSessionIfNeeded → setActiveSession →
  // loadPipelineMessages）可达 10s+，硬 20s 在慢机器上偶发误报（2026-08-23
  // 实测 6s~30s 抖动）。poll 45s 把慢启动留成环境容忍，不掩盖真故障。
  await expect
    .poll(
      async () =>
        await page
          .locator('[data-testid="chat-input-textarea"]')
          .isVisible()
          .catch(() => false),
      { timeout: 45_000, message: '聊天输入框应在会话恢复后可见' },
    )
    .toBe(true);

  console.log('✅ 聊天界面就绪');
}

/**
 * 轻量登录：仅注入 token 并刷新，不创建新会话
 *
 * 适用于需要登录但不需要聊天界面的测试（如设置页、监控页等）。
 */
export async function login(page: Page): Promise<void> {
  await registerUser(page);
  const tokens = await loginViaAPI(page);
  await injectTokensAndReload(page, tokens);
}

/**
 * 登出当前用户
 */
export async function logout(page: Page): Promise<void> {
  // 清除 localStorage 中的认证信息
  await page.evaluate(() => {
    localStorage.removeItem('access_token');
    localStorage.removeItem('refresh_token');
    localStorage.removeItem('access_token_expiry');
    localStorage.removeItem('auth_user');
  });
  await page.reload({ waitUntil: 'domcontentloaded' });
}
