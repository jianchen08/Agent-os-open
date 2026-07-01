/**
 * 简单的工具卡片测试 - 用于验证修复
 */

import { test, expect } from '@playwright/test';

test.describe('简单工具卡片测试', () => {
  test('验证工具卡片渲染', async ({ page }) => {
    // 先通过 API 登录获取 token
    console.log('正在通过 API 登录...');
    const loginResponse = await page.request.post('http://localhost:8988/api/v1/auth/login', {
      data: {
        username: 'admin',
        password: 'admin123'
      }
    });

    if (!loginResponse.ok()) {
      throw new Error(`登录失败: ${loginResponse.status()}`);
    }

    const loginData = await loginResponse.json();
    const accessToken = loginData.access_token;
    const refreshToken = loginData.refresh_token;
    const expiresIn = loginData.expires_in || 3600; // 默认1小时

    console.log('✅ API 登录成功，获取到 token');

    // 设置 localStorage 中的完整认证数据
    await page.goto('http://localhost:5188/');
    await page.evaluate((data) => {
      const now = Date.now();
      localStorage.setItem('access_token', data.accessToken);
      localStorage.setItem('refresh_token', data.refreshToken);
      localStorage.setItem('access_token_expiry', (now + data.expiresIn * 1000).toString());
      // 设置用户信息
      const user = {
        id: 'admin-user',
        username: 'admin',
        email: 'admin@example.com',
        created_at: new Date().toISOString()
      };
      localStorage.setItem('auth_user', JSON.stringify(user));
    }, { accessToken, refreshToken, expiresIn });

    // 刷新页面以应用 token
    await page.reload();

    // 等待页面加载
    await page.waitForTimeout(2000);

    // 检查是否有输入框
    const inputBox = page.locator('[data-testid="chat-input-textarea"]');
    await expect(inputBox, '应该找到输入框').toBeVisible({ timeout: 10000 });
    console.log('✅ 页面已加载，输入框可见');

    // 发送消息触发工具调用
    await inputBox.fill('请读取 package.json 文件的内容');
    const sendButton = page.locator('[data-testid="chat-send-button"]');
    await sendButton.click();

    console.log('消息已发送，等待AI响应...');

    // 等待AI响应
    const assistantMessage = page.locator('[data-role="assistant"]');
    await expect(assistantMessage.first()).toBeVisible({ timeout: 15000 });
    console.log('AI响应已开始');

    // 等待工具卡片
    const toolCard = page.locator('[data-activity-type="tool_call"]');

    try {
      await expect(toolCard.first(), '工具卡片应该出现').toBeVisible({ timeout: 40000 });
      console.log('✅ 工具卡片已出现！');

      // 获取卡片信息
      const cardCount = await toolCard.count();
      console.log(`找到 ${cardCount} 个工具卡片`);

      // 获取卡片标题
      const firstCard = toolCard.first();
      const title = await firstCard.locator('.font-medium').textContent();
      const status = await firstCard.getAttribute('data-activity-status');

      console.log(`工具名称: ${title}`);
      console.log(`工具状态: ${status}`);

      // 截图
      await page.screenshot({ path: 'test-results/tool-card-success.png' });
      console.log('✅ 截图已保存');

    } catch (e) {
      console.log('❌ 未检测到工具卡片');
      await page.screenshot({ path: 'test-results/tool-card-failed.png' });

      // 打印页面内容用于调试
      const content = await page.content();
      console.log('页面内容预览:', content.substring(0, 500));

      throw e;
    }
  });
});
