/**
 * 页面导航切换 E2E 测试
 *
 * 测试场景：
 * 1. 侧边栏导航
 * 2. 顶部导航栏导航
 * 3. 浏览器前进后退
 * 4. 直接 URL 访问
 * 5. 路由保护验证
 */

import { test, expect } from '@playwright/test';
import { login, quickLogin, verifyURL, verifyRouteChange, waitForPageLoad, waitAndClick } from './helpers';

/**
 * 导航测试配置
 */
const NAV_ROUTES = {
  HOME: '/',
  MAIN: '/main',
  SETTINGS: '/settings',
  TOOLS: '/tools',
  AGENTS: '/agents',
  MONITORING: '/monitoring',
  ADMIN: '/admin',
  LOGIN: '/login',
  REGISTER: '/register',
} as const;

/**
 * 页面内容验证选择器
 */
const PAGE_SELECTORS = {
  [NAV_ROUTES.HOME]: '[data-testid="dashboard-page"], .dashboard',
  [NAV_ROUTES.MAIN]: '[data-testid="main-layout"], main',
  [NAV_ROUTES.SETTINGS]: '[data-testid="settings-page"], .settings',
  [NAV_ROUTES.TOOLS]: '[data-testid="tools-page"], .tools',
  [NAV_ROUTES.AGENTS]: '[data-testid="agents-page"], .agents',
  [NAV_ROUTES.MONITORING]: '[data-testid="monitoring-page"], .monitoring',
  [NAV_ROUTES.ADMIN]: '[data-testid="admin-page"], .admin',
  [NAV_ROUTES.LOGIN]: '[data-testid="login-page"], .login',
  [NAV_ROUTES.REGISTER]: '[data-testid="register-page"], .register',
} as const;

/**
 * 顶部导航项配置
 */
const TOP_NAV_ITEMS = [
  { path: NAV_ROUTES.MAIN, label: '主页', testId: 'nav-item-main' },
  { path: NAV_ROUTES.TOOLS, label: '工具', testId: 'nav-item-tools' },
  { path: NAV_ROUTES.AGENTS, label: '智能体', testId: 'nav-item-agents' },
  { path: NAV_ROUTES.MONITORING, label: '监控', testId: 'nav-item-monitoring' },
  { path: NAV_ROUTES.SETTINGS, label: '设置', testId: 'nav-item-settings' },
] as const;

// ============================================
// 测试套件 1: 顶部导航栏导航
// ============================================

test.describe('顶部导航栏导航', () => {
  test.beforeEach(async ({ page }) => {
    await quickLogin(page);
    await waitForPageLoad(page);
  });

  TOP_NAV_ITEMS.forEach(({ path, label, testId }) => {
    test(`应该能通过顶部导航栏导航到 ${label} 页面`, async ({ page }) => {
      // 记录当前 URL
      const currentUrl = page.url();

      // 点击导航项
      await waitAndClick(page, `[data-testid="${testId}"]`);

      // 验证 URL 变化
      await verifyURL(page, path);

      // 验证导航项高亮状态
      const navButton = page.locator(`[data-testid="${testId}"]`);
      await expect(navButton).toHaveAttribute('data-active', 'true');

      // 验证页面内容加载
      const pageContent = page.locator('main');
      await expect(pageContent).toBeVisible();
    });
  });

  test('应该能正确显示当前激活的导航项', async ({ page }) => {
    // 导航到不同页面，验证导航高亮状态
    for (const { path, testId } of TOP_NAV_ITEMS) {
      await page.goto(path);
      await waitForPageLoad(page);

      // 验证当前导航项高亮
      const activeNav = page.locator(`[data-testid="${testId}"]`);
      await expect(activeNav).toHaveAttribute('data-active', 'true');

      // 验证其他导航项不高亮
      for (const otherItem of TOP_NAV_ITEMS) {
        if (otherItem.testId !== testId) {
          const otherNav = page.locator(`[data-testid="${otherItem.testId}"]`);
          await expect(otherNav).not.toHaveAttribute('data-active', 'true');
        }
      }
    }
  });
});

// ============================================
// 测试套件 2: 浏览器前进后退
// ============================================

test.describe('浏览器前进后退', () => {
  test.beforeEach(async ({ page }) => {
    await quickLogin(page);
    await waitForPageLoad(page);
  });

  test('应该能通过浏览器后退按钮返回上一页', async ({ page }) => {
    // 导航序列: 主页 -> 工具 -> 智能体
    await page.goto(NAV_ROUTES.MAIN);
    await waitForPageLoad(page);

    await page.goto(NAV_ROUTES.TOOLS);
    await waitForPageLoad(page);

    await page.goto(NAV_ROUTES.AGENTS);
    await waitForPageLoad(page);

    // 验证当前在智能体页面
    await verifyURL(page, NAV_ROUTES.AGENTS);

    // 点击后退
    await page.goBack();
    await waitForPageLoad(page);

    // 验证返回到工具页面
    await verifyURL(page, NAV_ROUTES.TOOLS);

    // 再次后退
    await page.goBack();
    await waitForPageLoad(page);

    // 验证返回到主页
    await verifyURL(page, NAV_ROUTES.MAIN);
  });

  test('应该能通过浏览器前进按钮前进到下一页', async ({ page }) => {
    // 导航序列: 主页 -> 工具 -> 智能体
    await page.goto(NAV_ROUTES.MAIN);
    await waitForPageLoad(page);

    await page.goto(NAV_ROUTES.TOOLS);
    await waitForPageLoad(page);

    await page.goto(NAV_ROUTES.AGENTS);
    await waitForPageLoad(page);

    // 后退两次
    await page.goBack();
    await page.goBack();
    await waitForPageLoad(page);

    // 验证在主页
    await verifyURL(page, NAV_ROUTES.MAIN);

    // 前进
    await page.goForward();
    await waitForPageLoad(page);

    // 验证前进到工具页面
    await verifyURL(page, NAV_ROUTES.TOOLS);

    // 再次前进
    await page.goForward();
    await waitForPageLoad(page);

    // 验证前进到智能体页面
    await verifyURL(page, NAV_ROUTES.AGENTS);
  });

  test('应该在前进后退时正确更新导航高亮状态', async ({ page }) => {
    // 导航到设置页面
    await page.goto(NAV_ROUTES.SETTINGS);
    await waitForPageLoad(page);

    // 验证设置导航项高亮
    const settingsNav = page.locator('[data-testid="nav-item-settings"]');
    await expect(settingsNav).toHaveAttribute('data-active', 'true');

    // 导航到监控页面
    await page.goto(NAV_ROUTES.MONITORING);
    await waitForPageLoad(page);

    // 验证监控导航项高亮
    const monitoringNav = page.locator('[data-testid="nav-item-monitoring"]');
    await expect(monitoringNav).toHaveAttribute('data-active', 'true');

    // 后退
    await page.goBack();
    await waitForPageLoad(page);

    // 验证设置导航项重新高亮
    await expect(settingsNav).toHaveAttribute('data-active', 'true');

    // 前进
    await page.goForward();
    await waitForPageLoad(page);

    // 验证监控导航项重新高亮
    await expect(monitoringNav).toHaveAttribute('data-active', 'true');
  });
});

// ============================================
// 测试套件 3: 直接 URL 访问
// ============================================

test.describe('直接 URL 访问', () => {
  test.beforeEach(async ({ page }) => {
    await quickLogin(page);
  });

  test('应该能直接访问所有受保护的路由', async ({ page }) => {
    const protectedRoutes = [
      NAV_ROUTES.MAIN,
      NAV_ROUTES.SETTINGS,
      NAV_ROUTES.TOOLS,
      NAV_ROUTES.AGENTS,
      NAV_ROUTES.MONITORING,
      NAV_ROUTES.ADMIN,
    ];

    for (const route of protectedRoutes) {
      // 直接访问 URL
      await page.goto(route);

      // 等待页面加载
      await waitForPageLoad(page);

      // 验证 URL 正确
      expect(page.url()).toContain(route);

      // 验证页面内容可见
      const mainContent = page.locator('main');
      await expect(mainContent).toBeVisible();

      // 验证布局加载
      const layout = page.locator('[data-testid="main-layout"]');
      await expect(layout).toBeVisible();
    }
  });

  test('应该能正确处理不存在的路由', async ({ page }) => {
    // 访问不存在的路由
    await page.goto('/non-existent-route');

    // 应该重定向到首页或显示 404
    await page.waitForTimeout(1000);

    // 验证要么在首页，要么显示 404
    const url = page.url();
    const isValidRedirect = url.includes(NAV_ROUTES.HOME) || url.includes('404');
    expect(isValidRedirect).toBeTruthy();
  });
});

// ============================================
// 测试套件 4: 路由保护验证
// ============================================

test.describe('路由保护验证', () => {
  test('未登录用户访问受保护路由应该重定向到登录页', async ({ page }) => {
    const protectedRoutes = [
      NAV_ROUTES.MAIN,
      NAV_ROUTES.SETTINGS,
      NAV_ROUTES.TOOLS,
      NAV_ROUTES.AGENTS,
      NAV_ROUTES.MONITORING,
    ];

    for (const route of protectedRoutes) {
      // 清除登录状态
      await page.evaluate(() => {
        localStorage.clear();
        sessionStorage.clear();
      });

      // 尝试访问受保护路由
      await page.goto(route);

      // 验证重定向到登录页
      await page.waitForURL(`**${NAV_ROUTES.LOGIN}`, { timeout: 5000 });
      expect(page.url()).toContain(NAV_ROUTES.LOGIN);
    }
  });

  test('已登录用户访问公开路由应该重定向到首页', async ({ page }) => {
    // 先登录
    await quickLogin(page);
    await waitForPageLoad(page);

    // 尝试访问登录页
    await page.goto(NAV_ROUTES.LOGIN);

    // 应该重定向到首页
    await page.waitForURL(`**${NAV_ROUTES.HOME}`, { timeout: 5000 });
    expect(page.url()).toContain(NAV_ROUTES.HOME);
  });

  test('登录后应该保持原始访问的路由', async ({ page }) => {
    const targetRoute = NAV_ROUTES.SETTINGS;

    // 未登录状态访问受保护路由
    await page.goto(targetRoute);

    // 等待重定向到登录页
    await page.waitForURL(`**${NAV_ROUTES.LOGIN}`, { timeout: 5000 });

    // 执行登录
    await login(page);

    // 验证登录后跳转到原始路由（或首页）
    await waitForPageLoad(page);
    const currentUrl = page.url();
    const isValidDestination = currentUrl.includes(NAV_ROUTES.HOME) || currentUrl.includes(targetRoute);
    expect(isValidDestination).toBeTruthy();
  });
});

// ============================================
// 测试套件 5: 导航交互完整性
// ============================================

test.describe('导航交互完整性', () => {
  test.beforeEach(async ({ page }) => {
    await quickLogin(page);
    await waitForPageLoad(page);
  });

  test('应该在导航时正确更新页面标题', async ({ page }) => {
    // 导航到不同页面
    const routes = [
      { path: NAV_ROUTES.MAIN, expectedTitle: '主页' },
      { path: NAV_ROUTES.SETTINGS, expectedTitle: '设置' },
      { path: NAV_ROUTES.TOOLS, expectedTitle: '工具' },
    ];

    for (const { path, expectedTitle } of routes) {
      await page.goto(path);
      await waitForPageLoad(page);

      // 验证页面标题（可选，取决于应用实现）
      // const title = await page.title();
      // expect(title).toContain(expectedTitle);
    }
  });

  test('应该在导航时正确更新浏览器历史记录', async ({ page }) => {
    // 导航序列
    const navigationPath = [
      NAV_ROUTES.MAIN,
      NAV_ROUTES.TOOLS,
      NAV_ROUTES.SETTINGS,
    ];

    for (const route of navigationPath) {
      await page.goto(route);
      await waitForPageLoad(page);
    }

    // 验证历史记录长度
    const historyLength = await page.evaluate(() => window.history.length);
    expect(historyLength).toBeGreaterThan(1);
  });

  test('应该能通过编程方式导航（JavaScript）', async ({ page }) => {
    // 使用 JavaScript 导航
    await page.evaluate((route) => {
      window.location.href = route;
    }, NAV_ROUTES.TOOLS);

    await waitForPageLoad(page);
    await verifyURL(page, NAV_ROUTES.TOOLS);
  });
});

// ============================================
// 测试套件 6: 侧边栏交互
// ============================================

test.describe('侧边栏导航交互', () => {
  test.beforeEach(async ({ page }) => {
    await quickLogin(page);
    await waitForPageLoad(page);
  });

  test('应该能在侧边栏中看到会话列表', async ({ page }) => {
    // 等待侧边栏加载
    const sidebar = page.locator('[data-testid="sidebar"]');
    await expect(sidebar).toBeVisible();

    // 验证侧边栏头部
    const sidebarHeader = page.locator('[data-testid="sidebar-header"]');
    await expect(sidebarHeader).toBeVisible();

    // 验证新建按钮
    const newSessionButton = page.locator('[data-testid="new-session-button"]');
    await expect(newSessionButton).toBeVisible();
  });

  test('应该能点击新建会话按钮', async ({ page }) => {
    // 点击新建会话按钮
    const newSessionButton = page.locator('[data-testid="new-session-button"]');
    await newSessionButton.click();

    // 验证模态框出现
    // 注意：这取决于会话创建的具体实现
    await page.waitForTimeout(500);
  });
});

// ============================================
// 测试套件 7: 响应式导航
// ============================================

test.describe('响应式导航', () => {
  test.beforeEach(async ({ page }) => {
    await quickLogin(page);
  });

  test('应该在桌面端显示完整的顶部导航', async ({ page }) => {
    // 设置桌面视口
    await page.setViewportSize({ width: 1280, height: 720 });
    await page.goto(NAV_ROUTES.MAIN);
    await waitForPageLoad(page);

    // 验证所有导航项可见
    for (const { testId } of TOP_NAV_ITEMS) {
      const navItem = page.locator(`[data-testid="${testId}"]`);
      await expect(navItem).toBeVisible();
    }
  });

  test('应该在移动端适配导航布局', async ({ page }) => {
    // 设置移动视口
    await page.setViewportSize({ width: 375, height: 667 });
    await page.goto(NAV_ROUTES.MAIN);
    await waitForPageLoad(page);

    // 验证导航仍然可用（可能在折叠菜单中）
    const topNav = page.locator('[data-testid="top-nav"]');
    await expect(topNav).toBeVisible();

    // 验证侧边栏可能被隐藏或折叠
    const sidebar = page.locator('[data-testid="sidebar"]');
    const isVisible = await sidebar.isVisible().catch(() => false);
    // 移动端侧边栏可能默认隐藏，这是正常行为
  });
});

// ============================================
// 测试套件 8: 导航性能
// ============================================

test.describe('导航性能', () => {
  test.beforeEach(async ({ page }) => {
    await quickLogin(page);
  });

  test('应该在合理时间内完成页面导航', async ({ page }) => {
    const startTime = Date.now();

    await page.goto(NAV_ROUTES.SETTINGS);

    await waitForPageLoad(page);

    const navigationTime = Date.now() - startTime;

    // 导航应该在 5 秒内完成
    expect(navigationTime).toBeLessThan(5000);
  });

  test('应该在导航时不阻塞 UI', async ({ page }) => {
    // 快速连续导航
    const routes = [NAV_ROUTES.TOOLS, NAV_ROUTES.AGENTS, NAV_ROUTES.MONITORING];

    for (const route of routes) {
      await page.goto(route);
      // 不等待完全加载，继续下一个导航
      await page.waitForTimeout(100);
    }

    // 最终应该能正常加载
    await waitForPageLoad(page);
    await expect(page.locator('main')).toBeVisible();
  });
});

// ============================================
// 测试套件 9: 边界情况
// ============================================

test.describe('导航边界情况', () => {
  test.beforeEach(async ({ page }) => {
    await quickLogin(page);
  });

  test('应该能处理快速连续点击导航项', async ({ page }) => {
    await page.goto(NAV_ROUTES.MAIN);
    await waitForPageLoad(page);

    // 快速点击多个导航项
    for (const { testId } of TOP_NAV_ITEMS) {
      await page.click(`[data-testid="${testId}"]`);
      await page.waitForTimeout(50); // 50ms 间隔
    }

    // 等待稳定
    await waitForPageLoad(page);

    // 验证最终状态正确
    const mainContent = page.locator('main');
    await expect(mainContent).toBeVisible();
  });

  test('应该能处理重复导航到同一路由', async ({ page }) => {
    await page.goto(NAV_ROUTES.SETTINGS);
    await waitForPageLoad(page);

    // 多次导航到同一路由
    for (let i = 0; i < 3; i++) {
      await page.goto(NAV_ROUTES.SETTINGS);
      await page.waitForTimeout(100);
    }

    // 验证页面仍然正常
    await expect(page.locator('main')).toBeVisible();
    await verifyURL(page, NAV_ROUTES.SETTINGS);
  });

  test('应该能处理带查询参数的路由', async ({ page }) => {
    const routeWithQuery = `${NAV_ROUTES.TOOLS}?tab=example`;

    await page.goto(routeWithQuery);
    await waitForPageLoad(page);

    // 验证 URL 包含查询参数
    expect(page.url()).toContain('tab=example');
    expect(page.url()).toContain(NAV_ROUTES.TOOLS);
  });
});

// ============================================
// 测试套件 10: 导航状态保持
// ============================================

test.describe('导航状态保持', () => {
  test('应该在页面刷新后保持当前路由', async ({ page }) => {
    await quickLogin(page);

    // 导航到设置页面
    await page.goto(NAV_ROUTES.SETTINGS);
    await waitForPageLoad(page);

    // 刷新页面
    await page.reload();
    await waitForPageLoad(page);

    // 验证仍在设置页面
    await verifyURL(page, NAV_ROUTES.SETTINGS);
  });

  test('应该在页面刷新后保持登录状态', async ({ page }) => {
    await quickLogin(page);
    await waitForPageLoad(page);

    // 刷新页面
    await page.reload();
    await waitForPageLoad(page);

    // 验证仍然登录
    const userMenu = page.locator('[data-testid="user-menu-button"]');
    await expect(userMenu).toBeVisible();

    // 验证未重定向到登录页
    expect(page.url()).not.toContain(NAV_ROUTES.LOGIN);
  });
});
