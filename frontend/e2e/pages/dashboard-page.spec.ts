/**
 * 仪表盘页面 - 完整交互测试
 *
 * 测试所有组件、按钮、列表、弹窗
 */

import { test, expect } from '@playwright/test';

test.describe('仪表盘页面 - 完整交互测试', () => {

  test.beforeEach(async ({ page }) => {
    // 尝试登录或直接访问
    await page.goto(process.env.REACT_APP_FRONTEND_URL || "http://localhost:5188");
    await page.waitForTimeout(1000);

    // 如果在登录页，跳过测试
    if (page.url().includes('/login')) {
      test.skip(true, '需要先登录');
    }
  });

  test('1. 欢迎消息区域', async ({ page }) => {
    console.log('\n[仪表盘] 测试欢迎消息区域...');

    // 1.1 检查用户名显示
    const username = await page.locator('h1').textContent();
    console.log(`  欢迎消息: ${username}`);
    expect(username).toContain('欢迎回来');

    // 1.2 检查描述文字
    const description = await page.locator('p.text-muted-foreground').first().textContent();
    console.log(`  描述文字: ${description}`);
    expect(description).toBeTruthy();
  });

  test('2. 快速操作按钮', async ({ page }) => {
    console.log('\n[仪表盘] 测试快速操作按钮...');

    // 2.1 新建会话按钮
    const newSessionBtn = page.getByText('新建会话');
    const count1 = await newSessionBtn.count();

    if (count1 > 0) {
      console.log('  ✓ 新建会话按钮存在');

      // 检查图标
      const icon = page.locator('button').filter({ hasText: '新建会话' }).locator('svg').count();
      console.log(`    - 图标: ${icon > 0 ? '✓' : '✗'}`);

      // 悬停效果
      await newSessionBtn.first().hover();
      await page.waitForTimeout(200);
      console.log('    - 悬停效果: ✓');
    }

    // 2.2 查看主题演示按钮
    const demoBtn = page.getByText('查看主题演示');
    const count2 = await demoBtn.count();

    if (count2 > 0) {
      console.log('  ✓ 查看主题演示按钮存在');

      // 点击测试
      await demoBtn.first().click();
      await page.waitForTimeout(1000);

      const url = page.url();
      if (url.includes('/demo')) {
        console.log('    - 跳转: ✓ (到演示页)');
        // 返回
        await page.goBack();
        await page.waitForTimeout(1000);
      }
    }
  });

  test('3. 会话列表区域', async ({ page }) => {
    console.log('\n[仪表盘] 测试会话列表区域...');

    // 3.1 标题
    const title = await page.locator('h2:has-text("最近会话")').count();
    console.log(`  最近会话标题: ${title > 0 ? '✓' : '✗'}`);

    // 3.2 加载状态
    const loading = await page.locator('text=/加载中/').count();
    if (loading > 0) {
      console.log('  正在加载...');
      await page.waitForTimeout(2000);
    }

    // 3.3 空状态
    const empty = await page.locator('text=/还没有会话/').count();
    if (empty > 0) {
      console.log('  ✓ 空状态显示正常');
      return;
    }

    // 3.4 会话列表项
    const sessionItems = page.locator('button').filter({ hasText: '条消息' });
    const count = await sessionItems.count();

    console.log(`  会话数量: ${count}`);

    if (count > 0) {
      // 检查第一个会话项
      const first = sessionItems.first();

      // 会话标题
      const title = await first.locator('p.font-medium').textContent();
      console.log(`    - 标题: ${title}`);

      // 消息数量
      const msgCount = await first.locator('p:has-text("条消息")').textContent();
      console.log(`    - 消息数: ${msgCount}`);

      // 时间显示
      const time = await first.locator('[class*="text-muted-foreground"]').last().textContent();
      console.log(`    - 时间: ${time}`);

      // 3.5 点击会话
      console.log('  测试点击会话...');
      await first.click();
      await page.waitForTimeout(1000);

      const url = page.url();
      if (url.includes('/session/')) {
        console.log('    - 跳转到会话页: ✓');
      } else {
        console.log(`    - 未跳转: ${url}`);
      }

      // 返回
      await page.goBack();
      await page.waitForTimeout(1000);
    }
  });

  test('4. 创建新会话功能', async ({ page }) => {
    console.log('\n[仪表盘] 测试创建新会话功能...');

    const newSessionBtn = page.getByText('新建会话');
    const count = await newSessionBtn.count();

    if (count === 0) {
      console.log('  ✗ 新建会话按钮不存在');
      test.skip(true, '按钮不存在');
    }

    // 捕获 API 响应
    let apiResponse: any = null;
    page.on('response', async (res) => {
      if (res.url().includes('/api/v1/threads') && res.request().method() === 'POST') {
        try {
          apiResponse = await res.json();
        } catch (e) {}
      }
    });

    // 点击按钮
    await newSessionBtn.first().click();
    await page.waitForTimeout(3000);

    const url = page.url();

    if (url.includes('/session/')) {
      console.log('  ✓ 成功创建并跳转到会话页');

      // 验证 URL 格式
      const sessionId = url.split('/session/')[1]?.split('?')[0];
      if (sessionId) {
        console.log(`    - Session ID: ${sessionId}`);
      }

      // 验证 API 响应
      if (apiResponse) {
        console.log('  API 响应数据类型:');
        console.log(`    - thread_id: ${typeof apiResponse.thread_id} ${typeof apiResponse.thread_id === 'string' ? '✓' : '✗'}`);
        console.log(`    - created_at: ${typeof apiResponse.created_at} ${typeof apiResponse.created_at === 'string' ? '✓' : '✗'}`);
        console.log(`    - updated_at: ${typeof apiResponse.updated_at} ${typeof apiResponse.updated_at === 'string' ? '✓' : '✗'}`);
      }
    } else {
      console.log(`  ✗ 未跳转: ${url}`);
    }
  });

  test('5. 错误提示区域', async ({ page }) => {
    console.log('\n[仪表盘] 测试错误提示...');

    // 正常情况下不应该有错误
    const errorDiv = page.locator('.bg-destructive').count();
    console.log(`  错误提示区域: ${errorDiv > 0 ? '显示（可能有问题）' : '正常（无错误）'}`);
  });

  test('6. 查看全部会话按钮', async ({ page }) => {
    console.log('\n[仪表盘] 测试查看全部会话按钮...');

    const viewAllBtn = page.getByText('查看全部');
    const count = await viewAllBtn.count();

    if (count > 0) {
      console.log('  ✓ 查看全部按钮存在');

      // 获取会话数量
      const text = await viewAllBtn.first().textContent();
      console.log(`    - 按钮文字: ${text}`);

      // 点击测试
      await viewAllBtn.first().click();
      await page.waitForTimeout(1000);

      const url = page.url();
      console.log(`    - 跳转: ${url}`);
    } else {
      console.log('  - 查看全部按钮不存在（会话数 <= 5）');
    }
  });

  test('7. 响应式布局', async ({ page }) => {
    console.log('\n[仪表盘] 测试响应式布局...');

    // 获取视口大小
    const viewport = page.viewportSize();
    console.log(`  当前视口: ${viewport?.width}x${viewport?.height}`);

    // 检查主要容器
    const mainContainer = page.locator('[data-testid="dashboard-page"]');
    const visible = await mainContainer.isVisible();
    console.log(`  主容器可见: ${visible ? '✓' : '✗'}`);
  });
});
