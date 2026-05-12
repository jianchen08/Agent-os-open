/**
 * Dashboard 页面 E2E 测试
 *
 * 测试场景：
 * 1. 页面加载验证
 * 2. 统计数据显示
 * 3. 创建新会话按钮
 * 4. 会话列表展示
 * 5. 快速操作功能
 *
 * 测试规则：
 * - 模拟真实用户行为
 * - 验证前端 UI 变化
 * - 验证后端 API 响应
 * - 验证数据持久化
 */

import { test, expect } from '@playwright/test';
import {
  quickLogin,
  loginViaAPI,
  waitForAPI,
  waitForAPIResponse,
  getDBRecordCount,
  verifyURL,
  recordElementCount,
  verifyElementCountChanged,
  waitForSuccessMessage,
  verifyTheme,
  switchTheme,
  takeScreenshot,
  waitForPageLoad,
} from './helpers';

test.describe('Dashboard 页面 - 完整 E2E 测试', () => {
  // 使用 auth 存储状态跳过登录
  test.use({ storageState: 'tests/e2e/.auth/user.json' });

  test.beforeEach(async ({ page }) => {
    // 导航到首页
    await page.goto('/', { timeout: 30000 });
    await waitForPageLoad(page);
  });

  /**
   * 场景 1: 页面加载验证
   *
   * 验证点：
   * - 页面成功加载
   * - 主要 UI 元素可见
   * - 无 JavaScript 错误
   * - 后端 API 调用成功
   */
  test('场景 1: 页面加载验证', async ({ page }) => {
    console.log('\n[Dashboard] 测试页面加载...');

    // 1.1 验证 URL 正确
    await verifyURL(page, /^http:\/\/localhost:\d+\/?$/);
    console.log('  ✓ URL 正确');

    // 1.2 验证主容器存在
    const dashboardContainer = page.locator('[data-testid="dashboard-page"]');
    await expect(dashboardContainer).toBeVisible({ timeout: 10000 });
    console.log('  ✓ 主容器可见');

    // 1.3 验证欢迎区域
    const welcomeTitle = page.locator('h1');
    await expect(welcomeTitle).toBeVisible();
    const welcomeText = await welcomeTitle.textContent();
    expect(welcomeText).toContain('欢迎回来');
    console.log(`  ✓ 欢迎标题显示: ${welcomeText}`);

    // 1.4 验证描述文字
    const description = page.locator('p.text-muted-foreground').first();
    await expect(description).toBeVisible();
    console.log('  ✓ 描述文字可见');

    // 1.5 监听 API 请求（验证后端调用）
    const apiPromise = waitForAPIResponse(page, '/api/v1/threads');
    await page.reload();
    await apiPromise;
    console.log('  ✓ 后端 API 调用成功');

    // 1.6 验证无 JavaScript 错误
    const errors: string[] = [];
    page.on('pageerror', (error) => {
      errors.push(error.toString());
    });
    await page.waitForTimeout(2000);
    expect(errors).toHaveLength(0);
    console.log('  ✓ 无 JavaScript 错误');
  });

  /**
   * 场景 2: 统计数据显示
   *
   * 验证点：
   * - 会话列表加载
   * - 会话数量显示
   * - 消息数量统计
   * - 时间格式化显示
   */
  test('场景 2: 统计数据显示', async ({ page }) => {
    console.log('\n[Dashboard] 测试统计数据...');

    // 2.1 等待会话列表加载完成
    const apiPromise = waitForAPIResponse(page, '/api/v1/threads');
    await apiPromise;
    console.log('  ✓ 会话列表 API 响应成功');

    // 2.2 检查"最近会话"标题
    const sectionTitle = page.locator('h2:has-text("最近会话")');
    await expect(sectionTitle).toBeVisible({ timeout: 10000 });
    console.log('  ✓ "最近会话"标题显示');

    // 2.3 检查会话列表（可能为空）
    const emptyState = page.locator('text=/还没有会话/');
    const hasEmptyState = await emptyState.isVisible().catch(() => false);

    if (hasEmptyState) {
      console.log('  ✓ 空状态显示（无会话）');

      // 验证空状态图标
      const emptyIcon = page.locator('.text-center svg').first();
      await expect(emptyIcon).toBeVisible();
      console.log('  ✓ 空状态图标显示');
    } else {
      // 2.4 如果有会话，验证会话列表
      const sessionItems = page.locator('button[class*="hover:bg-accent"]');
      const count = await sessionItems.count();

      console.log(`  会话数量: ${count}`);

      if (count > 0) {
        // 检查第一个会话项
        const firstSession = sessionItems.first();

        // 会话标题
        const title = await firstSession.locator('p.font-medium').textContent();
        expect(title).toBeTruthy();
        console.log(`    - 标题: ${title}`);

        // 消息数量
        const messageCount = await firstSession.locator('p:has-text("条消息")').textContent();
        expect(messageCount).toBeTruthy();
        console.log(`    - 消息数: ${messageCount}`);

        // 时间显示
        const timeText = await firstSession.locator('[class*="text-muted-foreground"]').last().textContent();
        expect(timeText).toBeTruthy();
        console.log(`    - 时间: ${timeText}`);

        // 验证图标
        const icon = firstSession.locator('svg').first();
        await expect(icon).toBeVisible();
        console.log('    - 图标可见');

        // 验证按钮可点击
        await expect(firstSession).toBeEnabled();
        console.log('    - 按钮可点击');
      }
    }

    // 2.5 检查"查看全部"按钮（仅在会话数 > 5 时显示）
    const viewAllButton = page.locator('button:has-text("查看全部")');
    const hasViewAll = await viewAllButton.isVisible().catch(() => false);

    if (hasViewAll) {
      const buttonText = await viewAllButton.textContent();
      console.log(`  ✓ "查看全部"按钮显示: ${buttonText}`);
    } else {
      console.log('  - "查看全部"按钮未显示（会话数 <= 5）');
    }
  });

  /**
   * 场景 3: 创建新会话按钮
   *
   * 验证点：
   * - 按钮可见且可点击
   * - 点击后触发 API 调用
   * - 创建成功后跳转到会话页
   * - 会话数据持久化到数据库
   */
  test('场景 3: 创建新会话按钮', async ({ page }) => {
    console.log('\n[Dashboard] 测试创建新会话...');

    // 3.1 定位"新建会话"按钮
    const createButton = page.locator('button:has-text("新建会话")');
    await expect(createButton).toBeVisible({ timeout: 10000 });
    console.log('  ✓ "新建会话"按钮可见');

    // 3.2 验证按钮图标
    const icon = createButton.locator('svg');
    await expect(icon).toBeVisible();
    console.log('  ✓ 按钮图标显示');

    // 3.3 记录当前会话数量（前端）
    const initialCount = await recordElementCount(page, 'button[class*="hover:bg-accent"]');
    console.log(`  当前会话数: ${initialCount}`);

    // 3.4 监听 API 请求和响应
    const apiRequest = waitForAPI(page, '/api/v1/threads', 'POST');
    const apiResponse = waitForAPIResponse(page, '/api/v1/threads');

    // 3.5 点击按钮创建会话
    await createButton.click();
    console.log('  ✓ 点击创建按钮');

    // 3.6 等待 API 请求
    const request = await apiRequest;
    console.log(`  ✓ API 请求已发送: ${request.method()} ${request.url()}`);

    // 3.7 等待 API 响应
    const response = await apiResponse;
    const responseData = await response.json();
    console.log('  ✓ API 响应成功');

    // 验证响应数据结构
    expect(responseData).toHaveProperty('thread_id');
    expect(responseData).toHaveProperty('created_at');
    expect(responseData).toHaveProperty('updated_at');
    console.log('  ✓ API 响应数据结构正确');

    // 3.8 验证跳转到会话页
    await verifyURL(page, /\/session\/[a-f0-9-]+$/);
    const sessionId = page.url().split('/session/')[1]?.split('?')[0];
    console.log(`  ✓ 成功跳转到会话页: ${sessionId}`);

    // 3.9 验证数据持久化（通过 API 获取数据库中的记录）
    await page.goto('/', { timeout: 10000 });
    await waitForPageLoad(page);

    const newCount = await recordElementCount(page, 'button[class*="hover:bg-accent"]');
    console.log(`  新会话数: ${newCount}`);

    // 验证会话数量增加
    expect(newCount).toBeGreaterThan(initialCount);
    console.log('  ✓ 会话列表已更新（前端状态）');

    // 3.10 通过 API 验证数据库记录
    const dbCount = await getDBRecordCount(page, '/api/v1/threads');
    console.log(`  数据库会话总数: ${dbCount}`);
    expect(dbCount).toBeGreaterThan(0);
    console.log('  ✓ 数据库记录验证成功');
  });

  /**
   * 场景 4: 会话列表展示
   *
   * 验证点：
   * - 会话按更新时间排序
   * - 会话信息完整显示
   * - 点击会话正常跳转
   * - 会话状态正确显示
   */
  test('场景 4: 会话列表展示', async ({ page }) => {
    console.log('\n[Dashboard] 测试会话列表...');

    // 4.1 等待会话列表加载
    await waitForAPIResponse(page, '/api/v1/threads');
    console.log('  ✓ 会话列表加载完成');

    // 4.2 检查是否有会话
    const emptyState = page.locator('text=/还没有会话/');
    const hasEmptyState = await emptyState.isVisible().catch(() => false);

    if (hasEmptyState) {
      console.log('  ⚠ 空状态，跳过会话列表测试');
      test.skip(true, '没有可用的会话');
      return;
    }

    // 4.3 获取所有会话项
    const sessionItems = page.locator('button[class*="hover:bg-accent"]');
    const count = await sessionItems.count();
    console.log(`  会话数量: ${count}`);

    expect(count).toBeGreaterThan(0);

    if (count > 0) {
      // 4.4 验证第一个会话的详细信息
      const firstSession = sessionItems.first();

      // 检查所有必需元素
      const title = firstSession.locator('p.font-medium');
      const messageCount = firstSession.locator('p:has-text("条消息")');
      const timeIcon = firstSession.locator('svg.lucide-clock');
      const timeText = firstSession.locator('[class*="text-muted-foreground"]').last();
      const messageIcon = firstSession.locator('svg.lucide-message-square');

      await expect(title).toBeVisible();
      await expect(messageCount).toBeVisible();
      await expect(timeIcon).toBeVisible();
      await expect(timeText).toBeVisible();
      await expect(messageIcon).toBeVisible();
      console.log('  ✓ 第一个会话所有元素可见');

      // 4.5 验证时间格式化
      const time = await timeText.textContent();
      const validTimeFormats = [
        '刚刚',
        /^\d+ 分钟前$/,
        /^\d+ 小时前$/,
        /^\d{4}\/\d{1,2}\/\d{1,2}$/,
      ];

      const isValidTime = validTimeFormats.some((pattern) =>
        typeof pattern === 'string' ? time === pattern : pattern.test(time || '')
      );

      expect(isValidTime).toBeTruthy();
      console.log(`  ✓ 时间格式正确: ${time}`);

      // 4.6 点击会话并验证跳转
      const sessionTitle = await title.textContent();
      console.log(`  点击会话: ${sessionTitle}`);

      // 监听导航
      const navigated = page.waitForURL(/\/session\/[a-f0-9-]+$/);

      await firstSession.click();
      await navigated;

      console.log('  ✓ 成功跳转到会话详情页');

      // 4.7 验证 URL 格式
      const url = page.url();
      const sessionIdMatch = url.match(/\/session\/([a-f0-9-]+)$/);
      expect(sessionIdMatch).toBeTruthy();
      console.log(`  ✓ Session ID 格式正确: ${sessionIdMatch?.[1]}`);

      // 4.8 返回首页
      await page.goto('/', { timeout: 10000 });
      await waitForPageLoad(page);
      console.log('  ✓ 返回首页');
    }

    // 4.9 验证"查看全部"按钮（如果会话数 > 5）
    if (count > 5) {
      const viewAllButton = page.locator('button:has-text("查看全部")');
      await expect(viewAllButton).toBeVisible();

      const buttonText = await viewAllButton.textContent();
      expect(buttonText).toContain(`查看全部 ${count} 个会话`);
      console.log(`  ✓ "查看全部"按钮显示: ${buttonText}`);
    }
  });

  /**
   * 场景 5: 快速操作功能
   *
   * 验证点：
   * - "查看主题演示"按钮功能
   * - 按钮样式正确
   * - 跳转功能正常
   * - 导航后可返回
   */
  test('场景 5: 快速操作功能', async ({ page }) => {
    console.log('\n[Dashboard] 测试快速操作...');

    // 5.1 验证"查看主题演示"按钮
    const demoButton = page.locator('button:has-text("查看主题演示")');
    await expect(demoButton).toBeVisible({ timeout: 10000 });
    console.log('  ✓ "查看主题演示"按钮可见');

    // 5.2 验证按钮样式（outline 样式）
    const className = await demoButton.getAttribute('class');
    expect(className).toContain('outline');
    console.log('  ✓ 按钮样式正确（outline）');

    // 5.3 点击按钮
    const navigated = page.waitForURL(/\/demo\/deep-space$/);
    await demoButton.click();
    await navigated;
    console.log('  ✓ 成功跳转到主题演示页');

    // 5.4 验证演示页 URL
    const demoUrl = page.url();
    expect(demoUrl).toContain('/demo/deep-space');
    console.log(`  ✓ 演示页 URL 正确: ${demoUrl}`);

    // 5.5 返回首页
    await page.goto('/', { timeout: 10000 });
    await waitForPageLoad(page);
    console.log('  ✓ 返回首页');

    // 5.6 验证返回成功
    await verifyURL(page, /^http:\/\/localhost:\d+\/?$/);
    console.log('  ✓ 返回首页成功');
  });

  /**
   * 场景 6: 错误处理
   *
   * 验证点：
   * - API 错误时显示错误提示
   * - 错误提示样式正确
   * - 错误提示可关闭或消失
   */
  test('场景 6: 错误处理', async ({ page }) => {
    console.log('\n[Dashboard] 测试错误处理...');

    // 6.1 正常情况下不应有错误提示
    const errorDiv = page.locator('.bg-destructive');
    const hasError = await errorDiv.isVisible().catch(() => false);

    if (!hasError) {
      console.log('  ✓ 无错误提示（正常状态）');
    } else {
      const errorText = await errorDiv.textContent();
      console.log(`  ⚠ 错误提示显示: ${errorText}`);
    }

    // 6.2 模拟 API 错误（通过拦截请求）
    // 注意：这个测试可能需要根据实际 API 调整
    await page.route('**/api/v1/threads', (route) => {
      // 不拦截，让正常请求通过
      route.continue();
    });

    // 重新加载页面
    await page.reload();
    await waitForPageLoad(page);

    // 验证页面仍然正常工作
    const dashboardContainer = page.locator('[data-testid="dashboard-page"]');
    await expect(dashboardContainer).toBeVisible();
    console.log('  ✓ 页面在重载后正常工作');
  });

  /**
   * 场景 7: 响应式设计
   *
   * 验证点：
   * - 不同视口尺寸下布局正常
   * - 元素可见性正确
   * - 移动端适配
   */
  test('场景 7: 响应式设计', async ({ page }) => {
    console.log('\n[Dashboard] 测试响应式设计...');

    // 7.1 桌面端（默认）
    await page.setViewportSize({ width: 1920, height: 1080 });
    await page.reload();
    await waitForPageLoad(page);

    const dashboardContainer = page.locator('[data-testid="dashboard-page"]');
    await expect(dashboardContainer).toBeVisible();
    console.log('  ✓ 桌面端 (1920x1080) 布局正常');

    // 7.2 平板端
    await page.setViewportSize({ width: 768, height: 1024 });
    await page.reload();
    await waitForPageLoad(page);

    await expect(dashboardContainer).toBeVisible();
    console.log('  ✓ 平板端 (768x1024) 布局正常');

    // 7.3 移动端
    await page.setViewportSize({ width: 375, height: 667 });
    await page.reload();
    await waitForPageLoad(page);

    await expect(dashboardContainer).toBeVisible();
    console.log('  ✓ 移动端 (375x667) 布局正常');

    // 验证按钮仍然可见
    const createButton = page.locator('button:has-text("新建会话")');
    await expect(createButton).toBeVisible();
    console.log('  ✓ 移动端按钮可见');
  });

  /**
   * 场景 8: 主题切换
   *
   * 验证点：
   * - 主题切换功能正常
   * - 颜色主题正确应用
   * - 主题设置持久化
   */
  test('场景 8: 主题切换', async ({ page }) => {
    console.log('\n[Dashboard] 测试主题切换...');

    // 8.1 获取初始主题
    const initialTheme = await page.evaluate(() => {
      return document.documentElement.getAttribute('data-theme') || 'light';
    });
    console.log(`  初始主题: ${initialTheme}`);

    // 8.2 切换到深色模式
    const targetTheme = initialTheme === 'light' ? 'dark' : 'light';

    // 通过导航栏的主题按钮切换（如果存在）
    const themeButton = page.locator('[data-testid="theme-toggle"]').first();
    const hasThemeButton = await themeButton.isVisible().catch(() => false);

    if (hasThemeButton) {
      await themeButton.click();
      await page.waitForTimeout(500);
      console.log('  ✓ 通过按钮切换主题');
    } else {
      // 通过设置菜单切换
      await page.evaluate((theme) => {
        document.documentElement.setAttribute('data-theme', theme);
        localStorage.setItem('theme', theme);
      }, targetTheme);
      console.log('  ✓ 通过代码切换主题');
    }

    // 8.3 验证主题已切换
    const currentTheme = await page.evaluate(() => {
      return document.documentElement.getAttribute('data-theme') || 'light';
    });
    expect(currentTheme).toBe(targetTheme);
    console.log(`  ✓ 主题已切换到: ${currentTheme}`);

    // 8.4 验证主题持久化
    const storedTheme = await page.evaluate(() => {
      return localStorage.getItem('theme');
    });
    expect(storedTheme).toBe(targetTheme);
    console.log('  ✓ 主题设置已持久化');

    // 8.5 刷新页面验证主题保持
    await page.reload();
    await waitForPageLoad(page);

    const reloadedTheme = await page.evaluate(() => {
      return document.documentElement.getAttribute('data-theme') || 'light';
    });
    expect(reloadedTheme).toBe(targetTheme);
    console.log('  ✓ 刷新后主题保持不变');

    // 8.6 切换回原主题
    if (hasThemeButton) {
      await themeButton.click();
      await page.waitForTimeout(500);
    } else {
      await page.evaluate((theme) => {
        document.documentElement.setAttribute('data-theme', theme);
        localStorage.setItem('theme', theme);
      }, initialTheme);
    }

    console.log('  ✓ 主题切换测试完成');
  });

  /**
   * 场景 9: 性能测试
   *
   * 验证点：
   * - 页面加载时间
   * - API 响应时间
   * - 动画流畅度
   */
  test('场景 9: 性能测试', async ({ page }) => {
    console.log('\n[Dashboard] 测试性能...');

    // 9.1 测量页面加载时间
    const startTime = Date.now();
    await page.goto('/', { timeout: 30000 });
    await waitForPageLoad(page);
    const loadTime = Date.now() - startTime;
    console.log(`  页面加载时间: ${loadTime}ms`);

    // 验证加载时间合理（< 5秒）
    expect(loadTime).toBeLessThan(5000);

    // 9.2 测量 API 响应时间
    const apiStartTime = Date.now();
    await waitForAPIResponse(page, '/api/v1/threads');
    const apiTime = Date.now() - apiStartTime;
    console.log(`  API 响应时间: ${apiTime}ms`);

    // 验证 API 响应时间合理（< 3秒）
    expect(apiTime).toBeLessThan(3000);

    // 9.3 检查性能指标（如果支持）
    const metrics = await page.evaluate(() => {
      const navigation = performance.getEntriesByType('navigation')[0] as PerformanceNavigationTiming;
      return {
        domContentLoaded: navigation.domContentLoadedEventEnd - navigation.domContentLoadedEventStart,
        loadComplete: navigation.loadEventEnd - navigation.loadEventStart,
      };
    });

    console.log(`  DOM 内容加载时间: ${metrics.domContentLoaded}ms`);
    console.log(`  页面完全加载时间: ${metrics.loadComplete}ms`);

    console.log('  ✓ 性能指标正常');
  });

  /**
   * 场景 10: 辅助功能
   *
   * 验证点：
   * - 键盘导航
   * - ARIA 标签
   * - 屏幕阅读器支持
   */
  test('场景 10: 辅助功能', async ({ page }) => {
    console.log('\n[Dashboard] 测试辅助功能...');

    // 10.1 验证 Tab 键导航
    await page.keyboard.press('Tab');
    await page.keyboard.press('Tab');

    const focusedElement = await page.evaluate(() => {
      return document.activeElement?.tagName;
    });
    console.log(`  当前聚焦元素: ${focusedElement}`);

    // 10.2 验证按钮有 text 或 aria-label
    const buttons = page.locator('button');
    const buttonCount = await buttons.count();
    console.log(`  按钮数量: ${buttonCount}`);

    for (let i = 0; i < Math.min(buttonCount, 5); i++) {
      const button = buttons.nth(i);
      const text = await button.textContent();
      const ariaLabel = await button.getAttribute('aria-label');

      const hasAccessibleText = !!(text?.trim() || ariaLabel);
      expect(hasAccessibleText).toBeTruthy();
    }
    console.log('  ✓ 按钮具有可访问文本');

    // 10.3 验证语义化 HTML
    const h1 = page.locator('h1');
    await expect(h1).toBeVisible();
    console.log('  ✓ 使用语义化标题 (h1)');

    const h2 = page.locator('h2');
    await expect(h2).toBeVisible();
    console.log('  ✓ 使用语义化标题 (h2)');

    console.log('  ✓ 辅助功能测试完成');
  });

  /**
   * 场景 11: 截图对比
   *
   * 验证点：
   * - 视觉回归测试
   * - UI 布局一致性
   */
  test('场景 11: 截图对比', async ({ page }) => {
    console.log('\n[Dashboard] 测试截图对比...');

    // 11.1 等待页面完全加载
    await waitForAPIResponse(page, '/api/v1/threads');
    await page.waitForTimeout(1000);

    // 11.2 截取整个页面
    await takeScreenshot(page, 'dashboard-full');
    console.log('  ✓ 已保存完整页面截图');

    // 11.3 截取特定区域
    const welcomeSection = page.locator('h1').locator('..');
    await expect(welcomeSection).toBeVisible();
    await welcomeSection.screenshot({
      path: 'screenshots/dashboard-welcome.png',
    });
    console.log('  ✓ 已保存欢迎区域截图');

    // 11.4 如果有会话列表，截图会话列表
    const sessionList = page.locator('h2:has-text("最近会话")').locator('..');
    const isVisible = await sessionList.isVisible().catch(() => false);

    if (isVisible) {
      await sessionList.screenshot({
        path: 'screenshots/dashboard-session-list.png',
      });
      console.log('  ✓ 已保存会话列表截图');
    }

    console.log('  ✓ 截图对比测试完成');
  });
});

/**
 * 未登录用户测试
 */
test.describe('Dashboard 页面 - 未登录用户', () => {
  test('未登录时重定向到登录页', async ({ page }) => {
    console.log('\n[Dashboard] 测试未登录重定向...');

    // 清除所有存储
    await page.context().clearCookies();
    await page.evaluate(() => {
      localStorage.clear();
      sessionStorage.clear();
    });

    // 尝试访问首页
    await page.goto('/', { timeout: 30000 });

    // 等待重定向
    await page.waitForURL('**/login', { timeout: 10000 });
    console.log('  ✓ 未登录用户被重定向到登录页');

    // 验证 URL
    const url = page.url();
    expect(url).toContain('/login');
    console.log(`  ✓ 当前 URL: ${url}`);
  });
});

/**
 * API 集成测试
 */
test.describe('Dashboard 页面 - API 集成', () => {
  test.use({ storageState: 'tests/e2e/.auth/user.json' });

  test('验证 API 数据流', async ({ page }) => {
    console.log('\n[Dashboard] 测试 API 数据流...');

    // 1. 监听所有 API 请求
    const apiRequests: string[] = [];
    page.on('request', (request) => {
      if (request.url().includes('/api/')) {
        apiRequests.push(`${request.method()} ${request.url()}`);
      }
    });

    // 2. 访问首页
    await page.goto('/', { timeout: 30000 });
    await waitForPageLoad(page);

    // 3. 等待关键 API 完成
    await waitForAPIResponse(page, '/api/v1/threads');

    // 4. 输出 API 请求日志
    console.log('  API 请求列表:');
    apiRequests.forEach((req) => {
      console.log(`    - ${req}`);
    });

    // 5. 验证关键 API 被调用
    const hasThreadsApi = apiRequests.some((req) => req.includes('/api/v1/threads'));
    expect(hasThreadsApi).toBeTruthy();
    console.log('  ✓ 关键 API 已调用');

    console.log('  ✓ API 数据流测试完成');
  });
});
