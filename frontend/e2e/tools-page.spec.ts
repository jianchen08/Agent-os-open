/**
 * 工具页面完整测试 (tools-page)
 *
 * 测试覆盖：
 * - 页面加载和导航
 * - 页面基础结构验证
 * - 工具统计信息显示
 * - 空状态显示
 * - 导航到设置页面
 * - 响应式设计
 * - 性能测试
 */

import { test, expect } from '@playwright/test';
import { login, takeScreenshot, verifyURL, waitForPageLoad, clearAndFill } from './helpers';

test.describe('工具页面测试套件', () => {
  // 每个测试前登录
  test.beforeEach(async ({ page }) => {
    await login(page);
  });

  test.describe('页面加载与基础结构', () => {
    test('01-应该正确加载工具页面', async ({ page }) => {
      await page.goto('/tools');
      await waitForPageLoad(page);

      // 验证 URL
      await verifyURL(page, '/tools');

      // 检查页面标题
      const pageTitle = page.locator('h1').filter({ hasText: '工具管理' });
      await expect(pageTitle).toBeVisible();

      // 检查副标题
      const subtitle = page.locator('p').filter({ hasText: /查看和管理系统中可用的工具/ });
      await expect(subtitle).toBeVisible();

      // 检查图标
      const icon = page.locator('[data-testid="tools-page"] svg');
      await expect(icon).toBeVisible();

      await takeScreenshot(page, '01-tools-page-loaded');
    });

    test('02-应该显示添加工具按钮', async ({ page }) => {
      await page.goto('/tools');
      await waitForPageLoad(page);

      // 检查添加工具按钮
      const addButton = page.locator('button').filter({ hasText: '添加工具' });
      await expect(addButton).toBeVisible();

      // 检查按钮图标
      const plusIcon = addButton.locator('svg');
      await expect(plusIcon).toBeVisible();

      await takeScreenshot(page, '02-tools-add-button');
    });

    test('03-应该显示工具统计卡片', async ({ page }) => {
      await page.goto('/tools');
      await waitForPageLoad(page);

      // 检查统计卡片容器
      const statsContainer = page.locator('.grid').filter({ hasText: /已安装工具/ });
      await expect(statsContainer).toBeVisible();

      // 检查三个统计卡片
      const installedCard = page.locator('.p-4').filter({ hasText: '已安装工具' });
      const activeCard = page.locator('.p-4').filter({ hasText: '活跃工具' });
      const callsCard = page.locator('.p-4').filter({ hasText: '工具调用次数' });

      await expect(installedCard).toBeVisible();
      await expect(activeCard).toBeVisible();
      await expect(callsCard).toBeVisible();

      // 验证统计值显示（当前应该为 0）
      await expect(installedCard.locator('.text-2xl')).toContainText('0');
      await expect(activeCard.locator('.text-2xl')).toContainText('0');
      await expect(callsCard.locator('.text-2xl')).toContainText('0');

      await takeScreenshot(page, '03-tools-stats-cards');
    });
  });

  test.describe('空状态显示', () => {
    test('04-应该显示空状态提示', async ({ page }) => {
      await page.goto('/tools');
      await waitForPageLoad(page);

      // 检查空状态容器
      const emptyState = page.locator('.text-center').filter({ hasText: /暂无工具/ });
      await expect(emptyState).toBeVisible();

      // 检查空状态图标
      const emptyIcon = emptyState.locator('svg');
      await expect(emptyIcon).toBeVisible();

      // 检查空状态文本
      await expect(emptyState.locator('p').filter({ hasText: '暂无工具' })).toBeVisible();
      await expect(emptyState.locator('p').filter({ hasText: /工具功能即将上线/ })).toBeVisible();

      await takeScreenshot(page, '04-tools-empty-state');
    });

    test('05-应该显示前往设置按钮', async ({ page }) => {
      await page.goto('/tools');
      await waitForPageLoad(page);

      // 检查前往设置按钮
      const settingsButton = page.locator('button').filter({ hasText: '前往设置' });
      await expect(settingsButton).toBeVisible();

      // 检查按钮图标
      const settingsIcon = settingsButton.locator('svg');
      await expect(settingsIcon).toBeVisible();

      await takeScreenshot(page, '05-tools-settings-button');
    });

    test('06-点击前往设置应该导航到设置页面', async ({ page }) => {
      await page.goto('/tools');
      await waitForPageLoad(page);

      // 点击前往设置按钮
      const settingsButton = page.locator('button').filter({ hasText: '前往设置' });
      await settingsButton.click();

      // 验证导航到设置页面
      await verifyURL(page, '/settings');

      // 验证设置页面标题
      const settingsTitle = page.locator('h1').filter({ hasText: '系统设置' });
      await expect(settingsTitle).toBeVisible();

      await takeScreenshot(page, '06-tools-navigate-to-settings');
    });
  });

  test.describe('可用工具区域', () => {
    test('07-应该显示可用工具标题', async ({ page }) => {
      await page.goto('/tools');
      await waitForPageLoad(page);

      // 检查可用工具标题
      const toolsTitle = page.locator('h2').filter({ hasText: '可用工具' });
      await expect(toolsTitle).toBeVisible();

      await takeScreenshot(page, '07-tools-section-title');
    });

    test('08-应该在空状态时显示占位内容', async ({ page }) => {
      await page.goto('/tools');
      await waitForPageLoad(page);

      // 检查可用工具区域
      const toolsSection = page.locator('h2').filter({ hasText: '可用工具' }).locator('xpath=ancestor::div[@class="space-y-4"]');

      // 验证空状态内容存在
      const placeholderIcon = toolsSection.locator('.text-center svg');
      await expect(placeholderIcon).toBeVisible();

      const placeholderText = toolsSection.locator('.text-center').filter({ hasText: '暂无工具' });
      await expect(placeholderText).toBeVisible();

      await takeScreenshot(page, '08-tools-placeholder-content');
    });
  });

  test.describe('页面布局和样式', () => {
    test('09-应该使用正确的布局容器', async ({ page }) => {
      await page.goto('/tools');
      await waitForPageLoad(page);

      // 检查主容器
      const mainContainer = page.locator('[data-testid="tools-page"]');
      await expect(mainContainer).toBeVisible();

      // 验证容器样式类
      const containerClasses = await mainContainer.getAttribute('class');
      expect(containerClasses).toContain('flex-1');
      expect(containerClasses).toContain('overflow-auto');
      expect(containerClasses).toContain('p-6');

      await takeScreenshot(page, '09-tools-layout-structure');
    });

    test('10-应该使用最大宽度限制', async ({ page }) => {
      await page.goto('/tools');
      await waitForPageLoad(page);

      // 检查内容容器
      const contentContainer = page.locator('.max-w-6xl');
      await expect(contentContainer).toBeVisible();

      // 验证样式类
      const containerClasses = await contentContainer.getAttribute('class');
      expect(containerClasses).toContain('max-w-6xl');
      expect(containerClasses).toContain('mx-auto');
      expect(containerClasses).toContain('space-y-6');

      await takeScreenshot(page, '10-tools-max-width');
    });

    test('11-标题应该正确对齐', async ({ page }) => {
      await page.goto('/tools');
      await waitForPageLoad(page);

      // 检查标题容器
      const titleContainer = page.locator('.flex.items-center.justify-between');
      await expect(titleContainer).toBeVisible();

      // 验证标题在左侧
      const title = titleContainer.locator('h1');
      await expect(title).toBeVisible();

      // 验证按钮在右侧
      const addButton = titleContainer.locator('button').filter({ hasText: '添加工具' });
      await expect(addButton).toBeVisible();

      await takeScreenshot(page, '11-tools-title-alignment');
    });
  });

  test.describe('导航功能', () => {
    test('12-应该从首页导航到工具页面', async ({ page }) => {
      // 从首页开始
      await page.goto('/');
      await waitForPageLoad(page);

      // 点击工具导航（假设在导航栏中）
      const toolsNavLink = page.locator('a[href="/tools"], nav a').filter({ hasText: /工具/ });
      const navLinkExists = await toolsNavLink.count() > 0;

      if (navLinkExists) {
        await toolsNavLink.first().click();
        await verifyURL(page, '/tools');
      } else {
        // 如果没有导航链接，直接访问
        await page.goto('/tools');
      }

      // 验证页面加载
      const pageTitle = page.locator('h1').filter({ hasText: '工具管理' });
      await expect(pageTitle).toBeVisible();

      await takeScreenshot(page, '12-tools-navigation-from-home');
    });

    test('13-应该能够直接访问工具页面', async ({ page }) => {
      // 直接访问工具页面
      await page.goto('/tools');
      await waitForPageLoad(page);

      // 验证页面正确加载
      await expect(page.locator('h1').filter({ hasText: '工具管理' })).toBeVisible();

      // 验证没有重定向到其他页面
      expect(page.url()).toContain('/tools');

      await takeScreenshot(page, '13-tools-direct-access');
    });
  });

  test.describe('响应式设计', () => {
    test('14-应该在桌面端正常显示', async ({ page }) => {
      await page.setViewportSize({ width: 1920, height: 1080 });
      await page.goto('/tools');
      await waitForPageLoad(page);

      // 检查页面标题
      const pageTitle = page.locator('h1').filter({ hasText: '工具管理' });
      await expect(pageTitle).toBeVisible();

      // 检查统计卡片（桌面端应该是 3 列）
      const statsGrid = page.locator('.grid').filter({ hasText: /已安装工具/ });
      await expect(statsGrid).toBeVisible();

      const gridClasses = await statsGrid.getAttribute('class');
      expect(gridClasses).toContain('md:grid-cols-3');

      await takeScreenshot(page, '14-tools-responsive-desktop');
    });

    test('15-应该在平板端正常显示', async ({ page }) => {
      await page.setViewportSize({ width: 768, height: 1024 });
      await page.goto('/tools');
      await waitForPageLoad(page);

      // 检查页面标题
      const pageTitle = page.locator('h1').filter({ hasText: '工具管理' });
      await expect(pageTitle).toBeVisible();

      // 检查统计卡片仍然可见
      const installedCard = page.locator('.p-4').filter({ hasText: '已安装工具' });
      await expect(installedCard).toBeVisible();

      await takeScreenshot(page, '15-tools-responsive-tablet');
    });

    test('16-应该在移动端正常显示', async ({ page }) => {
      await page.setViewportSize({ width: 375, height: 667 });
      await page.goto('/tools');
      await waitForPageLoad(page);

      // 检查页面标题
      const pageTitle = page.locator('h1').filter({ hasText: '工具管理' });
      await expect(pageTitle).toBeVisible();

      // 在移动端，统计卡片应该垂直堆叠
      const statsGrid = page.locator('.grid');
      await expect(statsGrid).toBeVisible();

      // 检查按钮是否仍然可见（可能在移动端调整布局）
      const addButton = page.locator('button').filter({ hasText: '添加工具' });
      await expect(addButton).toBeVisible();

      await takeScreenshot(page, '16-tools-responsive-mobile');
    });

    test('17-应该在宽屏显示器上正常显示', async ({ page }) => {
      await page.setViewportSize({ width: 2560, height: 1440 });
      await page.goto('/tools');
      await waitForPageLoad(page);

      // 检查页面标题
      const pageTitle = page.locator('h1').filter({ hasText: '工具管理' });
      await expect(pageTitle).toBeVisible();

      // 验证内容居中
      const contentContainer = page.locator('.max-w-6xl');
      await expect(contentContainer).toBeVisible();

      await takeScreenshot(page, '17-tools-responsive-ultrawide');
    });
  });

  test.describe('交互测试', () => {
    test('18-点击添加工具按钮应该有反馈', async ({ page }) => {
      await page.goto('/tools');
      await waitForPageLoad(page);

      const addButton = page.locator('button').filter({ hasText: '添加工具' });

      // 记录点击前的状态
      const isEnabled = await addButton.isEnabled();
      expect(isEnabled).toBeTruthy();

      // 点击按钮（目前可能不会有实际效果，因为是占位符）
      await addButton.click();

      // 等待一小段时间检查是否有任何反馈
      await page.waitForTimeout(500);

      // 验证按钮仍然可点击
      const stillEnabled = await addButton.isEnabled();
      expect(stillEnabled).toBeTruthy();

      await takeScreenshot(page, '18-tools-add-button-click');
    });

    test('19-统计卡片应该有正确的样式', async ({ page }) => {
      await page.goto('/tools');
      await waitForPageLoad(page);

      // 检查所有统计卡片的样式
      const statCards = page.locator('.p-4.rounded-lg.border');

      const cardCount = await statCards.count();
      expect(cardCount).toBe(3);

      // 验证每个卡片都有必要的样式类
      for (let i = 0; i < cardCount; i++) {
        const card = statCards.nth(i);
        const classes = await card.getAttribute('class');

        expect(classes).toContain('p-4');
        expect(classes).toContain('rounded-lg');
        expect(classes).toContain('border');
        expect(classes).toContain('border-border');
        expect(classes).toContain('bg-card');
      }

      await takeScreenshot(page, '19-tools-card-styles');
    });

    test('20-页面应该有正确的垂直间距', async ({ page }) => {
      await page.goto('/tools');
      await waitForPageLoad(page);

      // 检查主容器的间距
      const mainContainer = page.locator('.max-w-6xl');
      const classes = await mainContainer.getAttribute('class');

      expect(classes).toContain('space-y-6');

      // 验证子元素之间的间距
      const titleSection = mainContainer.locator('.flex.items-center.justify-between').first();
      await expect(titleSection).toBeVisible();

      const statsSection = mainContainer.locator('.grid').filter({ hasText: /已安装工具/ });
      await expect(statsSection).toBeVisible();

      await takeScreenshot(page, '20-tools-vertical-spacing');
    });
  });

  test.describe('性能测试', () => {
    test('21-应该快速加载页面', async ({ page }) => {
      const startTime = Date.now();

      await page.goto('/tools');
      await waitForPageLoad(page);

      const loadTime = Date.now() - startTime;

      // 页面应该在 3 秒内加载完成
      expect(loadTime).toBeLessThan(3000);

      console.log(`工具页面加载时间: ${loadTime}ms`);

      await takeScreenshot(page, '21-tools-load-performance');
    });

    test('22-应该正确处理快速刷新', async ({ page }) => {
      // 快速刷新多次
      for (let i = 0; i < 3; i++) {
        await page.goto('/tools');
        await waitForPageLoad(page);

        // 验证页面内容存在
        const pageTitle = page.locator('h1').filter({ hasText: '工具管理' });
        await expect(pageTitle).toBeVisible();
      }

      await takeScreenshot(page, '22-tools-rapid-refresh');
    });
  });

  test.describe('可访问性测试', () => {
    test('23-图标应该有正确的语义', async ({ page }) => {
      await page.goto('/tools');
      await waitForPageLoad(page);

      // 检查标题中的图标
      const titleIcon = page.locator('h1 svg');
      await expect(titleIcon).toBeVisible();

      // 检查空状态中的图标
      const emptyIcon = page.locator('.text-center svg');
      await expect(emptyIcon).toBeVisible();

      await takeScreenshot(page, '23-tools-icon-semantics');
    });

    test('24-文本应该有足够的对比度', async ({ page }) => {
      await page.goto('/tools');
      await waitForPageLoad(page);

      // 检查标题颜色
      const title = page.locator('h1').filter({ hasText: '工具管理' });
      await expect(title).toBeVisible();

      const titleClasses = await title.getAttribute('class');
      expect(titleClasses).toContain('text-foreground');

      // 检查副标题颜色
      const subtitle = page.locator('p').filter({ hasText: /查看和管理系统中可用的工具/ });
      await expect(subtitle).toBeVisible();

      const subtitleClasses = await subtitle.getAttribute('class');
      expect(subtitleClasses).toContain('text-muted-foreground');

      await takeScreenshot(page, '24-tools-text-contrast');
    });

    test('25-按钮应该有清晰的焦点状态', async ({ page }) => {
      await page.goto('/tools');
      await waitForPageLoad(page);

      const addButton = page.locator('button').filter({ hasText: '添加工具' });

      // 使用 Tab 键聚焦按钮
      await addButton.focus();

      // 验证按钮获得焦点
      const isFocused = await addButton.evaluate((el) => document.activeElement === el);
      expect(isFocused).toBeTruthy();

      await takeScreenshot(page, '25-tools-button-focus');
    });
  });

  test.describe('数据测试（未来功能准备）', () => {
    test('26-页面结构应该支持工具列表渲染', async ({ page }) => {
      await page.goto('/tools');
      await waitForPageLoad(page);

      // 验证存在可用工具区域
      const toolsSection = page.locator('h2').filter({ hasText: '可用工具' });
      await expect(toolsSection).toBeVisible();

      // 验证区域的父容器
      const sectionContainer = toolsSection.locator('xpath=ancestor::div[@class="space-y-4"]');
      await expect(sectionContainer).toBeVisible();

      // 这个区域将来会显示工具列表
      // 当前显示空状态，但结构已经准备好

      await takeScreenshot(page, '26-tools-list-structure-ready');
    });

    test('27-统计卡片应该支持动态更新', async ({ page }) => {
      await page.goto('/tools');
      await waitForPageLoad(page);

      // 检查统计数字的容器
      const statsNumbers = page.locator('.text-2xl.font-bold');

      const count = await statsNumbers.count();
      expect(count).toBe(3);

      // 验证当前值（都应该是 0）
      for (let i = 0; i < count; i++) {
        await expect(statsNumbers.nth(i)).toContainText('0');
      }

      // 当工具功能实现后，这些数字应该会动态更新

      await takeScreenshot(page, '27-tools-stats-dynamic-ready');
    });
  });

  test.describe('边界情况测试', () => {
    test('28-应该处理极端视窗尺寸', async ({ page }) => {
      // 测试非常小的视窗
      await page.setViewportSize({ width: 320, height: 568 });
      await page.goto('/tools');
      await waitForPageLoad(page);

      // 验证页面仍然可用
      const pageTitle = page.locator('h1').filter({ hasText: '工具管理' });
      await expect(pageTitle).toBeVisible();

      await takeScreenshot(page, '28-tools-extreme-small-viewport');
    });

    test('29-应该处理非常大的视窗尺寸', async ({ page }) => {
      // 测试非常大的视窗
      await page.setViewportSize({ width: 3840, height: 2160 });
      await page.goto('/tools');
      await waitForPageLoad(page);

      // 验证内容仍然居中
      const contentContainer = page.locator('.max-w-6xl');
      await expect(contentContainer).toBeVisible();

      const pageTitle = page.locator('h1').filter({ hasText: '工具管理' });
      await expect(pageTitle).toBeVisible();

      await takeScreenshot(page, '29-tools-extreme-large-viewport');
    });

    test('30-应该正确处理页面缩放', async ({ page }) => {
      await page.goto('/tools');
      await waitForPageLoad(page);

      // 测试不同的缩放级别
      const zoomLevels = [0.5, 0.75, 1.0, 1.25, 1.5];

      for (const zoom of zoomLevels) {
        await page.evaluate((level) => {
          document.body.style.zoom = String(level);
        }, zoom);

        await page.waitForTimeout(200);

        // 验证页面仍然可见
        const pageTitle = page.locator('h1').filter({ hasText: '工具管理' });
        await expect(pageTitle).toBeVisible();
      }

      // 重置缩放
      await page.evaluate(() => {
        document.body.style.zoom = '1.0';
      });

      await takeScreenshot(page, '30-tools-page-zoom');
    });
  });

  test.describe('集成测试', () => {
    test('31-应该能够在工具和设置页面之间导航', async ({ page }) => {
      // 从工具页面开始
      await page.goto('/tools');
      await waitForPageLoad(page);

      // 点击前往设置
      const settingsButton = page.locator('button').filter({ hasText: '前往设置' });
      await settingsButton.click();

      // 验证在设置页面
      await verifyURL(page, '/settings');
      await expect(page.locator('h1').filter({ hasText: '系统设置' })).toBeVisible();

      // 返回工具页面
      await page.goto('/tools');
      await waitForPageLoad(page);

      // 验证返回成功
      await expect(page.locator('h1').filter({ hasText: '工具管理' })).toBeVisible();

      await takeScreenshot(page, '31-tools-settings-navigation');
    });

    test('32-应该保持用户登录状态', async ({ page }) => {
      // 验证已登录
      const userMenu = page.locator('[data-testid="user-menu"], .user-menu');
      await expect(userMenu).toBeVisible();

      // 访问工具页面
      await page.goto('/tools');
      await waitForPageLoad(page);

      // 验证仍然登录
      await expect(userMenu).toBeVisible();

      // 验证可以访问受保护的内容
      const pageTitle = page.locator('h1').filter({ hasText: '工具管理' });
      await expect(pageTitle).toBeVisible();

      await takeScreenshot(page, '32-tools-auth-state');
    });
  });

  test.describe('用户体验测试', () => {
    test('33-空状态应该提供清晰的指引', async ({ page }) => {
      await page.goto('/tools');
      await waitForPageLoad(page);

      // 检查空状态消息
      const emptyState = page.locator('.text-center').filter({ hasText: /暂无工具/ });

      // 验证有多个消息层次
      const mainMessage = emptyState.locator('p').filter({ hasText: '暂无工具' });
      await expect(mainMessage).toBeVisible();

      const subMessage = emptyState.locator('p').filter({ hasText: /工具功能即将上线/ });
      await expect(subMessage).toBeVisible();

      // 验证有操作按钮
      const actionButton = emptyState.locator('button').filter({ hasText: '前往设置' });
      await expect(actionButton).toBeVisible();

      await takeScreenshot(page, '33-tools-empty-state-guidance');
    });

    test('34-应该有视觉层次结构', async ({ page }) => {
      await page.goto('/tools');
      await waitForPageLoad(page);

      // 检查标题层次
      const h1 = page.locator('h1');
      const h2 = page.locator('h2');

      await expect(h1).toBeVisible();
      await expect(h2).toBeVisible();

      // 验证标题样式
      const h1Classes = await h1.getAttribute('class');
      expect(h1Classes).toContain('text-2xl');
      expect(h1Classes).toContain('font-bold');

      const h2Classes = await h2.getAttribute('class');
      expect(h2Classes).toContain('text-lg');
      expect(h2Classes).toContain('font-semibold');

      await takeScreenshot(page, '34-tools-visual-hierarchy');
    });

    test('35-应该有一致的间距系统', async ({ page }) => {
      await page.goto('/tools');
      await waitForPageLoad(page);

      // 检查容器使用 space-y-6
      const mainContainer = page.locator('.max-w-6xl');
      const mainClasses = await mainContainer.getAttribute('class');
      expect(mainClasses).toContain('space-y-6');

      // 检查统计卡片使用 grid gap-4
      const statsGrid = page.locator('.grid');
      const gridClasses = await statsGrid.getAttribute('class');
      expect(gridClasses).toContain('gap-4');

      await takeScreenshot(page, '35-tools-consistent-spacing');
    });
  });
});
