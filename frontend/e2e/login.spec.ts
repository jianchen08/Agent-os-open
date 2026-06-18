/**
 * 登录流程 E2E 测试
 *
 * 覆盖方案文档 7.8 节场景 5（认证全链路）的前端部分：
 * - 打开登录页 → 输入账号密码 → 点击登录 → 验证页面跳转 → 验证 Token 持久化
 *
 * 使用真实浏览器操作（page.fill / page.click）模拟用户行为。
 * 来源：方案文档 7.8 场景 5，features.md 场景 7
 */

import { test, expect } from '@playwright/test';
import {
  loginViaUI,
  verifyTokenPersisted,
} from './utils/test-helpers';
import { API_BASE, APP_URL, TEST_USER } from './helpers/auth';

test.describe('登录流程 E2E', () => {
  test.describe.configure({ timeout: 120_000 });

  test('打开登录页，应展示登录表单', async ({ page }) => {
    await page.goto(`${APP_URL}/login`);
    await page.waitForLoadState('domcontentloaded');

    // 验证登录页可见
    await expect(
      page.locator('[data-testid="login-page"]'),
      '登录页应可见',
    ).toBeVisible({ timeout: 10_000 });

    // 验证表单元素
    await expect(
      page.locator('[data-testid="login-form"]'),
      '登录表单应可见',
    ).toBeVisible();
    await expect(
      page.locator('[data-testid="login-username-input"]'),
      '用户名输入框应可见',
    ).toBeVisible();
    await expect(
      page.locator('[data-testid="login-password-input"]'),
      '密码输入框应可见',
    ).toBeVisible();
    await expect(
      page.locator('[data-testid="login-submit-button"]'),
      '登录按钮应可见',
    ).toBeVisible();
  });

  test('输入账号密码并点击登录，应跳转到首页', async ({ page }) => {
    // 确保测试用户存在
    await page.request.post(`${API_BASE}/api/v1/auth/register`, {
      data: {
        username: TEST_USER.username,
        password: TEST_USER.password,
        email: TEST_USER.email,
      },
      failOnStatusCode: false,
    });

    // 执行 UI 级登录
    await loginViaUI(page);

    // 验证已跳转到首页
    await expect(page).toHaveURL(APP_URL + '/', { timeout: 30_000 });
  });

  test('登录成功后，Token 应持久化到 localStorage', async ({ page }) => {
    // 确保测试用户存在
    await page.request.post(`${API_BASE}/api/v1/auth/register`, {
      data: {
        username: TEST_USER.username,
        password: TEST_USER.password,
        email: TEST_USER.email,
      },
      failOnStatusCode: false,
    });

    // 执行 UI 级登录
    await loginViaUI(page);

    // 验证 Token 持久化
    const result = await verifyTokenPersisted(page);
    expect(result.success, 'Token 持久化验证应成功').toBe(true);
    expect(result.token, 'access_token 不应为空').toBeTruthy();
    expect(result.refreshToken, 'refresh_token 不应为空').toBeTruthy();
  });

  test('刷新页面后，认证状态应保持（Token 持久化生效）', async ({ page }) => {
    // 确保测试用户存在
    await page.request.post(`${API_BASE}/api/v1/auth/register`, {
      data: {
        username: TEST_USER.username,
        password: TEST_USER.password,
        email: TEST_USER.email,
      },
      failOnStatusCode: false,
    });

    // 执行 UI 级登录
    await loginViaUI(page);

    // 验证首次 Token 持久化
    const firstResult = await verifyTokenPersisted(page);

    // 刷新页面
    await page.reload();
    await page.waitForLoadState('networkidle');

    // 验证刷新后 Token 仍然存在
    const tokensAfterReload = await page.evaluate(() => {
      return {
        token: localStorage.getItem('access_token'),
        refreshToken: localStorage.getItem('refresh_token'),
        expiry: localStorage.getItem('access_token_expiry'),
      };
    });

    expect(
      tokensAfterReload.token,
      '刷新后 access_token 应仍然存在',
    ).not.toBeNull();
    expect(
      tokensAfterReload.token,
      '刷新后 token 应与刷新前一致',
    ).toBe(firstResult.token);

    // 验证页面未被重定向回登录页
    await expect(page).not.toHaveURL(/\/login/);
  });

  test('空用户名提交时，应显示验证错误', async ({ page }) => {
    await page.goto(`${APP_URL}/login`);
    await page.waitForLoadState('domcontentloaded');

    // 留空用户名，只填写密码
    await page.locator('[data-testid="login-username-input"]').fill('');
    await page.locator('[data-testid="login-password-input"]').fill(TEST_USER.password);

    // 触发失焦验证
    await page.locator('[data-testid="login-password-input"]').blur();

    // 验证错误提示出现
    await expect(
      page.locator('[data-testid="username-error"]'),
      '用户名为空时应显示验证错误',
    ).toBeVisible({ timeout: 5_000 });
  });

  test('错误密码登录时，应显示错误提示', async ({ page }) => {
    await page.goto(`${APP_URL}/login`);
    await page.waitForLoadState('domcontentloaded');

    // 填写正确用户名和错误密码
    await page.locator('[data-testid="login-username-input"]').fill(TEST_USER.username);
    await page.locator('[data-testid="login-password-input"]').fill('WrongPassword123!');

    // 点击登录
    await page.locator('[data-testid="login-submit-button"]').click();

    // 验证错误提示出现
    await expect(
      page.locator('[data-testid="login-error"]'),
      '错误密码应显示全局错误提示',
    ).toBeVisible({ timeout: 15_000 });
  });
});
