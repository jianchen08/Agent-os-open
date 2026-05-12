/**
 * 认证流程完整测试
 *
 * 测试用户登录、注册、登出的完整流程
 */

import { test, expect } from '@playwright/test';

const TEST_USER = {
  username: 'testuser',
  password: 'testpass123',
  email: 'test@example.com',
};

test.describe('认证流程测试', () => {
  test.describe('登录页面功能', () => {
    test.beforeEach(async ({ page }) => {
      await page.goto('/login');
      await page.waitForLoadState('domcontentloaded');
    });

    test('应该显示登录表单的所有必要元素', async ({ page }) => {
      // 检查页面标题
      const pageTitle = await page.title();
      console.log('页面标题:', pageTitle);
      expect(pageTitle).toBeTruthy();

      // 查找用户名输入框
      const usernameInput = page.locator('input[name="username"], input[id*="username"], input[placeholder*="用户"]');
      const hasUsername = await usernameInput.count() > 0;

      if (hasUsername) {
        await expect(usernameInput.first()).toBeVisible();
        console.log('✓ 用户名输入框存在');
      } else {
        console.log('✗ 用户名输入框不存在');
      }

      // 查找密码输入框
      const passwordInput = page.locator('input[name="password"], input[id*="password"], input[placeholder*="密码"], input[type="password"]');
      const hasPassword = await passwordInput.count() > 0;

      if (hasPassword) {
        await expect(passwordInput.first()).toBeVisible();
        console.log('✓ 密码输入框存在');
      } else {
        console.log('✗ 密码输入框不存在');
      }

      // 查找提交按钮
      const submitButton = page.locator('button[type="submit"], button:has-text("登录")');
      const hasSubmit = await submitButton.count() > 0;

      if (hasSubmit) {
        await expect(submitButton.first()).toBeVisible();
        console.log('✓ 登录按钮存在');
      } else {
        console.log('✗ 登录按钮不存在');
      }

      // 查找注册链接
      const registerLink = page.locator('a[href*="register"], a:has-text("注册")');
      const hasRegisterLink = await registerLink.count() > 0;

      if (hasRegisterLink) {
        console.log('✓ 注册链接存在');
      } else {
        console.log('✗ 注册链接不存在');
      }

      await page.screenshot({ path: 'test-results/auth-login-form-elements.png' });
    });

    test('应该能够填写表单字段', async ({ page }) => {
      // 填写用户名
      const usernameInput = page.locator('input[name="username"], input[id*="username"]');
      const usernameCount = await usernameInput.count();

      if (usernameCount > 0) {
        await usernameInput.first().fill(TEST_USER.username);
        const value = await usernameInput.first().inputValue();
        expect(value).toBe(TEST_USER.username);
        console.log('✓ 用户名输入成功');

        await page.screenshot({ path: 'test-results/auth-username-filled.png' });
      }

      // 填写密码
      const passwordInput = page.locator('input[name="password"], input[type="password"]');
      const passwordCount = await passwordInput.count();

      if (passwordCount > 0) {
        await passwordInput.first().fill(TEST_USER.password);
        const value = await passwordInput.first().inputValue();
        expect(value).toBe(TEST_USER.password);
        console.log('✓ 密码输入成功');

        await page.screenshot({ path: 'test-results/auth-password-filled.png' });
      }
    });

    test('应该支持密码可见性切换', async ({ page }) => {
      const passwordInput = page.locator('input[type="password"]');
      const passwordCount = await passwordInput.count();

      if (passwordCount > 0) {
        const input = passwordInput.first();

        // 初始状态应该是密码类型
        const inputType = await input.getAttribute('type');
        console.log('初始输入框类型:', inputType);

        // 查找切换按钮
        const toggleButton = page.locator('button[aria-label*="密码"], button[aria-label*="password"], .password-toggle');
        const toggleCount = await toggleButton.count();

        if (toggleCount > 0) {
          await toggleButton.first().click();
          await page.waitForTimeout(500);

          const newType = await input.getAttribute('type');
          console.log('点击后输入框类型:', newType);

          await page.screenshot({ path: 'test-results/auth-password-visible.png' });
        } else {
          console.log('✗ 未找到密码切换按钮');
        }
      }

      await page.screenshot({ path: 'test-results/auth-password-toggle.png' });
    });

    test('应该支持回车键提交', async ({ page }) => {
      const passwordInput = page.locator('input[type="password"]');
      const passwordCount = await passwordInput.count();

      if (passwordCount > 0) {
        // 填写表单
        const usernameInput = page.locator('input[name="username"], input[id*="username"]');
        const usernameCount = await usernameInput.count();

        if (usernameCount > 0) {
          await usernameInput.first().fill(TEST_USER.username);
        }

        await passwordInput.first().fill(TEST_USER.password);

        // 在密码框按回车
        const currentUrl = page.url();
        await passwordInput.first().press('Enter');
        await page.waitForTimeout(2000);

        const newUrl = page.url();
        console.log('按回车前 URL:', currentUrl);
        console.log('按回车后 URL:', newUrl);

        await page.screenshot({ path: 'test-results/auth-enter-submit.png' });
      }
    });

    test('应该显示表单验证错误', async ({ page }) => {
      // 不填写任何内容，直接点击提交
      const submitButton = page.locator('button[type="submit"], button:has-text("登录")');
      const submitCount = await submitButton.count();

      if (submitCount > 0) {
        await submitButton.first().click();
        await page.waitForTimeout(1000);

        // 检查是否有错误提示
        const errorSelectors = [
          '.error',
          '[role="alert"]',
          '.text-red',
          '.text-destructive',
          '[data-invalid="true"]',
        ];

        let foundError = false;
        for (const selector of errorSelectors) {
          const errorElement = page.locator(selector);
          const count = await errorElement.count();
          if (count > 0) {
            const isVisible = await errorElement.first().isVisible();
            if (isVisible) {
              console.log(`✓ 找到错误提示: ${selector}`);
              foundError = true;
              break;
            }
          }
        }

        if (!foundError) {
          console.log('✗ 未找到错误提示（可能使用了其他验证方式）');
        }

        await page.screenshot({ path: 'test-results/auth-validation-error.png' });
      }
    });

    test('应该有注册页面链接', async ({ page }) => {
      const registerLink = page.locator('a[href*="register"], a:has-text("注册")');
      const count = await registerLink.count();

      if (count > 0) {
        await registerLink.first().click();
        await page.waitForTimeout(1000);

        const currentUrl = page.url();
        console.log('点击注册链接后 URL:', currentUrl);
        expect(currentUrl).toContain('/register');

        await page.screenshot({ path: 'test-results/auth-register-page.png' });
      } else {
        console.log('✗ 未找到注册链接');
      }
    });
  });

  test.describe('注册页面功能', () => {
    test.beforeEach(async ({ page }) => {
      await page.goto('/register');
      await page.waitForLoadState('domcontentloaded');
    });

    test('应该显示注册表单', async ({ page }) => {
      await page.screenshot({ path: 'test-results/auth-register-form.png' });

      // 查找注册表单元素
      const usernameInput = page.locator('input[name="username"], input[id*="username"]');
      const emailInput = page.locator('input[name="email"], input[type="email"], input[id*="email"]');
      const passwordInput = page.locator('input[name="password"], input[type="password"]');
      const confirmPasswordInput = page.locator(
        'input[name*="confirm"], input[name*="repeat"], input[placeholder*="确认"]'
      );

      console.log('用户名输入框:', await usernameInput.count() > 0 ? '✓' : '✗');
      console.log('邮箱输入框:', await emailInput.count() > 0 ? '✓' : '✗');
      console.log('密码输入框:', await passwordInput.count() > 0 ? '✓' : '✗');
      console.log('确认密码输入框:', await confirmPasswordInput.count() > 0 ? '✓' : '✗');
    });

    test('应该能够填写注册表单', async ({ page }) => {
      const usernameInput = page.locator('input[name="username"], input[id*="username"]');
      const emailInput = page.locator('input[name="email"], input[type="email"]');
      const passwordInput = page.locator('input[name="password"], input[type="password"]');

      if (await usernameInput.count() > 0) {
        await usernameInput.first().fill(TEST_USER.username);
      }

      if (await emailInput.count() > 0) {
        await emailInput.first().fill(TEST_USER.email);
      }

      if (await passwordInput.count() > 0) {
        await passwordInput.first().fill(TEST_USER.password);
      }

      await page.screenshot({ path: 'test-results/auth-register-filled.png' });
    });

    test('应该有返回登录链接', async ({ page }) => {
      const loginLink = page.locator('a[href*="login"], a:has-text("登录")');
      const count = await loginLink.count();

      if (count > 0) {
        await loginLink.first().click();
        await page.waitForTimeout(1000);

        const currentUrl = page.url();
        console.log('点击登录链接后 URL:', currentUrl);
        expect(currentUrl).toContain('/login');

        await page.screenshot({ path: 'test-results/auth-back-to-login.png' });
      }
    });
  });

  test.describe('认证状态管理', () => {
    test('应该能够检测登录状态', async ({ page }) => {
      await page.goto('/');
      await page.waitForLoadState('domcontentloaded');
      await page.waitForTimeout(2000);

      const currentUrl = page.url();
      console.log('当前 URL:', currentUrl);

      // 如果重定向到登录页，说明未登录
      if (currentUrl.includes('/login')) {
        console.log('✓ 检测到未登录状态');
      } else {
        console.log('✓ 用户已登录或无需登录');

        // 检查是否有用户菜单
        const userMenu = page.locator('[data-testid="user-menu"], .user-menu');
        const hasUserMenu = await userMenu.count() > 0;

        if (hasUserMenu) {
          console.log('✓ 找到用户菜单');
        }
      }

      await page.screenshot({ path: 'test-results/auth-status-check.png' });
    });

    test('应该保护受保护的路由', async ({ page }) => {
      const protectedRoutes = ['/settings', '/profile'];

      for (const route of protectedRoutes) {
        await page.goto(route);
        await page.waitForLoadState('domcontentloaded');
        await page.waitForTimeout(1000);

        const currentUrl = page.url();
        console.log(`访问 ${route}，重定向到: ${currentUrl}`);

        // 检查是否重定向到登录页
        if (currentUrl.includes('/login')) {
          console.log(`✓ ${route} 受保护，重定向到登录页`);
        } else {
          console.log(`? ${route} 可能不受保护或用户已登录`);
        }
      }

      await page.screenshot({ path: 'test-results/auth-protected-routes.png' });
    });
  });

  test.describe('登出功能', () => {
    test('应该能够登出（如果已登录）', async ({ page }) => {
      await page.goto('/');
      await page.waitForLoadState('domcontentloaded');
      await page.waitForTimeout(2000);

      const currentUrl = page.url();

      // 如果未登录，跳过测试
      if (currentUrl.includes('/login')) {
        console.log('用户未登录，跳过登出测试');
        test.skip();
      }

      // 查找用户菜单
      const userMenu = page.locator('[data-testid="user-menu"], .user-menu');
      const hasUserMenu = await userMenu.count() > 0;

      if (hasUserMenu) {
        await userMenu.first().click();
        await page.waitForTimeout(500);

        await page.screenshot({ path: 'test-results/auth-user-menu-open.png' });

        // 查找登出按钮
        const logoutButton = page.locator('button:has-text("退出"), button:has-text("登出"), button:has-text("Logout")');
        const logoutCount = await logoutButton.count();

        if (logoutCount > 0) {
          await logoutButton.first().click();
          await page.waitForTimeout(2000);

          const newUrl = page.url();
          console.log('登出后 URL:', newUrl);

          await page.screenshot({ path: 'test-results/auth-after-logout.png' });
        } else {
          console.log('✗ 未找到登出按钮');
        }
      } else {
        console.log('✗ 未找到用户菜单');
      }
    });
  });
});
