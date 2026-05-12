/**
 * 冒烟测试 - 验证基本功能
 *
 * 快速检查页面是否能够正常加载和显示
 */

import { test, expect } from '@playwright/test';

test.describe('冒烟测试', () => {
  test('首页应该能够加载', async ({ page }) => {
    await page.goto('/');

    // 等待页面加载
    await page.waitForLoadState('domcontentloaded');

    // 截图
    await page.screenshot({ path: 'test-results/homepage.png' });

    // 检查页面是否可访问
    expect(await page.title()).toBeTruthy();
  });

  test('登录页应该能够加载', async ({ page }) => {
    await page.goto('/login');

    // 等待页面加载
    await page.waitForLoadState('domcontentloaded');

    // 截图
    await page.screenshot({ path: 'test-results/login-page.png' });

    // 检查页面标题
    const title = await page.title();
    expect(title).toBeTruthy();

    console.log('登录页面标题:', title);
  });

  test('检查页面元素是否存在', async ({ page }) => {
    await page.goto('/');
    await page.waitForLoadState('domcontentloaded');

    // 截图
    await page.screenshot({ path: 'test-results/page-elements.png', fullPage: true });

    // 检查是否有主要元素
    const body = page.locator('body');
    await expect(body).toBeVisible();

    console.log('页面基本信息:', {
      url: page.url(),
      title: await page.title(),
      viewportSize: page.viewportSize(),
    });
  });

  test('检查控制台错误', async ({ page }) => {
    const errors: string[] = [];

    // 监听控制台错误
    page.on('console', (msg) => {
      if (msg.type() === 'error') {
        errors.push(msg.text());
      }
    });

    await page.goto('/');
    await page.waitForLoadState('domcontentloaded');

    // 等待一段时间让页面完全加载
    await page.waitForTimeout(3000);

    // 打印错误信息
    if (errors.length > 0) {
      console.log('发现控制台错误:');
      errors.forEach((error, index) => {
        console.log(`  ${index + 1}. ${error}`);
      });
    } else {
      console.log('没有发现控制台错误');
    }

    await page.screenshot({ path: 'test-results/console-check.png' });
  });

  test('检查网络请求', async ({ page }) => {
    const requests: string[] = [];
    const failedRequests: string[] = [];

    // 监听请求
    page.on('request', (request) => {
      requests.push(request.url());
    });

    // 监听请求失败
    page.on('requestfailed', (request) => {
      failedRequests.push(request.url());
    });

    await page.goto('/');
    await page.waitForLoadState('networkidle');

    console.log('网络请求统计:', {
      total: requests.length,
      failed: failedRequests.length,
    });

    if (failedRequests.length > 0) {
      console.log('失败的请求:');
      failedRequests.forEach((url, index) => {
        console.log(`  ${index + 1}. ${url}`);
      });
    }

    await page.screenshot({ path: 'test-results/network-check.png' });
  });

  test('检查响应式布局', async ({ page }) => {
    const sizes = [
      { width: 1920, height: 1080, name: 'desktop' },
      { width: 768, height: 1024, name: 'tablet' },
      { width: 375, height: 667, name: 'mobile' },
    ];

    for (const size of sizes) {
      await page.setViewportSize({ width: size.width, height: size.height });
      await page.goto('/');
      await page.waitForLoadState('domcontentloaded');
      await page.waitForTimeout(500);

      await page.screenshot({
        path: `test-results/responsive-${size.name}.png`,
        fullPage: true,
      });

      console.log(`${size.name} (${size.width}x${size.height}) 截图已完成`);
    }
  });
});
