/**
 * E2E 测试 Mock 后端响应工具
 *
 * 使用 Playwright page.route() 拦截网络请求，返回预设响应。
 * 适用于后端不可用时的独立 E2E 测试场景。
 *
 * 设计原则：
 * - 所有 mock 响应与 api_contract.md 中的接口格式一致
 * - 支持按需启用（不影响使用真实后端的测试）
 * - 提供完整的 mock 数据工厂函数
 *
 * 来源：方案文档 7.4 节 E2E 可行性评估，api_contract.md 通信协议
 */

import type { Page, Route } from '@playwright/test';
import { APP_URL } from '../helpers/auth';

// ─── Mock 数据工厂 ────────────────────────────────────────────

/** 生成 mock JWT token（非真实签名，仅用于 E2E） */
function mockToken(prefix: string): string {
  return `mock_${prefix}_${Date.now()}_${Math.random().toString(36).substring(2, 12)}`;
}

/** Mock 登录响应 */
export function mockLoginResponse(): Record<string, unknown> {
  return {
    access_token: mockToken('access'),
    refresh_token: mockToken('refresh'),
    token_type: 'bearer',
    expires_in: 3600,
  };
}

/** Mock 注册响应 */
export function mockRegisterResponse(): Record<string, unknown> {
  return {
    ...mockLoginResponse(),
    user_id: 'mock-user-001',
  };
}

/** Mock 用户信息响应 */
export function mockUserInfoResponse(): Record<string, unknown> {
  return {
    id: 'mock-user-001',
    username: 'e2euser',
    email: 'e2e@test.com',
    created_at: new Date().toISOString(),
  };
}

/** Mock 任务列表响应 */
export function mockTaskListResponse(): Record<string, unknown> {
  return {
    items: [
      {
        id: 'task-001',
        intent: 'E2E 测试任务',
        name: 'E2E 测试任务',
        status: 'completed',
        description: '由 mock-server 生成的测试任务',
        error: null,
        current_step: '已完成',
        created_at: new Date(Date.now() - 3600_000).toISOString(),
      },
      {
        id: 'task-002',
        intent: '运行中的任务',
        name: '运行中的任务',
        status: 'running',
        description: '正在执行',
        error: null,
        current_step: '步骤 2/3',
        created_at: new Date(Date.now() - 1800_000).toISOString(),
      },
      {
        id: 'task-003',
        intent: '等待中的任务',
        name: '等待中的任务',
        status: 'pending',
        description: '排队等待',
        error: null,
        current_step: '',
        created_at: new Date().toISOString(),
      },
    ],
    total: 3,
    page: 1,
    page_size: 20,
  };
}

/** Mock 工作空间文件树响应 */
export function mockWorkspaceFilesResponse(): Record<string, unknown> {
  return {
    id: 'workspace-001',
    container_task_id: 'task-001',
    session_id: 'session-001',
    title: '测试工作空间',
    description: 'E2E 测试工作空间',
    file_tree: [
      {
        name: 'src',
        type: 'directory',
        path: '/src',
        artifact_id: null,
        children: [
          {
            name: 'main.py',
            type: 'file',
            path: '/src/main.py',
            artifact_id: 'artifact-001',
            children: null,
            metadata: {},
          },
        ],
      },
      {
        name: 'README.md',
        type: 'file',
        path: '/README.md',
        artifact_id: 'artifact-002',
        children: null,
        metadata: {},
      },
    ],
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
  };
}

// ─── 路由拦截器 ───────────────────────────────────────────────

/** Mock API 路由匹配规则 */
const MOCK_ROUTES = {
  // 认证相关
  login: /\/api\/v1\/auth\/login$/,
  register: /\/api\/v1\/auth\/register$/,
  currentUser: /\/api\/v1\/auth\/me$/,
  refresh: /\/api\/v1\/auth\/refresh$/,
  logout: /\/api\/v1\/auth\/logout$/,
  // 任务相关
  taskList: /\/api\/tasks(\?|$)/,
  taskDetail: /\/api\/tasks\/[\w-]+$/,
  // 工作空间相关
  workspaceFiles: /\/api\/workspaces\/[\w-]+\/files$/,
} as const;

/**
 * 为单条路由生成 JSON 响应
 */
function jsonResponse(data: Record<string, unknown>, status = 200) {
  return (route: Route) =>
    route.fulfill({
      status,
      contentType: 'application/json',
      body: JSON.stringify(data),
    });
}

/**
 * 处理 mock 请求分发
 */
async function handleMockRoute(
  route: Route,
  handlers: Record<string, (route: Route) => Promise<void>>,
): Promise<void> {
  const url = route.request().url();
  const method = route.request().method();

  // 匹配登录
  if (MOCK_ROUTES.login.test(url) && method === 'POST') {
    await handlers.login?.(route) ?? route.continue();
    return;
  }
  // 匹配注册
  if (MOCK_ROUTES.register.test(url) && method === 'POST') {
    await handlers.register?.(route) ?? route.continue();
    return;
  }
  // 匹配当前用户
  if (MOCK_ROUTES.currentUser.test(url) && method === 'GET') {
    await handlers.currentUser?.(route) ?? route.continue();
    return;
  }
  // 匹配任务列表
  if (MOCK_ROUTES.taskList.test(url) && method === 'GET') {
    await handlers.taskList?.(route) ?? route.continue();
    return;
  }
  // 匹配工作空间文件
  if (MOCK_ROUTES.workspaceFiles.test(url) && method === 'GET') {
    await handlers.workspaceFiles?.(route) ?? route.continue();
    return;
  }

  // 默认放行
  await route.continue();
}

/**
 * 启用全部 mock 后端响应
 *
 * 拦截认证、任务、工作空间等 API 请求，返回预设 mock 数据。
 * 应在 page.goto 之前调用。
 *
 * @param page Playwright Page 实例
 */
export async function enableMockBackend(page: Page): Promise<void> {
  const handlers: Record<string, (route: Route) => Promise<void>> = {
    login: jsonResponse(mockLoginResponse()),
    register: jsonResponse(mockRegisterResponse(), 201),
    currentUser: jsonResponse(mockUserInfoResponse()),
    refresh: jsonResponse(mockLoginResponse()),
    logout: jsonResponse({ message: 'ok' }),
    taskList: jsonResponse(mockTaskListResponse()),
    workspaceFiles: jsonResponse(mockWorkspaceFilesResponse()),
  };

  await page.route('**/api/**', (route) => handleMockRoute(route, handlers));

  console.log('✅ Mock 后端已启用（认证 + 任务 + 工作空间）');
}

/**
 * 启用仅认证 mock
 *
 * 只拦截登录/注册/用户信息请求，其余请求放行到真实后端。
 * 适用于需要真实后端但想用 mock 凭据快速登录的场景。
 *
 * @param page Playwright Page 实例
 */
export async function enableMockAuth(page: Page): Promise<void> {
  const handlers: Record<string, (route: Route) => Promise<void>> = {
    login: jsonResponse(mockLoginResponse()),
    register: jsonResponse(mockRegisterResponse(), 201),
    currentUser: jsonResponse(mockUserInfoResponse()),
  };

  await page.route('**/api/v1/auth/**', (route) => handleMockRoute(route, handlers));

  console.log('✅ Mock 认证已启用');
}

/**
 * 注入 mock localStorage 认证数据
 *
 * 直接在浏览器中设置 localStorage，模拟已登录状态。
 * 适用于无需验证登录流程本身、仅需跳过登录页的场景。
 *
 * @param page Playwright Page 实例
 */
export async function injectMockAuth(page: Page): Promise<void> {
  const tokens = mockLoginResponse();

  await page.goto(APP_URL);

  await page.evaluate((data) => {
    const now = Date.now();
    localStorage.setItem('access_token', data.access_token);
    localStorage.setItem('refresh_token', data.refresh_token);
    localStorage.setItem('access_token_expiry', (now + 3600 * 1000).toString());
    localStorage.setItem(
      'auth_user',
      JSON.stringify({
        id: 'mock-user-001',
        username: 'e2euser',
        email: 'e2e@test.com',
        created_at: new Date().toISOString(),
      }),
    );
  }, tokens);

  console.log('✅ Mock 认证数据已注入 localStorage');
}
