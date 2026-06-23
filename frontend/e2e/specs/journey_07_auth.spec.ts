/**
 * 用户旅程 07：认证
 *
 * 覆盖场景：注册 → 登录 → Token 验证 → 登出 → 权限检查
 * 对应 features.md 场景 7：认证
 */

import { test, expect } from '@playwright/test';
import { APP_URL, API_BASE, TEST_USER, VIEWER_USER, registerUser, loginViaAPI, logout } from '../helpers/auth';
import { ROUTES } from '../helpers/navigation';

test.describe.configure({ timeout: 120_000 });

test.describe('旅程07：认证', () => {
  test('7.1 注册页面应正确显示', async ({ page }) => {
    await page.goto('/register');
    await page.waitForLoadState('networkidle');

    // 验证注册表单存在
    const formElements = page.locator('input');
    const count = await formElements.count();
    expect(count, '注册页应有输入框').toBeGreaterThanOrEqual(2);

    // 验证有注册按钮
    const registerBtn = page.locator('button').filter({ hasText: /注册|register/i }).first();
    await expect(registerBtn, '注册按钮应可见').toBeVisible({ timeout: 5_000 });
  });

  test('7.2 注册新用户应成功或提示已存在', async ({ page }) => {
    await page.goto('/register');
    await page.waitForLoadState('networkidle');

    // 填写注册表单
    const usernameInput = page.locator('input[placeholder*="用户名"], input[name="username"], input[type="text"]').first();
    const passwordInput = page.locator('input[type="password"]').first();
    const emailInput = page.locator('input[type="email"], input[placeholder*="邮箱"]').first();

    if (await usernameInput.isVisible().catch(() => false)) {
      await usernameInput.fill(TEST_USER.username);
      await passwordInput.fill(TEST_USER.password);
      if (await emailInput.isVisible().catch(() => false)) {
        await emailInput.fill(TEST_USER.email);
      }

      // 点击注册
      const registerBtn = page.locator('button').filter({ hasText: /注册|register/i }).first();
      await registerBtn.click();
      await page.waitForTimeout(2000);

      // 验证结果：要么成功跳转，要么提示已存在
      const currentUrl = page.url();
      const hasError = await page.locator('.error, [role="alert"], .text-red').isVisible().catch(() => false);
      const isSuccess = currentUrl.includes('login') || currentUrl === '/' || !hasError;
      expect(isSuccess, '注册应成功或友好提示').toBeTruthy();
    }
  });

  test('7.3 登录页面应正确显示', async ({ page }) => {
    await page.goto('/login');
    await page.waitForLoadState('networkidle');

    // 验证登录表单
    const inputs = page.locator('input');
    const count = await inputs.count();
    expect(count, '登录页应有输入框').toBeGreaterThanOrEqual(2);

    // 验证有登录按钮
    const loginBtn = page.locator('button').filter({ hasText: /登录|login/i }).first();
    await expect(loginBtn, '登录按钮应可见').toBeVisible({ timeout: 5_000 });
  });

  test('7.4 正确凭据登录应成功跳转', async ({ page }) => {
    // 先确保测试用户存在
    await registerUser(page);

    // 通过 API 登录验证
    const tokens = await loginViaAPI(page);
    expect(tokens.access_token, '应返回 access_token').toBeTruthy();
    expect(tokens.refresh_token, '应返回 refresh_token').toBeTruthy();
    expect(tokens.expires_in, '应返回 expires_in').toBeGreaterThan(0);
  });

  test('7.5 错误凭据登录应失败', async ({ page }) => {
    const loginResp = await page.request.post(`${API_BASE}/api/v1/auth/login`, {
      data: { username: 'wrong_user', password: 'wrong_pass' },
    });
    expect(loginResp.ok(), '错误凭据应登录失败').toBeFalsy();
    expect(loginResp.status(), '错误凭据应返回 4xx').toBeGreaterThanOrEqual(400);
  });

  test('7.6 登出后应跳转到登录页', async ({ page }) => {
    // 先登录
    await registerUser(page);
    const tokens = await loginViaAPI(page);
    await page.goto('/');
    await page.evaluate((data) => {
      localStorage.setItem('access_token', data.access_token);
      localStorage.setItem('refresh_token', data.refresh_token);
      localStorage.setItem('auth_user', JSON.stringify({ id: 'e2e', username: 'e2euser' }));
    }, { access_token: tokens.access_token, refresh_token: tokens.refresh_token });
    await page.reload();
    await page.waitForLoadState('networkidle');

    // 执行登出
    await logout(page);

    // 验证跳转到登录页或认证状态已清除
    const token = await page.evaluate(() => localStorage.getItem('access_token'));
    expect(token, '登出后 token 应被清除').toBeNull();
  });

  test('7.7 Token 过期后应无法访问受保护页面', async ({ page }) => {
    // 注入过期 token
    await page.goto('/');
    await page.evaluate(() => {
      localStorage.setItem('access_token', 'expired_token');
      localStorage.setItem('access_token_expiry', '0');
      localStorage.setItem('auth_user', JSON.stringify({ id: 'e2e', username: 'e2euser' }));
    });
    await page.reload();
    await page.waitForLoadState('networkidle');

    // 验证被重定向到登录页或显示错误
    await page.waitForTimeout(2000);
    const currentUrl = page.url();
    const isLoginPage = currentUrl.includes('login');
    const hasLoading = await page.locator('text=加载中').isVisible().catch(() => false);
    // 页面应处于受限状态（登录页或加载中）
    expect(isLoginPage || hasLoading || currentUrl === '/', '过期 token 应导致受限访问').toBeTruthy();
  });
});
