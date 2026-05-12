/**
 * E2E 测试辅助函数使用示例
 *
 * 本文件展示了如何使用 helpers.ts 中的各种辅助函数来编写 E2E 测试
 */

import { test, expect } from '@playwright/test';
import {
  // 登录相关
  login,
  logout,
  quickLogin,
  loginViaAPI,
  logoutAndCleanup,

  // 网络监听
  waitForAPI,
  waitForMultipleAPIs,
  waitForAPIResponse,
  verifyAPIStatus,
  getAPIData,

  // 状态记录
  recordState,
  compareStates,
  waitForStateChange,
  recordElementCount,
  verifyElementCountChanged,

  // 会话管理
  createSession,
  sendMessage,
  waitForAIResponse,
  getAllMessages,

  // 主题相关
  getCurrentTheme,
  switchTheme,
  verifyTheme,
  waitForThemeTransition,
  getThemeColors,

  // 其他工具
  waitForPageLoad,
  takeScreenshot,
  checkToast,
  fillForm,
  waitAndClick,
  waitForElement,
  waitForElementRemoved,
  clearAndFill,
  selectDropdown,
  waitForToast,
  waitForSuccessMessage,
  waitForErrorMessage,
  verifyURL,
  getStorageState,
  setStorageState,

  // 测试用户凭据
  testUser,
} from './helpers';

test.describe('辅助函数使用示例', () => {
  // 示例 1: 快速登录测试
  test('示例 1: 使用快速登录', async ({ page }) => {
    // 使用 quickLogin 自动处理登录
    await quickLogin(page);

    // 验证已登录
    await expect(page.locator('[data-testid="user-menu"]')).toBeVisible();

    await takeScreenshot(page, 'example-1-quick-login');
  });

  // 示例 2: 通过 API 登录（更快）
  test('示例 2: 使用 API 登录', async ({ page }) => {
    // 直接通过 API 登录，跳过 UI 操作
    const userData = await loginViaAPI(page);

    console.log('登录用户数据:', userData);

    // 验证登录成功
    const token = await getStorageState(page, 'token');
    expect(token).toBeTruthy();
  });

  // 示例 3: 监听网络请求
  test('示例 3: 监听 API 请求', async ({ page }) => {
    await quickLogin(page);

    // 监听创建会话的 POST 请求
    const createRequest = waitForAPI(page, '/api/sessions', 'POST');

    // 执行创建会话的操作
    await page.goto('/sessions');
    await waitAndClick(page, 'button:has-text("创建")');
    await page.fill('input[name="name"]', '测试会话');
    await page.click('button:has-text("确认")');

    // 等待请求完成
    const request = await createRequest;
    console.log('创建会话请求:', request.url());
    console.log('请求数据:', await request.postData());

    await waitForSuccessMessage(page);
  });

  // 示例 4: 监听多个 API 请求
  test('示例 4: 并行监听多个 API 请求', async ({ page }) => {
    await quickLogin(page);

    // 同时监听多个请求
    const [createReq, updateReq] = await waitForMultipleAPIs(page, [
      { endpoint: '/api/sessions', method: 'POST' },
      { endpoint: '/api/messages', method: 'POST' }
    ]);

    console.log('两个请求都已完成:', {
      create: createReq.url(),
      update: updateReq.url()
    });
  });

  // 示例 5: 状态记录和比较
  test('示例 5: 记录并比较页面状态', async ({ page }) => {
    await quickLogin(page);

    // 记录初始状态
    const beforeState = await recordState(page, {
      username: '.user-name',
      sessionCount: '.session-count',
      createButton: 'button:has-text("创建会话")'
    });

    console.log('初始状态:', beforeState);

    // 执行操作：创建会话
    await page.goto('/sessions');
    await waitAndClick(page, 'button:has-text("创建")');
    await page.fill('input[name="name"]', '新会话');
    await page.click('button:has-text("确认")');
    await waitForSuccessMessage(page);

    // 记录操作后状态
    const afterState = await recordState(page, {
      username: '.user-name',
      sessionCount: '.session-count',
      createButton: 'button:has-text("创建会话")'
    });

    console.log('操作后状态:', afterState);

    // 比较差异
    const diff = compareStates(beforeState, afterState);
    console.log('状态变化:', diff);
  });

  // 示例 6: 等待状态变化
  test('示例 6: 等待元素状态变化', async ({ page }) => {
    await quickLogin(page);

    // 等待状态文本变化
    const { before, after } = await waitForStateChange(page, '.status-text');
    console.log('状态从', before, '变为', after);
  });

  // 示例 7: 创建会话
  test('示例 7: 创建会话', async ({ page }) => {
    await quickLogin(page);

    // 使用辅助函数创建会话
    const session = await createSession(page, {
      name: '测试会话',
      description: '这是一个测试会话',
      agentId: 'default'
    });

    console.log('创建的会话:', session);
    expect(session.id).toBeTruthy();

    await takeScreenshot(page, 'example-7-create-session');
  });

  // 示例 8: 发送消息并等待响应
  test('示例 8: 发送消息和接收响应', async ({ page }) => {
    await quickLogin(page);

    // 创建会话
    const session = await createSession(page, {
      name: '聊天测试'
    });

    // 发送消息
    await sendMessage(page, session.id, '你好，请介绍一下你自己');

    // 等待 AI 响应
    const response = await waitForAIResponse(page, 30000);
    console.log('AI 响应:', response);

    // 获取所有消息
    const messages = await getAllMessages(page);
    console.log('会话中的所有消息:', messages);

    // 验证消息数量
    expect(messages.length).toBeGreaterThanOrEqual(2); // 用户消息 + AI 响应
  });

  // 示例 9: 主题切换测试
  test('示例 9: 切换主题', async ({ page }) => {
    await quickLogin(page);

    // 获取当前主题
    const initialTheme = await getCurrentTheme(page);
    console.log('当前主题:', initialTheme);

    // 切换到深色模式
    await switchTheme(page, 'dark');

    // 验证主题已切换
    await verifyTheme(page, 'dark');

    // 等待切换动画完成
    await waitForThemeTransition(page);

    // 获取主题颜色
    const colors = await getThemeColors(page, 'dark');
    console.log('深色主题颜色:', colors);

    await takeScreenshot(page, 'example-9-dark-theme');

    // 切换回浅色模式
    await switchTheme(page, 'light');
    await verifyTheme(page, 'light');

    await takeScreenshot(page, 'example-9-light-theme');
  });

  // 示例 10: 元素数量验证
  test('示例 10: 验证元素数量变化', async ({ page }) => {
    await quickLogin(page);

    await page.goto('/sessions');

    // 记录初始会话数量
    const initialCount = await recordElementCount(page, '.session-item');
    console.log('初始会话数:', initialCount);

    // 创建新会话
    await createSession(page, { name: '新会话' });

    // 验证数量增加
    await verifyElementCountChanged(page, '.session-item', initialCount, 'increase');

    // 获取新的数量
    const newCount = await recordElementCount(page, '.session-item');
    console.log('新会话数:', newCount);
  });

  // 示例 11: 完整的登录流程测试
  test('示例 11: 完整登录流程', async ({ page }) => {
    // 记录登录前状态
    const beforeLogin = await recordState(page, {
      loginForm: 'form[data-testid="login-form"]',
      userMenu: '[data-testid="user-menu"]',
      token: 'localStorage-token'
    });

    console.log('登录前状态:', beforeLogin);

    // 使用自定义账号登录
    await login(page, 'testuser', 'testpass123');

    // 记录登录后状态
    const afterLogin = await recordState(page, {
      loginForm: 'form[data-testid="login-form"]',
      userMenu: '[data-testid="user-menu"]',
      token: 'localStorage-token'
    });

    console.log('登录后状态:', afterLogin);

    // 比较登录前后变化
    const loginDiff = compareStates(beforeLogin, afterLogin);
    console.log('登录变化:', loginDiff);

    // 验证登录成功
    await expect(page.locator('[data-testid="user-menu"]')).toBeVisible();

    // 登出并清理
    await logoutAndCleanup(page);

    // 验证已登出
    await expect(page.locator('[data-testid="user-menu"]')).not.toBeVisible();
  });

  // 示例 12: 表单填写和提交
  test('示例 12: 表单填写', async ({ page }) => {
    await quickLogin(page);

    await page.goto('/settings');

    // 使用 fillForm 批量填写
    await fillForm(page, {
      'input[name="configName"]': '测试配置',
      'input[name="apiKey"]': 'test-api-key-123',
      'textarea[name="description"]': '这是一个测试配置'
    });

    // 点击保存
    await waitAndClick(page, 'button[type="submit"]');

    // 等待成功消息
    await waitForSuccessMessage(page);

    await takeScreenshot(page, 'example-12-form-submission');
  });

  // 示例 13: 等待元素出现和消失
  test('示例 13: 等待加载完成', async ({ page }) => {
    await quickLogin(page);

    await page.goto('/sessions');

    // 等待加载动画出现
    await waitForElement(page, '.loading-spinner');

    // 执行操作触发加载
    await page.click('button:has-text("刷新")');

    // 等待加载动画消失
    await waitForElementRemoved(page, '.loading-spinner');

    console.log('加载完成');
  });

  // 示例 14: 下拉选择
  test('示例 14: 下拉选择', async ({ page }) => {
    await quickLogin(page);

    await page.goto('/settings');

    // 选择下拉选项
    await selectDropdown(page, '.model-select', 'GPT-4');

    // 等待选择生效
    await waitForElement(page, '.model-select:has-text("GPT-4")');

    await takeScreenshot(page, 'example-14-dropdown');
  });

  // 示例 15: URL 验证
  test('示例 15: 验证路由跳转', async ({ page }) => {
    await quickLogin(page);

    // 点击导航链接
    await page.click('a:has-text("会话")');

    // 验证 URL 已变化
    await verifyURL(page, '/sessions');

    // 点击进入详情页
    await page.click('.session-item:first-child');

    // 验证新的 URL
    await verifyURL(page, /\/sessions\/[a-f0-9-]+/);
  });

  // 示例 16: 完整的会话工作流
  test('示例 16: 完整会话工作流', async ({ page }) => {
    // 1. 登录
    await quickLogin(page);

    // 2. 创建会话
    const session = await createSession(page, {
      name: '完整测试会话',
      description: '测试完整工作流'
    });

    // 3. 发送第一条消息
    await sendMessage(page, session.id, '你好');

    // 4. 等待响应
    const response1 = await waitForAIResponse(page);
    console.log('第一条响应:', response1);

    // 5. 发送第二条消息
    await sendMessage(page, session.id, '请再说详细点');

    // 6. 等待响应
    const response2 = await waitForAIResponse(page);
    console.log('第二条响应:', response2);

    // 7. 获取所有消息
    const messages = await getAllMessages(page);
    console.log('总消息数:', messages.length);

    // 8. 验证消息结构
    expect(messages[0].role).toBe('user');
    expect(messages[1].role).toBe('assistant');

    // 9. 截图保存
    await takeScreenshot(page, 'example-16-complete-workflow');
  });

  // 示例 17: 错误处理测试
  test('示例 17: 测试错误消息', async ({ page }) => {
    await quickLogin(page);

    await page.goto('/settings');

    // 尝试提交无效表单
    await page.click('button[type="submit"]');

    // 等待错误消息
    await waitForErrorMessage(page);

    // 或者验证特定的错误消息
    await waitForToast(page, '请填写必填项');

    await takeScreenshot(page, 'example-17-error-message');
  });

  // 示例 18: 清空并填写
  test('示例 18: 清空并重新填写输入框', async ({ page }) => {
    await quickLogin(page);

    await page.goto('/settings');

    // 清空并填写新的值
    await clearAndFill(page, 'input[name="configName"]', '新配置名');

    // 验证值已更新
    const value = await page.inputValue('input[name="configName"]');
    expect(value).toBe('新配置名');
  });

  // 示例 19: 并发操作测试
  test('示例 19: 测试并发请求', async ({ page }) => {
    await quickLogin(page);

    await page.goto('/sessions');

    // 同时触发多个操作
    const [refreshReq, countReq] = await waitForMultipleAPIs(page, [
      { endpoint: '/api/sessions', method: 'GET' },
      { endpoint: '/api/sessions/count', method: 'GET' }
    ]);

    console.log('所有并发请求已完成');
  });

  // 示例 20: 清理测试数据
  test.afterEach(async ({ page }) => {
    // 每个测试后清理状态
    // 注意：这不会真的运行，因为被注释了
    // 如果需要，可以在每个 test 中调用
    //
    // await logoutAndCleanup(page);
  });
});
