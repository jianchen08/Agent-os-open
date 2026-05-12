/**
 * 登录功能测试（重构版）
 *
 * 展示如何使用统一的 test-helpers 模块
 */

import { test, expect } from '@playwright/test';

// 使用统一的辅助函数
import {
  login,
  logout,
  isLoggedIn,
  TEST_USER,
  TIMEOUTS,
  waitForPageLoad,
  checkToast,
  takeScreenshot,
  getStorageState,
  clearStorageState,
} from './test-helpers';

test.describe('登录功能测试', () => {
  // 每个测试前执行登录
  test.beforeEach(async ({ page }) => {
    await clearStorageState(page);
    await login(page);
  });

  test.afterEach(async ({ page }) => {
    await takeScreenshot(page, `login-test-${test.info().retry}`);
  });

  test('应该成功登录', async ({ page }) => {
    // 验证已登录
    await expect(page).toHaveURL(/\/(session\/[a-f0-9-]+)?/);

    // 验证页面元素
    const username = page.locator('[data-testid="username"], .user-name');
    await expect(username).toBeVisible({ timeout: TIMEOUTS.ELEMENT_VISIBLE });
  });

  test('应该显示正确的用户信息', async ({ page }) => {
    // 验证用户名
    const username = page.locator('[data-testid="username"], .user-name');
    await expect(username).toContainText(TEST_USER.username);
  });

  test('应该能够登出', async ({ page }) => {
    // 执行登出
    await logout(page);

    // 验证跳转到登录页
    await expect(page).toHaveURL(/\/login/);

    // 验证未登录状态
    const loggedIn = await isLoggedIn(page);
    expect(loggedIn).toBe(false);
  });

  test('应该正确保存登录状态', async ({ page }) => {
    // 检查 localStorage
    const token = await getStorageState(page, 'auth_token');
    expect(token).toBeTruthy();

    const user = await getStorageState(page, 'user');
    expect(user).toBeTruthy();
  });
});

test.describe('登录失败场景', () => {
  test('应该显示错误信息 - 用户名错误', async ({ page }) => {
    await clearStorageState(page);

    // 尝试使用错误的用户名登录
    await page.goto(`${process.env.BASE_URL || 'http://localhost:5188'}/login`);
    await page.fill('input[id="username"]', 'wrong_user');
    await page.fill('input[id="password"]', TEST_USER.password);
    await page.click('button[type="submit"]');

    // 验证错误提示
    await checkToast(page, '用户名或密码错误');
  });

  test('应该显示错误信息 - 密码错误', async ({ page }) => {
    await clearStorageState(page);

    // 尝试使用错误的密码登录
    await page.goto(`${process.env.BASE_URL || 'http://localhost:5188'}/login`);
    await page.fill('input[id="username"]', TEST_USER.username);
    await page.fill('input[id="password"]', 'wrong_password');
    await page.click('button[type="submit"]');

    // 验证错误提示
    await checkToast(page, '用户名或密码错误');
  });

  test('应该验证必填字段', async ({ page }) => {
    await clearStorageState(page);

    // 尝试不填写任何信息登录
    await page.goto(`${process.env.BASE_URL || 'http://localhost:5188'}/login`);
    await page.click('button[type="submit"]');

    // 验证表单验证
    const usernameInput = page.locator('input[id="username"]');
    await expect(usernameInput).toHaveAttribute('required', '');

    const passwordInput = page.locator('input[id="password"]');
    await expect(passwordInput).toHaveAttribute('required', '');
  });
});

// ============================================
// 重构说明
// ============================================

/*
 * 原代码（删除）：
 *
 * test.beforeEach(async ({ page }) => {
 *   await page.goto('/login', { timeout: 30000 });
 *   await page.fill('input[id="username"]', 'admin');
 *   await page.fill('input[id="password"]', 'admin123456');
 *   await page.click('button[type="submit"]');
 *   await page.waitForURL(/\/(session\/[a-f0-9-]+)?/, { timeout: 60000 });
 *   await page.waitForLoadState('networkidle', { timeout: 30000 }).catch(() => {});
 * });
 *
 * 新代码（使用统一 helper）：
 *
 * import { login, TEST_USER, TIMEOUTS, clearStorageState } from './test-helpers';
 *
 * test.beforeEach(async ({ page }) => {
 *   await clearStorageState(page);
 *   await login(page);
 * });
 *
 * 优势：
 * 1. 代码更简洁
 * 2. 使用统一的常量（TEST_USER, TIMEOUTS）
 * 3. 易于维护和修改
 * 4. 所有测试使用相同的登录逻辑
 */
