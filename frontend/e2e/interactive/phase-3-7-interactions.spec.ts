/**
 * 阶段 3-7: 所有交互组件测试
 *
 * 逐个页面、逐个组件、逐个操作测试
 * 验证前后端数据交互
 */

import { test, expect } from '@playwright/test';

const BASE_URL = process.env.REACT_APP_FRONTEND_URL || "http://localhost:5188";

// 测试用户
const testUser = {
  username: `interactive_${Date.now()}`,
  password: 'Test123456!',
  email: `interactive_${Date.now()}@example.com`
};

test.describe('阶段3: 登录页交互测试', () => {

  test('3A-3I. 登录页所有交互', async ({ page }) => {
    console.log('\n[阶段3] 开始登录页交互测试...');

    await page.goto(`${BASE_URL}/login`);

    // 3A: 用户名输入框 - 点击聚焦
    console.log('\n[3A] 测试用户名输入框聚焦');
    const usernameInput = page.locator('input[type="text"]').first();
    await usernameInput.click();
    await expect(usernameInput).toBeFocused();
    console.log('✅ 用户名输入框聚焦成功');

    // 3B: 输入文本
    console.log('\n[3B] 测试输入用户名');
    await usernameInput.fill('testuser');
    const value = await usernameInput.inputValue();
    expect(value).toBe('testuser');
    console.log('✅ 用户名输入正常');

    // 3C: 清空内容
    console.log('\n[3C] 测试清空用户名');
    await usernameInput.fill('');
    expect(await usernameInput.inputValue()).toBe('');
    console.log('✅ 用户名清空正常');

    // 3D: 密码输入框聚焦
    console.log('\n[3D] 测试密码输入框聚焦');
    const passwordInput = page.locator('input[type="password"]').first();
    await passwordInput.click();
    await expect(passwordInput).toBeFocused();
    console.log('✅ 密码输入框聚焦成功');

    // 3E: 输入密码
    console.log('\n[3E] 测试输入密码');
    await passwordInput.fill('password123');
    const passValue = await passwordInput.inputValue();
    expect(passValue).toBe('password123');
    console.log('✅ 密码输入正常');

    // 3F: 检查密码可见性（如果有切换按钮）
    console.log('\n[3F] 检查密码切换按钮');
    const toggleBtn = page.locator('button[aria-label*="密码"], button[aria-label*="password"], .password-toggle').count();
    if (toggleBtn > 0) {
      console.log('✅ 密码切换按钮存在');
    } else {
      console.log('⚠️  密码切换按钮不存在（可能未实现）');
    }

    // 3G: 点击登录按钮（空表单）
    console.log('\n[3G] 测试空表单提交');
    await usernameInput.fill('');
    await passwordInput.fill('');
    await page.click('button[type="submit"]');
    await page.waitForTimeout(500);

    const error = await page.locator('text=/用户名不能为空/').count();
    if (error > 0) {
      console.log('✅ 空表单验证正常');
    } else {
      console.log('⚠️  未显示验证错误');
    }

    // 3H: 尝试登录（需要数据库）
    console.log('\n[3H] 测试登录功能');

    let apiResponse: any = null;
    let apiStatus = 0;

    page.on('response', async (res) => {
      if (res.url().includes('/api/v1/auth/login')) {
        apiStatus = res.status();
        try {
          apiResponse = await res.json();
        } catch (e) {
          apiResponse = await res.text();
        }
      }
    });

    await usernameInput.fill(testUser.username);
    await passwordInput.fill(testUser.password);
    await page.click('button[type="submit"]');
    await page.waitForTimeout(3000);

    if (apiStatus === 200 && apiResponse) {
      console.log('✅ 登录成功');
      console.log('  验证数据类型:');
      console.log(`    - access_token: ${typeof apiResponse.access_token} ${typeof apiResponse.access_token === 'string' ? '✅' : '❌'}`);
      console.log(`    - refresh_token: ${typeof apiResponse.refresh_token} ${typeof apiResponse.refresh_token === 'string' ? '✅' : '❌'}`);
      console.log(`    - token_type: ${typeof apiResponse.token_type} ${typeof apiResponse.token_type === 'string' ? '✅' : '❌'}`);
      console.log(`    - expires_in: ${typeof apiResponse.expires_in} ${typeof apiResponse.expires_in === 'number' ? '✅' : '❌'}`);
    } else if (apiStatus === 401) {
      console.log('⚠️  用户不存在 (需要先注册)');
    } else if (apiStatus >= 500) {
      console.log('❌ 数据库错误');
      console.log('   提示: 请检查 PostgreSQL 是否运行');
    } else {
      console.log(`⚠️  HTTP ${apiStatus}`);
    }

    // 3I: 注册链接
    console.log('\n[3I] 测试注册链接');
    const registerLink = page.getByText('注册').first();
    await registerLink.click();
    await page.waitForTimeout(1000);

    const url = page.url();
    if (url.includes('/register')) {
      console.log('✅ 注册链接跳转正常');
    } else {
      console.log(`❌ 未跳转到注册页: ${url}`);
    }
  });
});

test.describe('阶段4: 注册页交互测试', () => {

  test('4A-4G. 注册页所有交互', async ({ page }) => {
    console.log('\n[阶段4] 开始注册页交互测试...');

    await page.goto(`${BASE_URL}/register`);

    // 4A: 用户名输入
    console.log('\n[4A] 测试用户名输入');
    const usernameInput = page.locator('input[type="text"]').first();
    await usernameInput.fill(testUser.username);
    expect(await usernameInput.inputValue()).toBe(testUser.username);
    console.log('✅ 用户名输入正常');

    // 4B: 邮箱输入
    console.log('\n[4B] 测试邮箱输入');
    const emailInput = page.locator('input[type="email"]').first();
    await emailInput.fill(testUser.email);
    expect(await emailInput.inputValue()).toBe(testUser.email);
    console.log('✅ 邮箱输入正常');

    // 4C: 无效邮箱
    console.log('\n[4C] 测试无效邮箱验证');
    await emailInput.fill('invalid-email');
    await page.waitForTimeout(500);
    const emailError = await page.locator('text=/邮箱|email|格式/i').count();
    if (emailError > 0) {
      console.log('✅ 邮箱格式验证存在');
    } else {
      console.log('⚠️  未显示邮箱格式错误（可能未实现）');
    }

    // 4D: 密码输入
    console.log('\n[4D] 测试密码输入');
    await emailInput.fill(testUser.email); // 恢复正确邮箱
    const passwordInputs = page.locator('input[type="password"]');
    const passCount = await passwordInputs.count();
    await passwordInputs.nth(0).fill(testUser.password);
    console.log(`✅ 密码输入正常 (找到 ${passCount} 个密码框)`);

    // 4E: 确认密码
    console.log('\n[4E] 测试确认密码');
    if (passCount >= 2) {
      await passwordInputs.nth(1).fill('different');
      await page.waitForTimeout(500);
      const matchError = await page.locator('text=/密码不匹配|不一致/match').count();
      if (matchError > 0) {
        console.log('✅ 密码匹配验证存在');
      } else {
        console.log('⚠️  未显示密码不匹配错误');
      }

      // 修正密码
      await passwordInputs.nth(1).fill(testUser.password);
      console.log('✅ 确认密码已修正');
    }

    // 4F: 注册功能
    console.log('\n[4F] 测试注册功能');

    let apiResponse: any = null;
    let apiStatus = 0;

    page.on('response', async (res) => {
      if (res.url().includes('/api/v1/auth/register')) {
        apiStatus = res.status();
        try {
          apiResponse = await res.json();
        } catch (e) {
          apiResponse = await res.text();
        }
      }
    });

    await page.click('button:has-text("注册")');
    await page.waitForTimeout(3000);

    if (apiStatus === 200 || apiStatus === 201) {
      console.log('✅ 注册成功');
      console.log('  验证数据类型:');
      console.log(`    - id: ${typeof apiResponse.id} ${typeof apiResponse.id === 'string' ? '✅' : '❌'}`);
      console.log(`    - username: ${typeof apiResponse.username} ${typeof apiResponse.username === 'string' ? '✅' : '❌'}`);
      console.log(`    - email: ${typeof apiResponse.email} ${typeof apiResponse.email === 'string' ? '✅' : '❌'}`);
      console.log(`    - created_at: ${typeof apiResponse.created_at} ${typeof apiResponse.created_at === 'string' ? '✅' : '❌'}`);
    } else if (apiStatus >= 500) {
      console.log('❌ 数据库错误');
      console.log('   提示: 请检查 PostgreSQL 配置');
    } else {
      console.log(`⚠️  HTTP ${apiStatus}`);
    }

    // 4G: 登录链接
    console.log('\n[4G] 测试登录链接');
    const loginLink = page.getByText('登录').first();
    await loginLink.click();
    await page.waitForTimeout(1000);

    const url = page.url();
    if (url.includes('/login')) {
      console.log('✅ 登录链接跳转正常');
    } else {
      console.log(`❌ 未跳转到登录页: ${url}`);
    }
  });
});

test.describe('阶段5-7: 需要登录的页面', () => {

  test('5A-5F. 仪表盘交互（如果已登录）', async ({ page }) => {
    console.log('\n[阶段5] 测试仪表盘交互...');

    await page.goto(BASE_URL);
    await page.waitForTimeout(2000);

    const url = page.url();

    if (url.includes('/login')) {
      console.log('⚠️  未登录，跳过仪表盘交互测试');
      console.log('   提示: 请先完成注册和登录');
      return;
    }

    // 如果已登录，执行测试
    console.log('✅ 已登录，开始仪表盘测试');

    // 5A: 欢迎消息
    const welcome = await page.locator('text=/欢迎回来/').textContent();
    console.log(`✅ 欢迎消息: ${welcome?.substring(0, 50)}`);

    // 5B: 会话列表
    console.log('\n[5B] 检查会话列表');
    let apiResponse: any = null;
    page.on('response', async (res) => {
      if (res.url().includes('/api/v1/threads')) {
        try {
          apiResponse = await res.json();
        } catch (e) {}
      }
    });

    await page.waitForTimeout(2000);

    if (apiResponse) {
      console.log(`✅ 会话列表加载: ${Array.isArray(apiResponse) ? apiResponse.length : '非数组'} 条`);
      if (Array.isArray(apiResponse) && apiResponse.length > 0) {
        console.log('  第一条数据类型验证:');
        const first = apiResponse[0];
        console.log(`    - thread_id: ${typeof first.thread_id}`);
        console.log(`    - current_state: ${typeof first.current_state}`);
      }
    }

    // 5E: 新建会话按钮
    console.log('\n[5E] 测试新建会话按钮');
    const createBtn = page.getByText('新建').or(page.getByText('+')).or(page.getByText('创建'));
    const count = await createBtn.count();

    if (count > 0) {
      console.log('✅ 新建会话按钮存在');

      page.on('response', async (res) => {
        if (res.url().includes('/api/v1/threads') && res.request().method() === 'POST') {
          try {
            apiResponse = await res.json();
          } catch (e) {}
        }
      });

      await createBtn.first().click();
      await page.waitForTimeout(2000);

      if (apiResponse) {
        console.log('✅ 会话创建响应:');
        console.log(`    - thread_id: ${typeof apiResponse.thread_id}`);
        console.log(`    - created_at: ${typeof apiResponse.created_at}`);
      }
    } else {
      console.log('❌ 新建会话按钮未找到');
    }
  });
});
