/**
 * 注册功能测试
 */

import { test, expect } from '@playwright/test';

test.describe('注册功能完整测试', () => {

  test('完整注册流程', async ({ page }) => {
    console.log('\n=== 注册功能测试 ===');

    // 1. 访问注册页
    await page.goto('http://localhost:5188/register');
    await page.waitForTimeout(1000);

    // 2. 填写注册表单
    const timestamp = Date.now();
    const username = `testuser${timestamp}`;
    const email = `test${timestamp}@example.com`;
    const password = 'password123';

    console.log(`注册用户: ${username}`);

    // 填写用户名
    await page.locator('input[type="text"]').first().fill(username);
    console.log('✓ 用户名已填写');

    // 填写邮箱
    await page.locator('input[type="email"]').fill(email);
    console.log('✓ 邮箱已填写');

    // 填写密码
    const passwordInputs = await page.locator('input[type="password"]').all();
    await passwordInputs[0].fill(password);
    await passwordInputs[1].fill(password);
    console.log('✓ 密码已填写');

    // 3. 监听 API 响应
    let apiResponse: any = null;
    page.on('response', async (res) => {
      if (res.url().includes('/api/v1/auth/register')) {
        console.log(`\n[API] ${res.url()}`);
        console.log(`[API] Status: ${res.status()}`);

        const contentType = res.headers()['content-type'] || '';
        if (contentType.includes('application/json')) {
          try {
            apiResponse = await res.json();
            console.log(`[API] Response:`, JSON.stringify(apiResponse, null, 2));
          } catch (e) {
            console.log(`[API] Response text:`, await res.text());
          }
        }
      }
    });

    // 4. 点击注册按钮
    await page.locator('button:has-text("注册")').click();
    console.log('\n✓ 注册按钮已点击');

    // 5. 等待响应
    await page.waitForTimeout(5000);

    // 6. 检查结果
    const currentUrl = page.url();
    console.log(`\n当前 URL: ${currentUrl}`);

    if (apiResponse) {
      console.log('\n=== 数据类型验证 ===');
      console.log(`access_token: ${typeof apiResponse.access_token} (${typeof apiResponse.access_token === 'string' ? '✓' : '✗'})`);
      console.log(`refresh_token: ${typeof apiResponse.refresh_token} (${typeof apiResponse.refresh_token === 'string' ? '✓' : '✗'})`);
      console.log(`token_type: ${typeof apiResponse.token_type} (${typeof apiResponse.token_type === 'string' ? '✓' : '✗'})`);
      console.log(`expires_in: ${typeof apiResponse.expires_in} (${typeof apiResponse.expires_in === 'number' ? '✓' : '✗'})`);
    }

    // 7. 验证是否跳转到仪表盘
    if (currentUrl.includes('/dashboard') || currentUrl === 'http://localhost:5188/') {
      console.log('✓ 注册成功，已跳转到仪表盘');
    } else if (currentUrl.includes('/register')) {
      console.log('⚠️ 仍在注册页，可能注册失败');
    } else {
      console.log(`⚠️ 跳转到: ${currentUrl}`);
    }
  });

});
