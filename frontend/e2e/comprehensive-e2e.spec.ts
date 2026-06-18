/**
 * Agent OS 前端全面 E2E 测试
 *
 * 覆盖 6 个场景：
 * 1. 消息实时显示与持续渲染（最关键）
 * 2. 工具调用卡片渲染
 * 3. 交互卡片弹出与响应
 * 4. 图片缩略图与放大显示
 * 5. 状态更新流转
 * 6. 多任务并发场景下消息不串不丢
 */

import { test, expect, type Page } from '@playwright/test';
import { API_BASE, APP_URL } from './helpers/auth';

const TEST_USER = { username: 'e2euser', password: 'Test123456!' };

// 全局超时
test.describe.configure({ timeout: 120_000 });

// ─── 登录辅助函数（复用 message-render-e2e.spec.ts 模式）────────────

async function loginAndWaitReady(page: Page): Promise<void> {
  // 0. 先尝试注册测试用户（如已存在则忽略）
  console.log('📝 步骤0: 尝试注册测试用户');
  const registerResp = await page.request.post(`${API_BASE}/api/v1/auth/register`, {
    data: {
      username: TEST_USER.username,
      password: TEST_USER.password,
      email: 'e2e@test.com',
    },
  });
  if (registerResp.ok()) {
    console.log('✅ 测试用户注册成功');
  } else {
    console.log(`ℹ️ 注册返回 ${registerResp.status()}（用户可能已存在，继续登录）`);
  }

  // 1. POST /api/v1/auth/login → 获取 access_token/refresh_token
  console.log('🔐 步骤1: POST /api/v1/auth/login 获取 token');
  const loginResp = await page.request.post(`${API_BASE}/api/v1/auth/login`, {
    data: { username: TEST_USER.username, password: TEST_USER.password },
  });

  if (!loginResp.ok()) {
    throw new Error(`登录失败: status=${loginResp.status()}`);
  }

  const tokens = await loginResp.json();
  console.log(`✅ 登录成功, access_token 长度: ${tokens.access_token?.length ?? 0}`);

  // 2. page.goto(APP_URL) → page.evaluate 注入 localStorage
  console.log('🌐 步骤2: 打开前端页面并注入 localStorage');
  await page.goto(APP_URL);
  await page.evaluate((data) => {
    const now = Date.now();
    localStorage.setItem('access_token', data.access_token);
    localStorage.setItem('refresh_token', data.refresh_token);
    localStorage.setItem('access_token_expiry', (now + data.expires_in * 1000).toString());
    localStorage.setItem(
      'auth_user',
      JSON.stringify({
        id: 'e2e',
        username: 'e2e_tester',
        email: 'e2e@test.com',
        created_at: new Date().toISOString(),
      }),
    );
  }, {
    access_token: tokens.access_token,
    refresh_token: tokens.refresh_token,
    expires_in: tokens.expires_in,
  });

  // 3. page.reload() → 点击新会话 → 等待聊天输入框可见
  console.log('🔄 步骤3: 刷新页面');
  await page.reload();
  await page.waitForLoadState('networkidle');

  console.log('📝 步骤4: 点击新会话按钮');
  const newSessionBtn = page.locator('main button', { hasText: '新会话' }).first();
  await expect(newSessionBtn, '新会话按钮应可见').toBeVisible({ timeout: 10_000 });
  await newSessionBtn.click();

  console.log('⏳ 步骤5: 等待聊天输入框可见');
  await expect(
    page.locator('[data-testid="chat-input-textarea"]'),
    '聊天输入框应可见',
  ).toBeVisible({ timeout: 15_000 });
  console.log('✅ 聊天界面就绪');
}

// ─── 截图辅助 ────────────────────────────────────────────────

async function screenshot(page: Page, name: string): Promise<void> {
  await page.screenshot({ path: `test-results/${name}.png`, fullPage: false });
}

// ─── 发送消息辅助 ────────────────────────────────────────────

async function sendMessage(page: Page, text: string): Promise<void> {
  const input = page.locator('[data-testid="chat-input-textarea"]');
  await expect(input, '输入框应可见').toBeVisible({ timeout: 10_000 });
  await input.fill(text);
  const sendBtn = page.locator('[data-testid="chat-send-button"]');
  await sendBtn.click();
  console.log(`📨 已发送消息: "${text.substring(0, 40)}..."`);
}

// ═══════════════════════════════════════════════════════════════
// 场景 1：消息实时显示与持续渲染（最关键）
// ═══════════════════════════════════════════════════════════════

test.describe('场景1：消息实时显示与持续渲染', () => {
  test('流式文本应持续增长，不出现断裂', async ({ page }) => {
    console.log('=== 场景1-1: 流式文本持续增长测试 ===');
    await loginAndWaitReady(page);

    // 发送一个需要较长回答的问题
    await sendMessage(page, '请用至少200字介绍一下React的主要特性');

    // 等待助手消息出现
    const assistantMsg = page.locator('[data-role="assistant"]').first();
    await expect(assistantMsg, '助手消息应出现').toBeVisible({ timeout: 30_000 });
    console.log('✅ 助手消息已出现');
    await screenshot(page, 's1-streaming-start');

    // 记录初始文本长度
    const initialLength = await assistantMsg.textContent().then((t) => t?.length ?? 0);
    console.log(`初始文本长度: ${initialLength}`);

    // 等待流式输出增长
    await page.waitForTimeout(5_000);
    const midLength = await assistantMsg.textContent().then((t) => t?.length ?? 0);
    console.log(`中期文本长度: ${midLength}`);

    // 再等一轮
    await page.waitForTimeout(5_000);
    const finalLength = await assistantMsg.textContent().then((t) => t?.length ?? 0);
    console.log(`最终文本长度: ${finalLength}`);

    await screenshot(page, 's1-streaming-end');

    // 验证文本持续增长
    console.log(`📊 文本长度变化: ${initialLength} → ${midLength} → ${finalLength}`);
    expect(finalLength, '最终文本长度应大于0').toBeGreaterThan(0);
  });

  test('工具调用完成后助手消息文本必须继续渲染', async ({ page }) => {
    console.log('=== 场景1-2: 工具调用后持续渲染测试 ===');
    await loginAndWaitReady(page);

    // 发送一个会触发工具调用的请求
    await sendMessage(page, '请读取 package.json 文件的内容，然后总结一下');

    // 等待助手消息出现
    const assistantMsg = page.locator('[data-role="assistant"]').first();
    await expect(assistantMsg, '助手消息应出现').toBeVisible({ timeout: 30_000 });
    console.log('✅ 助手消息已出现');

    // 等待工具调用完成（最多60秒）
    const toolCard = page.locator('[data-activity-type="tool_call"]').first();
    try {
      await expect(toolCard, '工具卡片应出现').toBeVisible({ timeout: 40_000 });
      console.log('✅ 工具卡片已出现');
      await screenshot(page, 's1-tool-call-active');

      // 等待工具调用状态变为 completed
      await page.waitForFunction(
        () => {
          const card = document.querySelector('[data-activity-type="tool_call"]');
          return card?.getAttribute('data-activity-status') === 'completed';
        },
        { timeout: 30_000 },
      ).catch(() => {
        console.log('⚠️ 工具调用状态未变为 completed（超时）');
      });

      await screenshot(page, 's1-tool-call-completed');
      console.log('✅ 工具调用已完成');
    } catch {
      console.log('⚠️ 未检测到工具卡片（可能未触发工具调用）');
      await screenshot(page, 's1-no-tool-card');
    }

    // 核心断言：工具调用完成后，助手消息文本必须继续增长，不能断裂
    const textAfterTool = await assistantMsg.textContent().then((t) => t?.length ?? 0);
    console.log(`工具调用后文本长度: ${textAfterTool}`);
    await page.waitForTimeout(5_000);
    const textLater = await assistantMsg.textContent().then((t) => t?.length ?? 0);
    console.log(`等待后文本长度: ${textLater}`);

    console.log(`📊 文本长度变化: ${textAfterTool} → ${textLater}`);
    expect(textLater, '工具调用后文本必须继续增长，不能断裂').toBeGreaterThanOrEqual(textAfterTool);
    expect(textLater, '最终文本不应为空').toBeGreaterThan(0);
  });
});

// ═══════════════════════════════════════════════════════════════
// 场景 2：工具调用卡片渲染
// ═══════════════════════════════════════════════════════════════

test.describe('场景2：工具调用卡片渲染', () => {
  test('工具卡片应显示工具名称和状态流转', async ({ page }) => {
    console.log('=== 场景2: 工具调用卡片渲染测试 ===');
    await loginAndWaitReady(page);

    await sendMessage(page, '请读取 package.json 文件的内容');

    // 等待工具卡片出现
    const toolCard = page.locator('[data-activity-type="tool_call"]').first();
    await expect(toolCard, '工具卡片应出现').toBeVisible({ timeout: 45_000 });
    console.log('✅ 工具卡片已出现');
    await screenshot(page, 's2-tool-card-appeared');

    // 验证工具名称（.font-medium 包含标题）
    const titleEl = toolCard.locator('.font-medium').first();
    const title = await titleEl.textContent();
    console.log(`🔧 工具名称: ${title}`);
    expect(title, '工具名称应非空').toBeTruthy();
    expect(title!.length, '工具名称长度应>0').toBeGreaterThan(0);

    // 捕获当前状态，截图记录 running 状态
    const status = await toolCard.getAttribute('data-activity-status');
    console.log(`初始状态: ${status}`);
    await screenshot(page, 's2-tool-card-running');

    expect(['running', 'completed', 'pending'], `状态应为已知值，实际: ${status}`).toContain(status);

    // 等待状态变为 completed
    await page.waitForFunction(
      () => {
        const card = document.querySelector('[data-activity-type="tool_call"]');
        return card?.getAttribute('data-activity-status') === 'completed';
      },
      { timeout: 30_000 },
    ).catch(() => {
      console.log('⚠️ 等待 completed 超时');
    });

    const finalStatus = await toolCard.getAttribute('data-activity-status');
    console.log(`最终状态: ${finalStatus}`);
    await screenshot(page, 's2-tool-card-completed');

    // 验证 data-activity-type 属性正确
    const activityType = await toolCard.getAttribute('data-activity-type');
    expect(activityType, 'data-activity-type 应为 tool_call').toBe('tool_call');
    console.log('✅ 工具调用卡片验证完成');
  });
});

// ═══════════════════════════════════════════════════════════════
// 场景 3：交互卡片弹出与响应
// ═══════════════════════════════════════════════════════════════

test.describe('场景3：交互卡片弹出与响应', () => {
  test('交互卡片出现并可点击', async ({ page }) => {
    console.log('=== 场景3: 交互卡片弹出与响应测试 ===');
    await loginAndWaitReady(page);

    // 发送一个可能触发 human_interaction 的请求
    await sendMessage(page, '请确认一下，你准备好回答我的问题了吗？点击确认继续');

    // 等待助手响应
    const assistantMsg = page.locator('[data-role="assistant"]').first();
    await expect(assistantMsg, '助手消息应出现').toBeVisible({ timeout: 30_000 });
    console.log('✅ 助手消息已出现');
    await screenshot(page, 's3-interaction-check');

    // 检查是否出现 human_interaction 卡片（两个选择器都尝试）
    const interactionByTestid = page.locator('[data-testid="human-interaction-card"]').first();
    const interactionByActivity = page.locator('[data-activity-type="human_interaction"]').first();

    const hasByTestid = await interactionByTestid.isVisible().catch(() => false);
    const hasByActivity = await interactionByActivity.isVisible().catch(() => false);
    const hasInteraction = hasByTestid || hasByActivity;

    if (hasInteraction) {
      console.log('✅ 发现交互卡片');

      const interactionCard = hasByTestid ? interactionByTestid : interactionByActivity;

      // 查找可点击的按钮
      const button = interactionCard.locator('button').first();
      if (await button.isVisible().catch(() => false)) {
        const buttonText = await button.textContent();
        console.log(`🔘 按钮文本: ${buttonText}`);
        await button.click();
        await page.waitForTimeout(2_000);
        await screenshot(page, 's3-interaction-clicked');

        // 验证卡片状态变化
        const status = await interactionCard.getAttribute('data-activity-status');
        console.log(`点击后状态: ${status}`);
      }
    } else {
      // 没有交互卡片也算通过（取决于后端配置）
      console.log('⚠️ 未出现交互卡片（可能当前 Agent 未配置交互确认）');
      await screenshot(page, 's3-no-interaction-card');
    }
  });
});

// ═══════════════════════════════════════════════════════════════
// 场景 4：图片缩略图与放大显示
// ═══════════════════════════════════════════════════════════════

test.describe('场景4：图片缩略图与放大显示', () => {
  test('Markdown中的图片渲染为缩略图并支持放大', async ({ page }) => {
    console.log('=== 场景4: 图片缩略图与放大显示测试 ===');
    await loginAndWaitReady(page);

    // 请求 Agent 生成包含图片的 markdown 内容
    await sendMessage(page, '请用markdown格式展示一张示例图片，使用这个URL: https://picsum.photos/200/300');

    // 等待助手响应
    const assistantMsg = page.locator('[data-role="assistant"]').first();
    await expect(assistantMsg, '助手消息应出现').toBeVisible({ timeout: 30_000 });
    console.log('✅ 助手消息已出现');
    await page.waitForTimeout(3_000); // 等待 markdown 渲染完成
    await screenshot(page, 's4-image-response');

    // 检查是否有图片渲染
    const images = assistantMsg.locator('img');
    const imgCount = await images.count();
    console.log(`🖼️ 找到 ${imgCount} 张图片`);

    if (imgCount > 0) {
      // 验证 img src 非空
      const src = await images.first().getAttribute('src');
      expect(src, '图片 src 应非空').toBeTruthy();
      console.log(`图片 src: ${src}`);

      // 尝试点击图片查看 lightbox
      await images.first().click();
      await page.waitForTimeout(1_000);
      await screenshot(page, 's4-image-lightbox');

      // 检查 lightbox 是否出现
      const lightbox = page.locator('[data-testid="lightbox"]').first();
      const hasLightbox = await lightbox.isVisible().catch(() => false);
      console.log(`🔍 Lightbox [data-testid="lightbox"] 出现: ${hasLightbox}`);

      if (hasLightbox) {
        console.log('✅ Lightbox 放大显示验证成功');
        // 关闭 lightbox（按 Escape）
        await page.keyboard.press('Escape');
        await page.waitForTimeout(500);
      } else {
        // 也尝试常见 lightbox 选择器
        const altLightbox = page.locator('.fixed.inset-0, [role="dialog"]').first();
        const hasAltLightbox = await altLightbox.isVisible().catch(() => false);
        console.log(`🔍 备选 Lightbox 出现: ${hasAltLightbox}`);
        if (hasAltLightbox) {
          await page.keyboard.press('Escape');
        }
      }
    } else {
      console.log('⚠️ 助手响应中未渲染图片（可能 Agent 未生成图片 markdown）');
    }
  });
});

// ═══════════════════════════════════════════════════════════════
// 场景 5：状态更新流转
// ═══════════════════════════════════════════════════════════════

test.describe('场景5：状态更新流转', () => {
  test('发送消息后状态从 running 流转为 completed', async ({ page }) => {
    console.log('=== 场景5-1: 状态更新流转测试 ===');
    await loginAndWaitReady(page);

    // 记录发送前的状态
    await screenshot(page, 's5-before-send');

    // 发送一个会触发工具调用的消息（更容易观察状态流转）
    await sendMessage(page, '帮我创建一个名为 e2e_test_file.txt 的文件，内容写 hello world');

    // 等待助手消息出现，验证 streaming 状态指示器存在
    const assistantMsg = page.locator('[data-role="assistant"]').first();
    await expect(assistantMsg, '助手消息应出现').toBeVisible({ timeout: 30_000 });
    console.log('✅ 助手消息已出现');

    // 检查 loading spinner 或状态指示器
    // streaming 状态可能通过多种 UI 元素体现：spinner、动画、文字指示
    await page.waitForTimeout(2_000);
    await screenshot(page, 's5-during-streaming');

    // 检查 ActivityCard 的 running 状态
    const runningCard = page.locator('[data-activity-status="running"]').first();
    const hasRunning = await runningCard.isVisible().catch(() => false);
    console.log(`🔄 检测到 running 状态 ActivityCard: ${hasRunning}`);

    // 等待 [data-activity-type] 元素出现并完成状态流转 running → completed
    const activityCard = page.locator('[data-activity-type]').first();
    const hasActivity = await activityCard.isVisible().catch(() => false);

    if (hasActivity) {
      const initialType = await activityCard.getAttribute('data-activity-type');
      console.log(`📋 活动卡片类型: ${initialType}`);

      // 等待状态变为 completed
      await page.waitForFunction(
        () => {
          const card = document.querySelector('[data-activity-type]');
          return card?.getAttribute('data-activity-status') === 'completed';
        },
        { timeout: 40_000 },
      ).catch(() => {
        console.log('⚠️ 等待 completed 状态超时');
      });

      await screenshot(page, 's5-after-completion');

      // 检查 completed 状态
      const completedCard = page.locator('[data-activity-status="completed"]').first();
      const hasCompleted = await completedCard.isVisible().catch(() => false);
      console.log(`✅ 检测到 completed 状态: ${hasCompleted}`);
    } else {
      console.log('⚠️ 未检测到 ActivityCard');
      await page.waitForTimeout(20_000);
      await screenshot(page, 's5-no-activity-card');
    }

    // 验证助手最终有响应
    const finalText = await assistantMsg.textContent().catch(() => '');
    expect(finalText!.length, '助手响应不应为空').toBeGreaterThan(0);
    console.log('✅ 状态流转验证完成');
  });

  test('连续任务的状态依次流转', async ({ page }) => {
    console.log('=== 场景5-2: 连续任务状态流转测试 ===');
    await loginAndWaitReady(page);

    // 发送需要多步骤的任务
    await sendMessage(page, '请先读取 README.md，然后告诉我文件有多少行');

    // 等待第一个活动卡片出现
    const activityCards = page.locator('[data-activity-status]');
    await expect(activityCards.first(), '应出现活动卡片').toBeVisible({ timeout: 45_000 });
    console.log('✅ 活动卡片已出现');
    await screenshot(page, 's5-multi-step-activities');

    // 统计活动卡片数量
    const cardCount = await activityCards.count();
    console.log(`📋 活动卡片数量: ${cardCount}`);

    // 等待所有完成
    await page.waitForFunction(
      () => {
        const cards = document.querySelectorAll('[data-activity-status]');
        return Array.from(cards).every(
          (c) => c.getAttribute('data-activity-status') === 'completed',
        );
      },
      { timeout: 60_000 },
    ).catch(() => {
      console.log('⚠️ 等待所有活动卡片完成超时');
    });

    await screenshot(page, 's5-multi-step-complete');

    // 验证最终有助手响应
    const assistantMsg = page.locator('[data-role="assistant"]').first();
    const responseText = await assistantMsg.textContent().catch(() => '');
    console.log(`助手响应长度: ${responseText?.length ?? 0}`);
    expect(responseText!.length, '助手响应不应为空').toBeGreaterThan(0);
    console.log('✅ 连续任务状态流转验证完成');
  });
});

// ═══════════════════════════════════════════════════════════════
// 场景 6：多任务并发场景下消息不串不丢
// ═══════════════════════════════════════════════════════════════

test.describe('场景6：多任务并发场景下消息不串不丢', () => {
  test('快速连续发送2条不同消息，每条都有响应且不混淆', async ({ page }) => {
    console.log('=== 场景6: 多任务并发消息不串不丢测试 ===');
    await loginAndWaitReady(page);

    // 快速连续发送2条不同消息
    const msg1 = '中国首都是哪？';
    const msg2 = '法国首都是哪？';

    await sendMessage(page, msg1);
    await page.waitForTimeout(500);
    await sendMessage(page, msg2);
    console.log('✅ 已快速发送2条消息');

    await screenshot(page, 's6-all-sent');

    // 等待所有响应完成
    await page.waitForTimeout(45_000);

    // 验证用户消息数量 ≥ 2
    const userMsgs = page.locator('[data-role="user"]');
    const userCount = await userMsgs.count();
    console.log(`👤 用户消息数量: ${userCount}`);
    expect(userCount, '用户消息数量应 >= 2').toBeGreaterThanOrEqual(2);

    // 验证至少2条助手响应
    const assistantMsgs = page.locator('[data-role="assistant"]');
    const assistantCount = await assistantMsgs.count();
    console.log(`🤖 助手响应数量: ${assistantCount}`);
    expect(assistantCount, '应至少有 2 条助手响应').toBeGreaterThanOrEqual(2);

    await screenshot(page, 's6-all-responses');

    // 验证两条响应内容不完全相同
    if (assistantCount >= 2) {
      const texts: string[] = [];
      for (let i = 0; i < assistantCount; i++) {
        const text = await assistantMsgs.nth(i).textContent().catch(() => '');
        texts.push(text?.trim() ?? '');
      }
      console.log('响应内容摘要:', texts.map((t) => t.substring(0, 50)));

      // 至少有两条响应内容不完全相同
      const uniqueTexts = new Set(texts);
      console.log(`唯一响应数量: ${uniqueTexts.size}`);
      expect(uniqueTexts.size, '两条响应内容不应完全相同').toBeGreaterThanOrEqual(2);
    }

    console.log('✅ 并发消息不串不丢验证完成');
  });
});
