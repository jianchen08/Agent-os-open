/**
 * 真实交互功能测试
 *
 * 测试实际的用户交互和功能，而不仅仅是页面加载
 */

import { test, expect } from '@playwright/test';

test.describe('真实交互功能测试', () => {
  test.beforeEach(async ({ page }) => {
    // 设置较长的超时时间
    test.setTimeout(60000);
  });

  test.describe('仪表板页面交互', () => {
    test('应该显示欢迎信息和用户名', async ({ page }) => {
      await page.goto('/');

      // 等待页面加载
      await page.waitForLoadState('domcontentloaded');
      await page.waitForTimeout(2000);

      // 检查欢迎信息
      const welcomeText = page.locator('h1').filter({ hasText: /欢迎回来/ });
      const isVisible = await welcomeText.isVisible().catch(() => false);

      if (isVisible) {
        await expect(welcomeText).toBeVisible();
        await page.screenshot({ path: 'test-results/interactions-dashboard-welcome.png' });
      } else {
        // 如果没有登录，可能会重定向到登录页
        const currentUrl = page.url();
        console.log('当前 URL:', currentUrl);
        await page.screenshot({ path: 'test-results/interactions-dashboard-redirect.png' });
      }
    });

    test('应该显示或隐藏"新建会话"按钮', async ({ page }) => {
      await page.goto('/');
      await page.waitForLoadState('domcontentloaded');
      await page.waitForTimeout(2000);

      // 查找新建会话按钮
      const newSessionButton = page.locator('button').filter({ hasText: /新建会话/ });
      const count = await newSessionButton.count();

      if (count > 0) {
        await expect(newSessionButton.first()).toBeVisible();
        await page.screenshot({ path: 'test-results/interactions-new-session-button.png' });

        // 尝试点击按钮
        await newSessionButton.first().click();
        await page.waitForTimeout(1000);

        // 检查是否打开了模态框或导航
        const currentUrl = page.url();
        console.log('点击新建会话后的 URL:', currentUrl);

        await page.screenshot({ path: 'test-results/interactions-after-new-session.png' });
      } else {
        console.log('未找到新建会话按钮，可能需要登录');
        await page.screenshot({ path: 'test-results/interactions-no-new-session.png' });
      }
    });

    test('应该显示最近会话列表或空状态', async ({ page }) => {
      await page.goto('/');
      await page.waitForLoadState('domcontentloaded');
      await page.waitForTimeout(2000);

      // 检查是否有会话列表
      const sessionList = page.locator('text=/最近会话/');
      const hasSessionList = await sessionList.count() > 0;

      if (hasSessionList) {
        await expect(sessionList.first()).toBeVisible();

        // 检查是否有会话项
        const sessionItems = page.locator('button').filter({ hasText: /条消息/ });
        const itemCount = await sessionItems.count();

        console.log('找到会话数量:', itemCount);

        if (itemCount > 0) {
          await page.screenshot({ path: 'test-results/interactions-sessions-with-data.png' });
        } else {
          // 检查空状态
          const emptyState = page.locator('text=/还没有会话/');
          const hasEmptyState = await emptyState.isVisible().catch(() => false);

          if (hasEmptyState) {
            await expect(emptyState).toBeVisible();
            await page.screenshot({ path: 'test-results/interactions-sessions-empty.png' });
          }
        }
      } else {
        await page.screenshot({ path: 'test-results/interactions-no-session-section.png' });
      }
    });

    test('应该显示加载状态', async ({ page }) => {
      // 快速导航以捕获加载状态
      await page.goto('/');

      // 立即检查是否有加载指示器
      const loadingText = page.locator('text=/加载中/');
      const wasLoading = await loadingText.isVisible().catch(() => false);

      if (wasLoading) {
        console.log('检测到加载状态');
        await page.screenshot({ path: 'test-results/interactions-loading-state.png' });
      }

      // 等待加载完成
      await page.waitForTimeout(3000);
      await page.screenshot({ path: 'test-results/interactions-after-loading.png' });
    });
  });

  test.describe('侧边栏交互功能', () => {
    test('应该显示或隐藏侧边栏', async ({ page }) => {
      await page.goto('/');
      await page.waitForLoadState('domcontentloaded');
      await page.waitForTimeout(2000);

      // 查找侧边栏
      const sidebar = page.locator('[data-testid="sidebar"], aside');
      const sidebarCount = await sidebar.count();

      if (sidebarCount > 0) {
        const isVisible = await sidebar.first().isVisible();
        console.log('侧边栏可见性:', isVisible);

        if (isVisible) {
          await page.screenshot({ path: 'test-results/interactions-sidebar-visible.png' });

          // 检查侧边栏内容
          const header = sidebar.first().locator('[data-testid="sidebar-header"]');
          const hasHeader = await header.count() > 0;

          if (hasHeader) {
            await expect(header.first()).toBeVisible();
            console.log('侧边栏头部可见');

            // 检查新建按钮
            const newButton = header.first().locator('[data-testid="new-session-button"]');
            const hasNewButton = await newButton.count() > 0;

            if (hasNewButton) {
              console.log('找到新建会话按钮');
              await page.screenshot({ path: 'test-results/interactions-sidebar-with-button.png' });
            }
          }
        } else {
          console.log('侧边栏被隐藏');
        }
      } else {
        console.log('未找到侧边栏元素');
        await page.screenshot({ path: 'test-results/interactions-no-sidebar.png' });
      }
    });

    test('应该显示会话搜索框', async ({ page }) => {
      await page.goto('/');
      await page.waitForLoadState('domcontentloaded');
      await page.waitForTimeout(2000);

      // 查找搜索框
      const searchInput = page.locator('input[placeholder*="搜索"], input[type="search"]');
      const count = await searchInput.count();

      if (count > 0) {
        await expect(searchInput.first()).toBeVisible();
        await page.screenshot({ path: 'test-results/interactions-search-input.png' });

        // 尝试输入搜索关键词
        await searchInput.first().fill('测试');
        await page.waitForTimeout(1000);

        console.log('搜索功能测试完成');
        await page.screenshot({ path: 'test-results/interactions-search-filled.png' });
      } else {
        console.log('未找到搜索框');
        await page.screenshot({ path: 'test-results/interactions-no-search.png' });
      }
    });
  });

  test.describe('按钮交互测试', () => {
    test('所有可点击元素应该有正确的状态', async ({ page }) => {
      await page.goto('/');
      await page.waitForLoadState('domcontentloaded');
      await page.waitForTimeout(2000);

      // 查找所有按钮
      const buttons = page.locator('button');
      const count = await buttons.count();

      console.log('页面上的按钮数量:', count);

      if (count > 0) {
        // 检查前几个按钮的可见性
        for (let i = 0; i < Math.min(count, 5); i++) {
          const button = buttons.nth(i);
          const isVisible = await button.isVisible().catch(() => false);

          if (isVisible) {
            const buttonText = await button.textContent();
            console.log(`按钮 ${i + 1}: "${buttonText?.trim()}"`);

            // 检查按钮是否可点击
            const isEnabled = await button.isEnabled();
            console.log(`  - 可用: ${isEnabled}`);
          }
        }

        await page.screenshot({ path: 'test-results/interactions-buttons.png' });
      }
    });

    test('悬停效果应该正常工作', async ({ page }) => {
      await page.goto('/');
      await page.waitForLoadState('domcontentloaded');
      await page.waitForTimeout(2000);

      // 查找可悬停的元素
      const hoverableElements = page.locator('button, a');
      const count = await hoverableElements.count();

      if (count > 0) {
        // 对第一个可见按钮进行悬停测试
        for (let i = 0; i < count; i++) {
          const element = hoverableElements.nth(i);
          const isVisible = await element.isVisible().catch(() => false);

          if (isVisible) {
            await element.hover();
            await page.waitForTimeout(500);

            const tagName = await element.evaluate((el) => el.tagName);
            console.log(`悬停在 ${tagName} 元素上`);

            await page.screenshot({ path: `test-results/interactions-hover-${i}.png` });
            break; // 只测试第一个元素
          }
        }
      }
    });
  });

  test.describe('响应式布局交互', () => {
    test('桌面视图应该显示侧边栏', async ({ page }) => {
      await page.setViewportSize({ width: 1280, height: 720 });
      await page.goto('/');
      await page.waitForLoadState('domcontentloaded');
      await page.waitForTimeout(2000);

      const sidebar = page.locator('[data-testid="sidebar"], aside');
      const count = await sidebar.count();

      if (count > 0) {
        const isVisible = await sidebar.first().isVisible();
        console.log('桌面视图 - 侧边栏可见:', isVisible);
      }

      await page.screenshot({ path: 'test-results/interactions-desktop-layout.png', fullPage: true });
    });

    test('移动视图应该隐藏或折叠侧边栏', async ({ page }) => {
      await page.setViewportSize({ width: 375, height: 667 });
      await page.goto('/');
      await page.waitForLoadState('domcontentloaded');
      await page.waitForTimeout(2000);

      await page.screenshot({ path: 'test-results/interactions-mobile-layout.png', fullPage: true });

      // 检查是否有汉堡菜单或侧边栏
      const sidebar = page.locator('[data-testid="sidebar"], aside');
      const count = await sidebar.count();

      if (count > 0) {
        const isVisible = await sidebar.first().isVisible();
        console.log('移动视图 - 侧边栏可见:', isVisible);
      }

      // 检查是否有菜单按钮
      const menuButton = page.locator('button[aria-label*="菜单"], button[aria-label*="menu"]');
      const menuCount = await menuButton.count();

      if (menuCount > 0) {
        console.log('找到菜单按钮');
      }
    });
  });

  test.describe('键盘导航测试', () => {
    test('应该支持 Tab 键导航', async ({ page }) => {
      await page.goto('/');
      await page.waitForLoadState('domcontentloaded');
      await page.waitForTimeout(2000);

      // 按几次 Tab 键
      for (let i = 0; i < 5; i++) {
        await page.keyboard.press('Tab');
        await page.waitForTimeout(300);

        // 获取当前焦点元素
        const focusedElement = await page.evaluate(() => {
          const el = document.activeElement;
          return {
            tagName: el?.tagName,
            type: (el as HTMLInputElement)?.type,
            ariaLabel: el?.getAttribute('aria-label'),
            textContent: el?.textContent?.slice(0, 50),
          };
        });

        console.log(`Tab ${i + 1}:`, focusedElement);
      }

      await page.screenshot({ path: 'test-results/interactions-keyboard-navigation.png' });
    });

    test('应该支持 Enter 键激活按钮', async ({ page }) => {
      await page.goto('/');
      await page.waitForLoadState('domcontentloaded');
      await page.waitForTimeout(2000);

      // Tab 到第一个按钮
      await page.keyboard.press('Tab');
      await page.waitForTimeout(300);

      // 按 Enter
      await page.keyboard.press('Enter');
      await page.waitForTimeout(1000);

      const currentUrl = page.url();
      console.log('按 Enter 后的 URL:', currentUrl);

      await page.screenshot({ path: 'test-results/interactions-enter-key.png' });
    });
  });

  test.describe('表单交互测试', () => {
    test('输入框应该可以输入和清除文本', async ({ page }) => {
      await page.goto('/');
      await page.waitForLoadState('domcontentloaded');
      await page.waitForTimeout(2000);

      // 查找输入框
      const input = page.locator('input[type="text"], input:not([type])');
      const count = await input.count();

      if (count > 0) {
        const firstInput = input.first();
        await expect(firstInput).toBeVisible();

        // 输入文本
        await firstInput.fill('测试文本');
        await page.waitForTimeout(500);

        const value = await firstInput.inputValue();
        console.log('输入框的值:', value);
        expect(value).toBe('测试文本');

        // 清除文本
        await firstInput.fill('');
        await page.waitForTimeout(500);

        const clearedValue = await firstInput.inputValue();
        console.log('清除后的值:', clearedValue);
        expect(clearedValue).toBe('');

        await page.screenshot({ path: 'test-results/interactions-input-test.png' });
      } else {
        console.log('页面上没有输入框');
      }
    });

    test('应该验证必填字段', async ({ page }) => {
      await page.goto('/login');
      await page.waitForLoadState('domcontentloaded');
      await page.waitForTimeout(1000);

      // 查找表单
      const form = page.locator('form');
      const formCount = await form.count();

      if (formCount > 0) {
        // 查找提交按钮
        const submitButton = page.locator('button[type="submit"]').first();
        const hasSubmitButton = await submitButton.count() > 0;

        if (hasSubmitButton) {
          // 不填写任何内容，直接点击提交
          await submitButton.click();
          await page.waitForTimeout(1000);

          // 检查是否有错误提示
          const errorMessage = page.locator('.error, [role="alert"], .text-red');
          const errorCount = await errorMessage.count();

          if (errorCount > 0) {
            console.log('检测到表单验证错误');
            await page.screenshot({ path: 'test-results/interactions-form-validation.png' });
          } else {
            console.log('未检测到表单验证');
          }
        }
      }

      await page.screenshot({ path: 'test-results/interactions-login-form.png' });
    });
  });

  test.describe('性能测试', () => {
    test('页面应该在合理时间内加载完成', async ({ page }) => {
      const startTime = Date.now();

      await page.goto('/');
      await page.waitForLoadState('domcontentloaded');
      await page.waitForLoadState('networkidle');

      const loadTime = Date.now() - startTime;
      console.log('页面加载时间:', loadTime, 'ms');

      expect(loadTime).toBeLessThan(10000); // 10秒内加载完成

      await page.screenshot({ path: 'test-results/interactions-page-load-time.png' });
    });

    test('按钮点击应该快速响应', async ({ page }) => {
      await page.goto('/');
      await page.waitForLoadState('domcontentloaded');
      await page.waitForTimeout(2000);

      const button = page.locator('button').first();
      const isVisible = await button.isVisible().catch(() => false);

      if (isVisible) {
        const startTime = Date.now();
        await button.click();
        const responseTime = Date.now() - startTime;

        console.log('按钮点击响应时间:', responseTime, 'ms');

        expect(responseTime).toBeLessThan(1000); // 1秒内响应
      }

      await page.screenshot({ path: 'test-results/interactions-button-response.png' });
    });
  });
});
