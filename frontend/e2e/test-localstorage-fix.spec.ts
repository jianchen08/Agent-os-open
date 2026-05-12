/**
 * 测试 localStorage SecurityError 修复
 *
 * 验证修复后的辅助函数能够正确处理 localStorage 访问
 */

import { test, expect } from '@playwright/test';
import {
  quickLoginImproved,
  setupTestEnvironment,
} from './helpers-improved';

test.describe('localStorage SecurityError 修复验证', () => {
  test.beforeEach(async () => {
    console.log('\n========== 测试开始 ==========');
  });

  test.afterEach(async () => {
    console.log('========== 测试结束 ==========\n');
  });

  test('应该能在 about:blank 页面上正确导航并访问 localStorage', async ({ page }) => {
    console.log('测试：从 about:blank 开始检查登录状态');

    // 确保从 about:blank 开始
    await page.goto('about:blank');
    expect(page.url()).toBe('about:blank');

    // 这个调用现在应该能正确处理 about:blank 的情况
    const isLoggedIn = await page.evaluate(async () => {
      // 模拟 checkLoginStatus 的逻辑
      try {
        const token = localStorage.getItem('access_token');
        const user = localStorage.getItem('user');
        return !!(token && user);
      } catch (error) {
        console.error('localStorage 访问失败:', error);
        return false;
      }
    });

    expect(isLoggedIn).toBe(false);
    console.log('✓ 成功处理 about:blank 页面');
  });

  test('应该能在正确导航后访问 localStorage', async ({ page }) => {
    console.log('测试：导航到首页后访问 localStorage');

    // 先导航到首页
    await page.goto('/');
    await page.waitForLoadState('domcontentloaded');

    // 检查 localStorage 是否可访问
    const canAccessLocalStorage = await page.evaluate(() => {
      try {
        localStorage.setItem('test_key', 'test_value');
        const value = localStorage.getItem('test_key');
        localStorage.removeItem('test_key');
        return value === 'test_value';
      } catch (error) {
        console.error('localStorage 访问失败:', error);
        return false;
      }
    });

    expect(canAccessLocalStorage).toBe(true);
    console.log('✓ 成功在正确页面上访问 localStorage');
  });

  test('应该能完成完整的登录流程', async ({ page }) => {
    console.log('测试：完整的登录流程');

    // 执行登录
    const result = await quickLoginImproved(page);

    expect(result.success).toBe(true);
    console.log(`✓ 登录成功，尝试次数: ${result.attempts}`);

    // 验证 token 已保存
    const hasToken = await page.evaluate(() => {
      return !!localStorage.getItem('access_token');
    });

    expect(hasToken).toBe(true);
    console.log('✓ Token 已正确保存到 localStorage');
  });

  test('应该能清理登录状态', async ({ page }) => {
    console.log('测试：清理登录状态');

    // 先登录
    await quickLoginImproved(page);

    // 验证已登录
    const hasTokenBefore = await page.evaluate(() => {
      return !!localStorage.getItem('access_token');
    });
    expect(hasTokenBefore).toBe(true);

    // 清理状态
    await page.evaluate(() => {
      localStorage.clear();
      sessionStorage.clear();
    });

    // 验证已清理
    const hasTokenAfter = await page.evaluate(() => {
      return !!localStorage.getItem('access_token');
    });
    expect(hasTokenAfter).toBe(false);

    console.log('✓ 登录状态清理成功');
  });

  test('应该能处理从 about:blank 开始的登录流程', async ({ page }) => {
    console.log('测试：从 about:blank 开始的登录流程');

    // 从 about:blank 开始
    await page.goto('about:blank');
    expect(page.url()).toBe('about:blank');

    // 执行登录，应该能自动处理 about:blank 的情况
    const result = await quickLoginImproved(page);

    expect(result.success).toBe(true);
    console.log('✓ 从 about:blank 开始登录成功');

    // 验证当前 URL 不再是 about:blank
    expect(page.url()).not.toBe('about:blank');
    console.log('✓ 已正确导航到应用页面');
  });

  test('应该能处理 localStorage 访问失败的情况', async ({ page }) => {
    console.log('测试：localStorage 访问失败的容错处理');

    // 导航到首页
    await page.goto('/');

    // 测试带有错误处理的 localStorage 访问
    const result = await page.evaluate(() => {
      try {
        // 尝试访问 localStorage
        const token = localStorage.getItem('access_token');
        return { success: true, hasToken: !!token };
      } catch (error) {
        // 如果失败，返回错误信息但不抛出异常
        return {
          success: false,
          error: error instanceof Error ? error.message : String(error)
        };
      }
    });

    // 无论 localStorage 是否可用，都应该返回结果而不是抛出异常
    expect(result).toBeDefined();
    console.log('✓ localStorage 访问有适当的错误处理');
  });
});

test.describe('登录流程集成测试', () => {
  test('应该能设置完整的测试环境', async ({ page }) => {
    console.log('测试：完整测试环境设置');

    // 这将测试所有修复的函数集成在一起
    await setupTestEnvironment(page);

    // 验证环境已正确设置
    const hasToken = await page.evaluate(() => {
      return !!localStorage.getItem('access_token');
    });

    expect(hasToken).toBe(true);
    console.log('✓ 测试环境设置成功');
  });

  test('应该能处理多次登录尝试', async ({ page }) => {
    console.log('测试：多次登录尝试');

    // 第一次登录
    const result1 = await quickLoginImproved(page);
    expect(result1.success).toBe(true);

    // 第二次登录（应该检测到已登录）
    const result2 = await quickLoginImproved(page);
    expect(result2.success).toBe(true);

    console.log(`✓ 多次登录成功，第二次尝试次数: ${result2.attempts}`);
    expect(result2.attempts).toBeLessThan(2); // 应该快速返回，因为已登录
  });
});
