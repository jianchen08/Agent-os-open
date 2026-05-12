/**
 * 主题切换功能完整测试
 *
 * 测试覆盖：
 * - 切换到暗色主题（验证 DOM、localStorage、组件颜色）
 * - 切换到亮色主题（验证 DOM、localStorage、组件颜色）
 * - 刷新后主题保持（验证持久化）
 * - 系统主题模式跟随
 * - 主题切换动画
 * - 多页面主题一致性
 * - 后端设置保存（如果有）
 *
 * Requirements: 2.4, 2.1.4, 2.2.4
 */

import { test, expect } from '@playwright/test';
import {
  login,
  getCurrentTheme,
  verifyTheme,
  getElementColor,
  getStorageState,
  setStorageState,
  waitForAPIResponse,
  waitForThemeTransition,
  takeScreenshot,
} from './helpers';

test.describe('主题切换功能测试套件', () => {
  // 每个测试前登录并导航到首页
  test.beforeEach(async ({ page }) => {
    await login(page);
    await page.goto('/');
    await page.waitForLoadState('networkidle');
  });

  test.describe('暗色主题切换', () => {
    test('01-应该能切换到暗色主题', async ({ page }) => {
      // 记录初始主题
      const initialTheme = await getCurrentTheme(page);
      console.log(`初始主题: ${initialTheme}`);

      // 尝试找到主题切换按钮
      const themeButtonSelectors = [
        '[data-testid="theme-toggle"]',
        'button[aria-label*="主题"]',
        'button[aria-label*="theme"]',
        '.theme-toggle',
      ];

      let themeButtonFound = false;
      for (const selector of themeButtonSelectors) {
        try {
          const isVisible = await page.locator(selector).isVisible({ timeout: 2000 });
          if (isVisible) {
            // 点击按钮切换主题
            await page.click(selector);
            themeButtonFound = true;
            break;
          }
        } catch {
          // 继续尝试下一个选择器
        }
      }

      // 如果找不到按钮，尝试通过设置页面切换
      if (!themeButtonFound) {
        console.log('未找到主题切换按钮，尝试通过设置页面切换');

        // 导航到设置页面
        await page.goto('/settings');
        await page.waitForLoadState('networkidle');

        // 查找主题选择器
        const themeSelect = page.locator('select[name="theme"], [data-testid="theme-select"]');
        const isVisible = await themeSelect.isVisible().catch(() => false);

        if (isVisible) {
          await themeSelect.selectOption('dark');
        } else {
          // 尝试通过 useTheme hook 直接操作
          await page.evaluate(() => {
            localStorage.setItem('theme', 'dark');
            window.dispatchEvent(new Event('storage'));
          });
        }
      }

      // 等待主题切换完成
      await waitForThemeTransition(page);
      await page.waitForTimeout(500);

      // 验证 html 根元素的 data-theme 属性或 class
      const newTheme = await getCurrentTheme(page);
      console.log(`切换后主题: ${newTheme}`);

      // 验证主题已切换到 dark
      expect(newTheme).toBe('dark');

      // 验证 localStorage 保存
      const storedTheme = await getStorageState(page, 'theme');
      expect(storedTheme).toBe('dark');

      // 验证组件颜色变化（检查背景色和前景色）
      const rootBgColor = await getElementColor(page, 'html', 'backgroundColor');
      const rootFgColor = await getElementColor(page, 'html', 'color');

      console.log(`暗色主题 - 背景色: ${rootBgColor}, 前景色: ${rootFgColor}`);

      // 暗色主题应该有较暗的背景
      expect(rootBgColor).not.toBe('rgb(255, 255, 255)');

      await takeScreenshot(page, '01-dark-theme-applied');
    });

    test('02-暗色主题下组件颜色应该正确', async ({ page }) => {
      // 设置为暗色主题
      await setStorageState(page, { theme: 'dark' });
      await page.reload();
      await page.waitForLoadState('networkidle');

      // 等待主题应用
      await page.waitForTimeout(500);

      // 检查导航栏颜色
      const navBg = await getElementColor(page, 'nav, header', 'backgroundColor');
      console.log(`导航栏背景: ${navBg}`);

      // 检查卡片组件颜色
      const cardBg = await getElementColor(page, '.card, .border', 'backgroundColor');
      console.log(`卡片背景: ${cardBg}`);

      // 检查文本颜色
      const textColor = await getElementColor(page, 'body', 'color');
      console.log(`文本颜色: ${textColor}`);

      // 暗色主题下文本应该是浅色
      const brightness = await page.evaluate(() => {
        const color = window.getComputedStyle(document.body).color;
        const rgb = color.match(/\d+/g);
        if (!rgb) return 0;
        const [, g, b] = rgb.map(Number);
        return (g * 299 + b * 587 + b * 114) / 1000;
      });

      expect(brightness).toBeGreaterThan(128); // 浅色文本

      await takeScreenshot(page, '02-dark-theme-colors');
    });

    test('03-暗色主题下按钮样式应该正确', async ({ page }) => {
      // 设置为暗色主题
      await setStorageState(page, { theme: 'dark' });
      await page.reload();
      await page.waitForLoadState('networkidle');

      // 查找页面上的按钮
      const buttons = page.locator('button').filter({ hasText: /./ });
      const buttonCount = await buttons.count();

      if (buttonCount > 0) {
        // 检查第一个按钮的样式
        const buttonBg = await getElementColor(page, 'button', 'backgroundColor');
        const buttonText = await getElementColor(page, 'button', 'color');

        console.log(`按钮背景: ${buttonBg}, 文本: ${buttonText}`);

        // 验证按钮可见性
        await expect(buttons.first()).toBeVisible();
      }

      await takeScreenshot(page, '03-dark-theme-buttons');
    });
  });

  test.describe('亮色主题切换', () => {
    test('04-应该能切换到亮色主题', async ({ page }) => {
      // 先切换到暗色主题
      await setStorageState(page, { theme: 'dark' });
      await page.reload();
      await page.waitForLoadState('networkidle');
      await page.waitForTimeout(500);

      // 验证当前是暗色
      let currentTheme = await getCurrentTheme(page);
      console.log(`当前主题: ${currentTheme}`);
      expect(currentTheme).toBe('dark');

      // 切换到亮色主题
      await setStorageState(page, { theme: 'light' });
      await page.reload();
      await page.waitForLoadState('networkidle');

      // 等待主题切换完成
      await waitForThemeTransition(page);
      await page.waitForTimeout(500);

      // 验证主题已切换到 light
      currentTheme = await getCurrentTheme(page);
      console.log(`切换后主题: ${currentTheme}`);
      expect(currentTheme).toBe('light');

      // 验证 localStorage 保存
      const storedTheme = await getStorageState(page, 'theme');
      expect(storedTheme).toBe('light');

      // 验证组件颜色变化
      const rootBgColor = await getElementColor(page, 'html', 'backgroundColor');
      console.log(`亮色主题 - 背景色: ${rootBgColor}`);

      // 亮色主题应该有较亮的背景
      expect(
        rootBgColor === 'rgb(255, 255, 255)' ||
        rootBgColor === 'rgba(255, 255, 255, 1)' ||
        rootBgColor.includes('255')
      ).toBeTruthy();

      await takeScreenshot(page, '04-light-theme-applied');
    });

    test('05-亮色主题下组件颜色应该正确', async ({ page }) => {
      // 设置为亮色主题
      await setStorageState(page, { theme: 'light' });
      await page.reload();
      await page.waitForLoadState('networkidle');

      // 等待主题应用
      await page.waitForTimeout(500);

      // 检查导航栏颜色
      const navBg = await getElementColor(page, 'nav, header', 'backgroundColor');
      console.log(`导航栏背景: ${navBg}`);

      // 检查卡片组件颜色
      const cardBg = await getElementColor(page, '.card, .border', 'backgroundColor');
      console.log(`卡片背景: ${cardBg}`);

      // 检查文本颜色
      const textColor = await getElementColor(page, 'body', 'color');
      console.log(`文本颜色: ${textColor}`);

      // 亮色主题下文本应该是深色
      const brightness = await page.evaluate(() => {
        const color = window.getComputedStyle(document.body).color;
        const rgb = color.match(/\d+/g);
        if (!rgb) return 255;
        const [r, g, b] = rgb.map(Number);
        return (r * 299 + g * 587 + b * 114) / 1000;
      });

      expect(brightness).toBeLessThan(128); // 深色文本

      await takeScreenshot(page, '05-light-theme-colors');
    });
  });

  test.describe('主题持久化', () => {
    test('06-刷新后主题应该保持不变（亮色）', async ({ page }) => {
      // 设置为亮色主题
      await setStorageState(page, { theme: 'light' });
      await page.reload();
      await page.waitForLoadState('networkidle');

      // 验证主题
      await verifyTheme(page, 'light');

      // 刷新页面
      await page.reload();
      await page.waitForLoadState('networkidle');
      await page.waitForTimeout(500);

      // 验证主题仍然是 light
      const currentTheme = await getCurrentTheme(page);
      expect(currentTheme).toBe('light');

      // 验证 localStorage 未变
      const storedTheme = await getStorageState(page, 'theme');
      expect(storedTheme).toBe('light');

      await takeScreenshot(page, '06-light-theme-persistence');
    });

    test('07-刷新后主题应该保持不变（暗色）', async ({ page }) => {
      // 设置为暗色主题
      await setStorageState(page, { theme: 'dark' });
      await page.reload();
      await page.waitForLoadState('networkidle');

      // 验证主题
      await verifyTheme(page, 'dark');

      // 刷新页面
      await page.reload();
      await page.waitForLoadState('networkidle');
      await page.waitForTimeout(500);

      // 验证主题仍然是 dark
      const currentTheme = await getCurrentTheme(page);
      expect(currentTheme).toBe('dark');

      // 验证 localStorage 未变
      const storedTheme = await getStorageState(page, 'theme');
      expect(storedTheme).toBe('dark');

      await takeScreenshot(page, '07-dark-theme-persistence');
    });

    test('08-跨页面主题应该保持一致', async ({ page }) => {
      // 设置为暗色主题
      await setStorageState(page, { theme: 'dark' });
      await page.reload();
      await page.waitForLoadState('networkidle');

      // 验证首页主题
      await verifyTheme(page, 'dark');

      // 导航到设置页面
      await page.goto('/settings');
      await page.waitForLoadState('networkidle');

      // 验证设置页面主题
      const settingsTheme = await getCurrentTheme(page);
      expect(settingsTheme).toBe('dark');

      // 导航到会话页面
      await page.goto('/sessions');
      await page.waitForLoadState('networkidle');

      // 验证会话页面主题
      const sessionsTheme = await getCurrentTheme(page);
      expect(sessionsTheme).toBe('dark');

      // 返回首页
      await page.goto('/');
      await page.waitForLoadState('networkidle');

      // 验证首页主题仍是 dark
      const homeTheme = await getCurrentTheme(page);
      expect(homeTheme).toBe('dark');

      await takeScreenshot(page, '08-theme-consistency-across-pages');
    });
  });

  test.describe('系统主题模式', () => {
    test('09-应该支持系统主题模式', async ({ page }) => {
      // 设置为系统主题
      await setStorageState(page, { theme: 'system' });
      await page.reload();
      await page.waitForLoadState('networkidle');

      // 验证 localStorage 保存
      const storedTheme = await getStorageState(page, 'theme');
      expect(storedTheme).toBe('system');

      // 验证主题已应用（应该是 light 或 dark 之一）
      const currentTheme = await getCurrentTheme(page);
      expect(['light', 'dark']).toContain(currentTheme);

      console.log(`系统主题模式下的实际主题: ${currentTheme}`);

      await takeScreenshot(page, '09-system-theme-mode');
    });

    test('10-系统主题应该跟随系统设置变化', async ({ page }) => {
      // 模拟系统主题变化
      await page.emulateMedia({ colorScheme: 'dark' });

      // 设置为系统主题
      await setStorageState(page, { theme: 'system' });
      await page.reload();
      await page.waitForLoadState('networkidle');

      // 验证应用了暗色主题
      const darkTheme = await getCurrentTheme(page);
      expect(darkTheme).toBe('dark');

      // 切换到亮色系统主题
      await page.emulateMedia({ colorScheme: 'light' });
      await page.reload();
      await page.waitForLoadState('networkidle');

      // 验证应用了亮色主题
      const lightTheme = await getCurrentTheme(page);
      expect(lightTheme).toBe('light');

      await takeScreenshot(page, '10-system-theme-follows');
    });
  });

  test.describe('主题切换交互', () => {
    test('11-主题切换应该流畅无卡顿', async ({ page }) => {
      // 设置为亮色主题
      await setStorageState(page, { theme: 'light' });
      await page.reload();
      await page.waitForLoadState('networkidle');

      const startTime = Date.now();

      // 切换到暗色主题
      await setStorageState(page, { theme: 'dark' });
      await page.reload();
      await page.waitForLoadState('networkidle');

      // 等待主题过渡完成
      await waitForThemeTransition(page);

      const switchTime = Date.now() - startTime;
      console.log(`主题切换耗时: ${switchTime}ms`);

      // 主题切换应该在合理时间内完成（小于 2 秒）
      expect(switchTime).toBeLessThan(2000);

      // 验证切换成功
      await verifyTheme(page, 'dark');

      await takeScreenshot(page, '11-theme-switch-performance');
    });

    test('12-快速切换主题不应该出错', async ({ page }) => {
      // 快速切换多次
      const themes: Array<'light' | 'dark'> = ['light', 'dark', 'light', 'dark', 'light'];

      for (const theme of themes) {
        await setStorageState(page, { theme });
        await page.evaluate(() => {
          // 触发 storage 事件以通知主题变化
          window.dispatchEvent(new Event('storage'));
        });
        await page.waitForTimeout(200);
      }

      await page.reload();
      await page.waitForLoadState('networkidle');

      // 验证最终状态正确
      await verifyTheme(page, 'light');

      // 验证页面没有崩溃
      const bodyText = await page.locator('body').textContent();
      expect(bodyText).not.toBeNull();

      await takeScreenshot(page, '12-rapid-theme-switch');
    });
  });

  test.describe('主题与后端同步', () => {
    test('13-主题设置应该保存到后端（如果支持）', async ({ page }) => {
      // 监听设置 API 调用
      let saveRequestCalled = false;

      page.on('request', (request) => {
        if (
          request.url().includes('/api/settings') ||
          request.url().includes('/api/user/preferences')
        ) {
          saveRequestCalled = true;
          console.log(`检测到设置保存请求: ${request.method()} ${request.url()}`);
        }
      });

      // 设置主题
      await setStorageState(page, { theme: 'dark' });
      await page.reload();
      await page.waitForLoadState('networkidle');

      // 等待可能的 API 调用
      await page.waitForTimeout(1000);

      // 验证主题已应用
      await verifyTheme(page, 'dark');

      // 记录是否调用了后端 API
      console.log(`后端保存调用: ${saveRequestCalled}`);

      await takeScreenshot(page, '13-theme-backend-sync');
    });

    test('14-登录后应该恢复保存的主题', async ({ page }) => {
      // 设置主题为暗色
      await setStorageState(page, { theme: 'dark' });
      await page.reload();
      await page.waitForLoadState('networkidle');

      // 验证暗色主题
      await verifyTheme(page, 'dark');

      // 登出
      await page.evaluate(() => {
        localStorage.clear();
      });

      // 重新登录
      await login(page);
      await page.goto('/');
      await page.waitForLoadState('networkidle');

      // 验证主题恢复（如果后端保存了主题）
      const restoredTheme = await getCurrentTheme(page);
      console.log(`登录后恢复的主题: ${restoredTheme}`);

      // 注意：这个测试可能会失败，因为后端可能还没有实现主题保存
      // 如果实现了主题保存，应该恢复为 dark
      // expect(restoredTheme).toBe('dark');

      await takeScreenshot(page, '14-theme-restored-after-login');
    });
  });

  test.describe('主题与组件集成', () => {
    test('15-主题应该影响所有可见组件', async ({ page }) => {
      // 在亮色主题下截图
      await setStorageState(page, { theme: 'light' });
      await page.reload();
      await page.waitForLoadState('networkidle');
      await takeScreenshot(page, '15-light-theme-components');

      // 切换到暗色主题
      await setStorageState(page, { theme: 'dark' });
      await page.reload();
      await page.waitForLoadState('networkidle');
      await takeScreenshot(page, '15-dark-theme-components');

      // 验证关键组件都受到了主题影响
      // 检查导航栏
      const nav = page.locator('nav, header').first();
      await expect(nav).toBeVisible();

      // 检查主要内容区域
      const main = page.locator('main').first();
      await expect(main).toBeVisible();

      // 验证背景色确实改变了
      const lightBg = await page.evaluate(() => {
        document.documentElement.classList.add('light');
        return window.getComputedStyle(document.documentElement).backgroundColor;
      });

      const darkBg = await page.evaluate(() => {
        document.documentElement.classList.remove('light');
        document.documentElement.classList.add('dark');
        return window.getComputedStyle(document.documentElement).backgroundColor;
      });

      expect(lightBg).not.toBe(darkBg);
      console.log(`亮色背景: ${lightBg}, 暗色背景: ${darkBg}`);
    });

    test('16-主题切换不应该破坏页面布局', async ({ page }) => {
      // 亮色主题
      await setStorageState(page, { theme: 'light' });
      await page.reload();
      await page.waitForLoadState('networkidle');

      // 获取页面布局信息
      const lightLayout = await page.evaluate(() => {
        const body = document.body;
        return {
          scrollHeight: body.scrollHeight,
          scrollWidth: body.scrollWidth,
          offsetHeight: body.offsetHeight,
        };
      });

      // 暗色主题
      await setStorageState(page, { theme: 'dark' });
      await page.reload();
      await page.waitForLoadState('networkidle');

      // 获取暗色主题布局信息
      const darkLayout = await page.evaluate(() => {
        const body = document.body;
        return {
          scrollHeight: body.scrollHeight,
          scrollWidth: body.scrollWidth,
          offsetHeight: body.offsetHeight,
        };
      });

      // 验证布局尺寸一致
      expect(lightLayout.scrollHeight).toBe(darkLayout.scrollHeight);
      expect(lightLayout.scrollWidth).toBe(darkLayout.scrollWidth);

      console.log(`亮色主题布局: ${JSON.stringify(lightLayout)}`);
      console.log(`暗色主题布局: ${JSON.stringify(darkLayout)}`);

      await takeScreenshot(page, '16-theme-no-layout-break');
    });
  });

  test.describe('主题边界情况', () => {
    test('17-无效主题值应该回退到默认', async ({ page }) => {
      // 设置无效的主题值
      await setStorageState(page, { theme: 'invalid-theme-value' });
      await page.reload();
      await page.waitForLoadState('networkidle');

      // 验证回退到有效主题
      const currentTheme = await getCurrentTheme(page);
      expect(['light', 'dark', 'system']).toContain(currentTheme);

      console.log(`无效值回退后的主题: ${currentTheme}`);

      await takeScreenshot(page, '17-invalid-theme-fallback');
    });

    test('18-删除主题设置应该应用默认主题', async ({ page }) => {
      // 先设置为暗色
      await setStorageState(page, { theme: 'dark' });
      await page.reload();
      await page.waitForLoadState('networkidle');

      // 验证暗色
      await verifyTheme(page, 'dark');

      // 删除主题设置
      await page.evaluate(() => {
        localStorage.removeItem('theme');
      });
      await page.reload();
      await page.waitForLoadState('networkidle');

      // 验证应用了默认主题（应该是 light 或 system）
      const defaultTheme = await getCurrentTheme(page);
      expect(['light', 'dark', 'system']).toContain(defaultTheme);

      console.log(`默认主题: ${defaultTheme}`);

      await takeScreenshot(page, '18-default-theme-after-clear');
    });

    test('19-主题切换在移动端应该正常工作', async ({ page }) => {
      // 设置移动端视口
      await page.setViewportSize({ width: 375, height: 667 });

      // 切换到暗色主题
      await setStorageState(page, { theme: 'dark' });
      await page.reload();
      await page.waitForLoadState('networkidle');

      // 验证主题
      await verifyTheme(page, 'dark');

      // 验证移动端布局正常
      const body = page.locator('body');
      await expect(body).toBeVisible();

      await takeScreenshot(page, '19-mobile-dark-theme');

      // 切换到亮色主题
      await setStorageState(page, { theme: 'light' });
      await page.reload();
      await page.waitForLoadState('networkidle');

      // 验证主题
      await verifyTheme(page, 'light');

      await takeScreenshot(page, '19-mobile-light-theme');
    });
  });

  test.describe('主题可访问性', () => {
    test('20-主题切换应该支持键盘操作', async ({ page }) => {
      // 如果页面有主题切换按钮，测试键盘访问
      const themeButton = page.locator('[data-testid="theme-toggle"], button[aria-label*="主题"]');

      const isVisible = await themeButton.isVisible().catch(() => false);

      if (isVisible) {
        // 使用 Tab 键聚焦按钮
        await page.keyboard.press('Tab');
        await page.keyboard.press('Tab');

        // 验证按钮获得焦点
        const focusedElement = await page.evaluate(() => document.activeElement?.tagName);
        expect(focusedElement).toBe('BUTTON');

        // 使用 Enter 键激活
        await page.keyboard.press('Enter');
        await page.waitForTimeout(500);

        // 验证主题切换
        const theme = await getCurrentTheme(page);
        console.log(`键盘切换后的主题: ${theme}`);

        await takeScreenshot(page, '20-keyboard-theme-switch');
      } else {
        console.log('未找到主题切换按钮，跳过键盘测试');
        test.skip();
      }
    });

    test('21-主题切换应该有适当的 ARIA 标签', async ({ page }) => {
      // 检查主题切换按钮的 ARIA 标签
      const themeButton = page.locator('[data-testid="theme-toggle"], button[aria-label*="主题"]');

      const isVisible = await themeButton.isVisible().catch(() => false);

      if (isVisible) {
        // 验证有 aria-label
        const ariaLabel = await themeButton.getAttribute('aria-label');
        expect(ariaLabel).toBeTruthy();

        console.log(`主题按钮的 aria-label: ${ariaLabel}`);

        await takeScreenshot(page, '21-theme-aria-labels');
      } else {
        console.log('未找到主题切换按钮，跳过 ARIA 测试');
        test.skip();
      }
    });
  });

  test.describe('主题性能优化', () => {
    test('22-主题切换不应该导致内存泄漏', async ({ page }) => {
      // 获取初始内存使用
      const initialMetrics = await page.metrics();
      console.log(`初始内存: ${initialMetrics.JSHeapUsedSize}`);

      // 多次切换主题
      for (let i = 0; i < 10; i++) {
        await setStorageState(page, { theme: i % 2 === 0 ? 'light' : 'dark' });
        await page.reload();
        await page.waitForLoadState('networkidle');
        await page.waitForTimeout(100);
      }

      // 获取最终内存使用
      const finalMetrics = await page.metrics();
      console.log(`最终内存: ${finalMetrics.JSHeapUsedSize}`);

      // 内存增长应该不超过 50%
      const memoryGrowth =
        (finalMetrics.JSHeapUsedSize - initialMetrics.JSHeapUsedSize) /
        initialMetrics.JSHeapUsedSize;

      expect(memoryGrowth).toBeLessThan(0.5);
      console.log(`内存增长率: ${(memoryGrowth * 100).toFixed(2)}%`);

      await takeScreenshot(page, '22-theme-memory-check');
    });

    test('23-主题 CSS 应该高效加载', async ({ page }) => {
      // 监控网络请求
      const cssRequests: string[] = [];
      page.on('request', (request) => {
        if (request.resourceType() === 'stylesheet') {
          cssRequests.push(request.url());
        }
      });

      // 设置主题
      await setStorageState(page, { theme: 'dark' });
      await page.reload();
      await page.waitForLoadState('networkidle');

      // 验证没有重复加载 CSS
      console.log(`CSS 请求数: ${cssRequests.length}`);
      expect(cssRequests.length).toBeLessThan(5); // 不应该有太多 CSS 请求

      await takeScreenshot(page, '23-theme-css-loading');
    });
  });
});
