/**
 * E2E 页面导航辅助模块
 *
 * 统一页面导航逻辑，通过 URL 直接导航或侧边栏点击导航到各页面。
 * 所有路由路径与 frontend/src/constants/routes.ts 保持一致。
 */

import { expect, type Page } from '@playwright/test';
import { login } from './auth';

/** 路由路径常量（与 frontend/src/constants/routes.ts 同步） */
export const ROUTES = {
  HOME: '/',
  LOGIN: '/login',
  REGISTER: '/register',
  SETTINGS: '/settings',
  SETTINGS_API: '/settings/api',
  SETTINGS_LLM: '/settings/llm',
  SETTINGS_CONTEXT: '/settings/context',
  SETTINGS_CONCURRENCY: '/settings/concurrency',
  SETTINGS_COST: '/settings/cost',
  SETTINGS_PLUGINS: '/settings/plugins',
  SETTINGS_MEMORY: '/settings/memory',
  SETTINGS_ISOLATION: '/settings/isolation',
  SETTINGS_SECURITY: '/settings/security',
  SETTINGS_EVALUATION: '/settings/evaluation',
  SETTINGS_EXTERNAL_TOOLS: '/settings/external-tools',
  SETTINGS_PIPELINE: '/settings/pipeline',
  SETTINGS_THEME: '/settings/theme',
  SETTINGS_GENERIC: '/settings/generic',
  TOOLS: '/tools',
  AGENTS: '/agents',
  MONITORING: '/monitoring',
  ADMIN: '/admin',
  MEMORY: '/memory',
  TRIGGERS: '/triggers',
  KNOWLEDGE_BASE: '/knowledge-base',
  DEBUG: '/debug',
  DEBUG_EXECUTION_RECORDS: '/debug/execution-records',
  DEBUG_SESSIONS: '/debug/sessions',
  DEBUG_TASKS: '/debug/tasks',
  DEBUG_EVALUATION_METRICS: '/debug/evaluation-metrics',
  DEBUG_USERS: '/debug/users',
} as const;

/**
 * 导航到指定页面（已登录状态）
 *
 * 通过 page.goto 直接访问路径，等待 networkidle 后返回。
 */
export async function navigateTo(page: Page, path: string): Promise<void> {
  await page.goto(path);
  await page.waitForLoadState('networkidle');
}

/**
 * 登录并导航到指定页面
 */
export async function loginAndNavigateTo(page: Page, path: string): Promise<void> {
  await login(page);
  await navigateTo(page, path);
}

/**
 * 通过侧边栏/导航栏链接导航
 *
 * 在已登录页面中，点击导航链接切换到目标页面。
 */
export async function navigateViaSidebar(
  page: Page,
  linkText: string | RegExp,
): Promise<void> {
  const link = page.locator(`a:has-text("${linkText}"), nav a`).filter({ hasText: linkText }).first();
  if (await link.isVisible().catch(() => false)) {
    await link.click();
  } else {
    // 回退：尝试通过 data-testid 查找导航项
    const navItem = page.locator('[data-testid*="nav"]').filter({ hasText: linkText }).first();
    if (await navItem.isVisible().catch(() => false)) {
      await navItem.click();
    }
  }
  await page.waitForLoadState('networkidle');
}

/**
 * 导航到设置页并点击指定标签
 *
 * @param tabName 标签名称（如 'LLM 配置'、'API 配置'、'工具配置' 等）
 */
export async function navigateToSettingsTab(page: Page, tabName: string): Promise<void> {
  await navigateTo(page, ROUTES.SETTINGS);
  const tab = page.locator('nav button, [role="tab"]').filter({ hasText: tabName }).first();
  await expect(tab, `设置标签 "${tabName}" 应可见`).toBeVisible({ timeout: 10_000 });
  await tab.click();
  await page.waitForTimeout(500);
}

/**
 * 导航到调试子页面
 */
export async function navigateToDebugSubpage(page: Page, subpath: string): Promise<void> {
  await navigateTo(page, `${ROUTES.DEBUG}/${subpath}`);
}

/**
 * 获取当前页面 URL 路径
 */
export function getCurrentPath(page: Page): string {
  const url = page.url();
  return new URL(url).pathname;
}

/**
 * 断言当前页面路径
 */
export async function expectCurrentPath(page: Page, expectedPath: string): Promise<void> {
  await expect(page).toHaveURL(new RegExp(expectedPath.replace(/\//g, '\\/')));
}
