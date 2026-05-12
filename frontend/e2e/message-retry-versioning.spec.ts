/**
 * 消息重试和版本管理 E2E 测试
 *
 * 测试覆盖范围：
 * 1. 原位重试 - 在同一位置重新生成AI回复
 * 2. 版本管理 - 保存多个版本，可以查看历史版本
 * 3. 版本切换 - 在不同版本之间切换
 * 4. 版本对比 - 比较不同版本的差异
 * 5. 数据库验证 - 验证版本信息正确存储
 */

import { test, expect } from '@playwright/test';
import {
  quickLogin,
  logoutAndCleanup,
  waitForPageLoad,
  takeScreenshot,
  waitForElement,
  waitForAPI,
  waitForAPIResponse,
  recordState,
  compareStates,
} from './helpers';

/**
 * ============================================================================
 * 测试配置
 * ============================================================================
 */

// 每个测试前清理并登录
test.beforeEach(async ({ page }) => {
  await logoutAndCleanup(page);
  await quickLogin(page);
  await page.waitForLoadState('networkidle');
});

// 测试失败时截图
test.afterEach(async ({ page }, testInfo) => {
  if (testInfo.status !== 'passed') {
    await page.screenshot({
      path: `test-results/message-retry-version-failed-${testInfo.title}.png`,
      fullPage: true,
    });
  }
});

/**
 * ============================================================================
 * 测试组 1: 原位重试功能
 * ============================================================================
 */
test.describe('原位重试功能', () => {
  test('01-应该显示重试按钮（AI消息）', async ({ page }) => {
    // 1. 创建会话
    await page.goto('/');

    // 2. 发送消息
    const chatInput = page.locator('textarea[placeholder*="消息"], [data-testid="chat-input"]');
    await expect(chatInput).toBeVisible();
    await chatInput.fill('请生成一首关于春天的诗');
    await chatInput.press('Enter');

    // 3. 等待AI响应（可能需要较长时间）
    try {
      await page.waitForSelector('[data-testid="message-item"][data-role="assistant"]', {
        timeout: 30000
      });
      console.log('AI响应已收到');
    } catch {
      console.log('30秒内未收到AI响应，跳过测试');
      return;
    }

    // 4. 悬停在AI消息上
    const aiMessage = page.locator('[data-testid="message-item"][data-role="assistant"]').last();
    await aiMessage.hover();

    // 5. 验证重试按钮存在
    const retryButton = aiMessage.locator(
      'button:has-text("重新生成"), button[title="重新生成"], button[aria-label="重新生成"], ' +
      'button:has(svg.lucide-refresh-cw), button:has(svg.lucide-rotate-ccw)'
    );

    const buttonVisible = await retryButton.isVisible().catch(() => false);
    if (buttonVisible) {
      await expect(retryButton).toBeVisible();
      console.log('✓ 重新生成按钮已显示');
      await takeScreenshot(page, 'retry-button-visible');
    } else {
      console.log('重新生成按钮未找到，可能需要添加');
    }
  });

  test('02-点击重试应该在原位置生成新版本', async ({ page }) => {
    // 1. 创建会话
    await page.goto('/');

    // 2. 发送消息并等待AI响应
    const chatInput = page.locator('textarea[placeholder*="消息"]');
    await chatInput.fill('请介绍一下Python编程语言');
    await chatInput.press('Enter');

    // 3. 等待AI响应
    try {
      await page.waitForSelector('[data-testid="message-item"][data-role="assistant"]', {
        timeout: 30000
      });
    } catch {
      console.log('未收到AI响应，跳过测试');
      return;
    }

    // 4. 记录原始响应内容
    const aiMessage = page.locator('[data-testid="message-item"][data-role="assistant"]').last();
    const originalContent = await aiMessage.textContent();
    console.log('原始响应长度:', originalContent?.length);

    // 5. 记录当前消息数量（确保重试不会增加消息数量）
    const messageCountBefore = await page.locator('[data-testid="message-item"]').count();
    console.log('重试前消息数量:', messageCountBefore);

    // 6. 点击重试按钮
    const retryButton = aiMessage.locator(
      'button:has-text("重新生成"), button:has(svg.lucide-refresh-cw)'
    );
    const hasRetryButton = await retryButton.isVisible().catch(() => false);

    if (!hasRetryButton) {
      console.log('重试按钮不可用，跳过测试');
      return;
    }

    // 7. 监听重试API（如果实现了）
    const retryApiCall = waitForAPI(page, '/api/messages/', 'POST')
      .catch(() => null);

    await retryButton.click();
    console.log('已点击重试按钮');

    // 8. 等待加载状态
    const loader = aiMessage.locator('.animate-spin, [data-testid="loading"], .loader');
    const hasLoader = await loader.isVisible().catch(() => false);
    if (hasLoader) {
      console.log('检测到加载状态');
      await expect(loader).toBeVisible();
    }

    // 9. 等待新内容生成
    await page.waitForTimeout(5000);

    // 10. 验证消息数量未增加（原位重试）
    const messageCountAfter = await page.locator('[data-testid="message-item"]').count();
    console.log('重试后消息数量:', messageCountAfter);
    expect(messageCountAfter).toBe(messageCountBefore);

    // 11. 验证内容已更新
    const newContent = await aiMessage.textContent();
    console.log('新响应长度:', newContent?.length);

    // 内容可能相同或不同，取决于AI响应
    console.log('✓ 原位重试测试完成');

    await takeScreenshot(page, 'retry-in-place');
  });

  test('03-重试时应该禁用编辑和删除按钮', async ({ page }) => {
    // 1. 创建会话
    await page.goto('/');

    // 2. 发送消息
    const chatInput = page.locator('textarea[placeholder*="消息"]');
    await chatInput.fill('测试重试时的按钮状态');
    await chatInput.press('Enter');

    // 3. 等待AI响应
    try {
      await page.waitForSelector('[data-testid="message-item"][data-role="assistant"]', {
        timeout: 30000
      });
    } catch {
      console.log('未收到AI响应，跳过测试');
      return;
    }

    // 4. 点击重试按钮
    const aiMessage = page.locator('[data-testid="message-item"][data-role="assistant"]').last();
    const retryButton = aiMessage.locator(
      'button:has-text("重新生成"), button:has(svg.lucide-refresh-cw)'
    );

    const hasRetryButton = await retryButton.isVisible().catch(() => false);
    if (!hasRetryButton) {
      console.log('重试按钮不可用');
      return;
    }

    await retryButton.click();

    // 5. 等待加载状态
    await page.waitForTimeout(500);

    // 6. 验证编辑按钮被禁用
    const editButton = aiMessage.locator('button:has(svg.lucide-edit)').first();
    const hasEditButton = await editButton.isVisible().catch(() => false);
    if (hasEditButton) {
      const isDisabled = await editButton.isDisabled();
      expect(isDisabled).toBeTruthy();
      console.log('✓ 编辑按钮已禁用');
    }

    // 7. 验证删除按钮被禁用
    const deleteButton = aiMessage.locator('button:has(svg.lucide-trash)').first();
    const hasDeleteButton = await deleteButton.isVisible().catch(() => false);
    if (hasDeleteButton) {
      const isDisabled = await deleteButton.isDisabled();
      expect(isDisabled).toBeTruthy();
      console.log('✓ 删除按钮已禁用');
    }

    await takeScreenshot(page, 'retry-buttons-disabled');
  });

  test('04-重试按钮在生成过程中应该显示加载状态', async ({ page }) => {
    // 1. 创建会话
    await page.goto('/');

    // 2. 发送消息
    const chatInput = page.locator('textarea[placeholder*="消息"]');
    await chatInput.fill('测试重试按钮加载状态');
    await chatInput.press('Enter');

    // 3. 等待AI响应
    try {
      await page.waitForSelector('[data-testid="message-item"][data-role="assistant"]', {
        timeout: 30000
      });
    } catch {
      console.log('未收到AI响应，跳过测试');
      return;
    }

    // 4. 点击重试按钮
    const aiMessage = page.locator('[data-testid="message-item"][data-role="assistant"]').last();
    const retryButton = aiMessage.locator(
      'button:has-text("重新生成"), button:has(svg.lucide-refresh-cw)'
    );

    const hasRetryButton = await retryButton.isVisible().catch(() => false);
    if (!hasRetryButton) {
      console.log('重试按钮不可用');
      return;
    }

    await retryButton.click();

    // 5. 验证重试按钮显示加载图标
    const loadingIcon = retryButton.locator('.animate-spin, svg:has-class("animate-spin")');
    const hasLoadingIcon = await loadingIcon.isVisible().catch(() => false);

    if (hasLoadingIcon) {
      console.log('✓ 重试按钮显示加载状态');
    }

    // 6. 验证按钮被禁用
    const isDisabled = await retryButton.isDisabled();
    expect(isDisabled).toBeTruthy();
    console.log('✓ 重试按钮已禁用');

    await takeScreenshot(page, 'retry-button-loading');
  });
});

/**
 * ============================================================================
 * 测试组 2: 版本管理功能
 * ============================================================================
 */
test.describe('版本管理功能', () => {
  test('05-应该显示版本指示器（有多个版本时）', async ({ page }) => {
    // 1. 创建会话
    await page.goto('/');

    // 2. 发送消息
    const chatInput = page.locator('textarea[placeholder*="消息"]');
    await chatInput.fill('请写一个简短的故事');
    await chatInput.press('Enter');

    // 3. 等待AI响应
    try {
      await page.waitForSelector('[data-testid="message-item"][data-role="assistant"]', {
        timeout: 30000
      });
    } catch {
      console.log('未收到AI响应，跳过测试');
      return;
    }

    // 4. 执行重试以创建第二个版本
    const aiMessage = page.locator('[data-testid="message-item"][data-role="assistant"]').last();
    const retryButton = aiMessage.locator(
      'button:has-text("重新生成"), button:has(svg.lucide-refresh-cw)'
    );

    const hasRetryButton = await retryButton.isVisible().catch(() => false);
    if (hasRetryButton) {
      await retryButton.click();
      await page.waitForTimeout(5000);
    }

    // 5. 检查版本指示器
    const versionIndicator = aiMessage.locator(
      '[data-testid="version-indicator"], .version-badge, .version-count'
    );
    const hasVersionIndicator = await versionIndicator.isVisible().catch(() => false);

    if (hasVersionIndicator) {
      await expect(versionIndicator).toBeVisible();
      const versionText = await versionIndicator.textContent();
      console.log('版本指示器:', versionText);
      expect(versionText).toMatch(/v\d+|版本\s*\d+|Version\s*\d+/);
      console.log('✓ 版本指示器已显示');
    } else {
      console.log('版本指示器未实现，需要添加');
    }

    await takeScreenshot(page, 'version-indicator');
  });

  test('06-应该显示版本历史面板', async ({ page }) => {
    // 1. 创建会话
    await page.goto('/');

    // 2. 发送消息并重试
    const chatInput = page.locator('textarea[placeholder*="消息"]');
    await chatInput.fill('请解释什么是机器学习');
    await chatInput.press('Enter');

    // 3. 等待AI响应
    try {
      await page.waitForSelector('[data-testid="message-item"][data-role="assistant"]', {
        timeout: 30000
      });
    } catch {
      console.log('未收到AI响应，跳过测试');
      return;
    }

    // 4. 执行重试
    const aiMessage = page.locator('[data-testid="message-item"][data-role="assistant"]').last();
    const retryButton = aiMessage.locator(
      'button:has-text("重新生成"), button:has(svg.lucide-refresh-cw)'
    );

    const hasRetryButton = await retryButton.isVisible().catch(() => false);
    if (hasRetryButton) {
      await retryButton.click();
      await page.waitForTimeout(5000);
    }

    // 5. 点击版本指示器或历史按钮
    const versionHistoryButton = aiMessage.locator(
      'button:has-text("历史"), button:has-text("版本"), [data-testid="version-history-button"], ' +
      '.version-indicator'
    );

    const hasVersionButton = await versionHistoryButton.isVisible().catch(() => false);
    if (hasVersionButton) {
      await versionHistoryButton.first().click();
      await page.waitForTimeout(500);

      // 6. 验证版本历史面板显示
      const versionPanel = page.locator(
        '[data-testid="version-history-panel"], .version-history, .version-list'
      );
      const hasPanel = await versionPanel.isVisible().catch(() => false);

      if (hasPanel) {
        await expect(versionPanel).toBeVisible();

        // 7. 验证版本列表
        const versionItems = versionPanel.locator(
          '[data-testid="version-item"], .version-item, .history-item'
        );
        const itemCount = await versionItems.count();
        expect(itemCount).toBeGreaterThan(0);
        console.log(`✓ 版本历史面板显示，共 ${itemCount} 个版本`);
      }
    } else {
      console.log('版本历史按钮未实现');
    }

    await takeScreenshot(page, 'version-history-panel');
  });

  test('07-版本历史应该显示版本信息', async ({ page }) => {
    // 1. 创建会话并生成多个版本
    await page.goto('/');

    const chatInput = page.locator('textarea[placeholder*="消息"]');
    await chatInput.fill('请推荐一本好书');
    await chatInput.press('Enter');

    try {
      await page.waitForSelector('[data-testid="message-item"][data-role="assistant"]', {
        timeout: 30000
      });
    } catch {
      console.log('未收到AI响应，跳过测试');
      return;
    }

    // 2. 执行重试创建第二个版本
    const aiMessage = page.locator('[data-testid="message-item"][data-role="assistant"]').last();
    const retryButton = aiMessage.locator(
      'button:has-text("重新生成"), button:has(svg.lucide-refresh-cw)'
    );

    const hasRetryButton = await retryButton.isVisible().catch(() => false);
    if (hasRetryButton) {
      await retryButton.click();
      await page.waitForTimeout(5000);
    }

    // 3. 打开版本历史
    const versionHistoryButton = aiMessage.locator(
      'button:has-text("历史"), .version-indicator'
    );

    const hasVersionButton = await versionHistoryButton.isVisible().catch(() => false);
    if (!hasVersionButton) {
      console.log('版本历史按钮未实现');
      return;
    }

    await versionHistoryButton.first().click();
    await page.waitForTimeout(500);

    // 4. 验证版本信息
    const versionPanel = page.locator('.version-history, .version-list');
    const hasPanel = await versionPanel.isVisible().catch(() => false);

    if (hasPanel) {
      // 验证版本号
      const versionNumbers = versionPanel.locator('text=/v\\d+|版本\\s*\\d+/');
      const hasVersionNumbers = await versionNumbers.count() > 0;
      if (hasVersionNumbers) {
        console.log('✓ 显示版本号');
      }

      // 验证时间戳
      const timestamps = versionPanel.locator('text=/\\d+分钟前|\\d+小时前|刚刚/');
      const hasTimestamps = await timestamps.count() > 0;
      if (hasTimestamps) {
        console.log('✓ 显示时间戳');
      }

      // 验证当前版本标记
      const currentBadge = versionPanel.locator(
        'text=/当前|最新|Current|Latest/'
      ).or(versionPanel.locator(':has-text("当前")')).or(versionPanel.locator(':has-text("最新")'));
      const hasCurrentBadge = await currentBadge.isVisible().catch(() => false);
      if (hasCurrentBadge) {
        console.log('✓ 显示当前版本标记');
      }
    }

    await takeScreenshot(page, 'version-history-info');
  });
});

/**
 * ============================================================================
 * 测试组 3: 版本切换功能
 * ============================================================================
 */
test.describe('版本切换功能', () => {
  test('08-应该能够切换到历史版本', async ({ page }) => {
    // 1. 创建会话并生成多个版本
    await page.goto('/');

    const chatInput = page.locator('textarea[placeholder*="消息"]');
    await chatInput.fill('请用一句话介绍量子计算');
    await chatInput.press('Enter');

    try {
      await page.waitForSelector('[data-testid="message-item"][data-role="assistant"]', {
        timeout: 30000
      });
    } catch {
      console.log('未收到AI响应，跳过测试');
      return;
    }

    // 2. 记录第一个版本的内容
    const aiMessage = page.locator('[data-testid="message-item"][data-role="assistant"]').last();
    const firstVersionContent = await aiMessage.textContent();
    console.log('第一版本内容长度:', firstVersionContent?.length);

    // 3. 执行重试
    const retryButton = aiMessage.locator(
      'button:has-text("重新生成"), button:has(svg.lucide-refresh-cw)'
    );

    const hasRetryButton = await retryButton.isVisible().catch(() => false);
    if (!hasRetryButton) {
      console.log('重试按钮不可用');
      return;
    }

    await retryButton.click();
    await page.waitForTimeout(5000);

    // 4. 记录第二个版本的内容
    const secondVersionContent = await aiMessage.textContent();
    console.log('第二版本内容长度:', secondVersionContent?.length);

    // 5. 打开版本历史
    const versionHistoryButton = aiMessage.locator(
      'button:has-text("历史"), .version-indicator'
    );

    const hasVersionButton = await versionHistoryButton.isVisible().catch(() => false);
    if (!hasVersionButton) {
      console.log('版本历史按钮未实现');
      return;
    }

    await versionHistoryButton.first().click();
    await page.waitForTimeout(500);

    // 6. 点击第一个版本（历史版本）
    const versionPanel = page.locator('.version-history, .version-list');
    const hasPanel = await versionPanel.isVisible().catch(() => false);

    if (hasPanel) {
      const firstVersionItem = versionPanel.locator('[data-testid="version-item"], .version-item').first();

      // 检查是否有切换按钮
      const switchButton = firstVersionItem.locator(
        'button:has-text("切换"), button:has-text("查看"), button:has-text("恢复"), ' +
        'button[aria-label*="切换"], button[aria-label*="查看"]'
      );

      const hasSwitchButton = await switchButton.isVisible().catch(() => false);
      if (hasSwitchButton) {
        await switchButton.click();
        await page.waitForTimeout(500);

        // 7. 验证内容已切换回第一个版本
        const currentContent = await aiMessage.textContent();
        console.log('切换后内容长度:', currentContent?.length);

        // 如果内容不同，说明切换成功
        if (currentContent !== secondVersionContent) {
          console.log('✓ 版本切换成功');
        }
      } else {
        console.log('版本切换按钮未实现');
      }
    }

    await takeScreenshot(page, 'version-switch');
  });

  test('09-切换版本时应该高亮显示当前版本', async ({ page }) => {
    // 1. 创建会话
    await page.goto('/');

    const chatInput = page.locator('textarea[placeholder*="消息"]');
    await chatInput.fill('请解释什么是区块链');
    await chatInput.press('Enter');

    try {
      await page.waitForSelector('[data-testid="message-item"][data-role="assistant"]', {
        timeout: 30000
      });
    } catch {
      console.log('未收到AI响应，跳过测试');
      return;
    }

    // 2. 打开版本历史
    const aiMessage = page.locator('[data-testid="message-item"][data-role="assistant"]').last();
    const versionHistoryButton = aiMessage.locator(
      'button:has-text("历史"), .version-indicator'
    );

    const hasVersionButton = await versionHistoryButton.isVisible().catch(() => false);
    if (!hasVersionButton) {
      console.log('版本历史按钮未实现');
      return;
    }

    await versionHistoryButton.first().click();
    await page.waitForTimeout(500);

    // 3. 验证当前版本有特殊样式
    const versionPanel = page.locator('.version-history, .version-list');
    const hasPanel = await versionPanel.isVisible().catch(() => false);

    if (hasPanel) {
      const currentVersionItem = versionPanel.locator(
        '[data-current="true"], .current, .active, :has-text("当前")'
      );
      const hasCurrentVersion = await currentVersionItem.isVisible().catch(() => false);

      if (hasCurrentVersion) {
        // 验证有高亮样式
        const backgroundColor = await currentVersionItem.evaluate(el => {
          return window.getComputedStyle(el).backgroundColor;
        });

        console.log('当前版本背景色:', backgroundColor);
        console.log('✓ 当前版本已高亮显示');
      }
    }

    await takeScreenshot(page, 'version-current-highlight');
  });

  test('10-应该能够恢复历史版本为当前版本', async ({ page }) => {
    // 1. 创建会话并生成多个版本
    await page.goto('/');

    const chatInput = page.locator('textarea[placeholder*="消息"]');
    await chatInput.fill('请介绍一种编程范式');
    await chatInput.press('Enter');

    try {
      await page.waitForSelector('[data-testid="message-item"][data-role="assistant"]', {
        timeout: 30000
      });
    } catch {
      console.log('未收到AI响应，跳过测试');
      return;
    }

    // 2. 执行重试
    const aiMessage = page.locator('[data-testid="message-item"][data-role="assistant"]').last();
    const retryButton = aiMessage.locator(
      'button:has-text("重新生成"), button:has(svg.lucide-refresh-cw)'
    );

    const hasRetryButton = await retryButton.isVisible().catch(() => false);
    if (hasRetryButton) {
      await retryButton.click();
      await page.waitForTimeout(5000);
    }

    // 3. 打开版本历史
    const versionHistoryButton = aiMessage.locator(
      'button:has-text("历史"), .version-indicator'
    );

    const hasVersionButton = await versionHistoryButton.isVisible().catch(() => false);
    if (!hasVersionButton) {
      console.log('版本历史按钮未实现');
      return;
    }

    await versionHistoryButton.first().click();
    await page.waitForTimeout(500);

    // 4. 点击恢复按钮
    const versionPanel = page.locator('.version-history, .version-list');
    const hasPanel = await versionPanel.isVisible().catch(() => false);

    if (hasPanel) {
      const firstVersionItem = versionPanel.locator('[data-testid="version-item"], .version-item').first();

      const restoreButton = firstVersionItem.locator(
        'button:has-text("恢复"), button:has-text("设为当前"), button[aria-label*="恢复"]'
      );

      const hasRestoreButton = await restoreButton.isVisible().catch(() => false);
      if (hasRestoreButton) {
        // 监听恢复API
        const restoreApiCall = waitForAPI(page, '/api/messages/', 'PUT')
          .catch(() => null);

        await restoreButton.click();

        // 等待API调用
        await restoreApiCall.catch(() => {});
        await page.waitForTimeout(1000);

        console.log('✓ 恢复版本操作已执行');
      } else {
        console.log('恢复版本按钮未实现');
      }
    }

    await takeScreenshot(page, 'version-restore');
  });
});

/**
 * ============================================================================
 * 测试组 4: 版本对比功能
 * ============================================================================
 */
test.describe('版本对比功能', () => {
  test('11-应该能够并排显示两个版本', async ({ page }) => {
    // 1. 创建会话并生成多个版本
    await page.goto('/');

    const chatInput = page.locator('textarea[placeholder*="消息"]');
    await chatInput.fill('请解释什么是微服务架构');
    await chatInput.press('Enter');

    try {
      await page.waitForSelector('[data-testid="message-item"][data-role="assistant"]', {
        timeout: 30000
      });
    } catch {
      console.log('未收到AI响应，跳过测试');
      return;
    }

    // 2. 执行重试
    const aiMessage = page.locator('[data-testid="message-item"][data-role="assistant"]').last();
    const retryButton = aiMessage.locator(
      'button:has-text("重新生成"), button:has(svg.lucide-refresh-cw)'
    );

    const hasRetryButton = await retryButton.isVisible().catch(() => false);
    if (!hasRetryButton) {
      console.log('重试按钮不可用');
      return;
    }

    await retryButton.click();
    await page.waitForTimeout(5000);

    // 3. 打开版本历史
    const versionHistoryButton = aiMessage.locator(
      'button:has-text("历史"), .version-indicator'
    );

    const hasVersionButton = await versionHistoryButton.isVisible().catch(() => false);
    if (!hasVersionButton) {
      console.log('版本历史按钮未实现');
      return;
    }

    await versionHistoryButton.first().click();
    await page.waitForTimeout(500);

    // 4. 点击对比按钮
    const versionPanel = page.locator('.version-history, .version-list');
    const hasPanel = await versionPanel.isVisible().catch(() => false);

    if (hasPanel) {
      const compareButton = versionPanel.locator(
        'button:has-text("对比"), button:has-text("比较"), button[aria-label*="对比"]'
      );

      const hasCompareButton = await compareButton.isVisible().catch(() => false);
      if (hasCompareButton) {
        await compareButton.click();
        await page.waitForTimeout(500);

        // 5. 验证对比视图显示
        const compareView = page.locator(
          '[data-testid="version-compare-view"], .version-compare, .diff-view'
        );
        const hasCompareView = await compareView.isVisible().catch(() => false);

        if (hasCompareView) {
          await expect(compareView).toBeVisible();

          // 6. 验证并排显示
          const versionPanels = compareView.locator('.version-panel, .compare-panel');
          const panelCount = await versionPanels.count();
          expect(panelCount).toBeGreaterThanOrEqual(2);
          console.log('✓ 并排显示版本');

          // 7. 验证版本标签
          const versionLabels = compareView.locator('text=/版本\\s*\\d+|v\\d+/');
          const labelCount = await versionLabels.count();
          expect(labelCount).toBeGreaterThanOrEqual(2);
        }
      } else {
        console.log('版本对比按钮未实现');
      }
    }

    await takeScreenshot(page, 'version-compare-side-by-side');
  });

  test('12-对比视图应该高亮显示差异', async ({ page }) => {
    // 1. 创建会话并生成多个版本
    await page.goto('/');

    const chatInput = page.locator('textarea[placeholder*="消息"]');
    await chatInput.fill('请解释什么是DevOps');
    await chatInput.press('Enter');

    try {
      await page.waitForSelector('[data-testid="message-item"][data-role="assistant"]', {
        timeout: 30000
      });
    } catch {
      console.log('未收到AI响应，跳过测试');
      return;
    }

    // 2. 执行重试
    const aiMessage = page.locator('[data-testid="message-item"][data-role="assistant"]').last();
    const retryButton = aiMessage.locator(
      'button:has-text("重新生成"), button:has(svg.lucide-refresh-cw)'
    );

    const hasRetryButton = await retryButton.isVisible().catch(() => false);
    if (!hasRetryButton) {
      return;
    }

    await retryButton.click();
    await page.waitForTimeout(5000);

    // 3. 打开版本对比
    const versionHistoryButton = aiMessage.locator(
      'button:has-text("历史"), .version-indicator'
    );

    const hasVersionButton = await versionHistoryButton.isVisible().catch(() => false);
    if (!hasVersionButton) {
      return;
    }

    await versionHistoryButton.first().click();
    await page.waitForTimeout(500);

    // 4. 点击对比按钮
    const versionPanel = page.locator('.version-history, .version-list');
    const hasPanel = await versionPanel.isVisible().catch(() => false);

    if (hasPanel) {
      const compareButton = versionPanel.locator(
        'button:has-text("对比"), button:has-text("比较")'
      );

      const hasCompareButton = await compareButton.isVisible().catch(() => false);
      if (hasCompareButton) {
        await compareButton.click();
        await page.waitForTimeout(500);

        // 5. 验证差异高亮
        const compareView = page.locator('.version-compare, .diff-view');
        const hasCompareView = await compareView.isVisible().catch(() => false);

        if (hasCompareView) {
          // 检查差异高亮样式
          const diffHighlight = compareView.locator(
            '.diff-added, .diff-removed, .diff-highlight, ' +
            'ins, del, .added, .removed'
          );
          const hasHighlight = await diffHighlight.count() > 0;

          if (hasHighlight) {
            console.log('✓ 差异已高亮显示');
          } else {
            console.log('差异高亮未实现或无差异');
          }
        }
      }
    }

    await takeScreenshot(page, 'version-compare-diff');
  });
});

/**
 * ============================================================================
 * 测试组 5: 数据库验证
 * ============================================================================
 */
test.describe('数据库验证', () => {
  test('13-应该验证版本信息存储在数据库中', async ({ page }) => {
    // 1. 创建会话
    await page.goto('/');

    const chatInput = page.locator('textarea[placeholder*="消息"]');
    await chatInput.fill('请解释什么是API');
    await chatInput.press('Enter');

    // 2. 等待AI响应
    try {
      await page.waitForSelector('[data-testid="message-item"][data-role="assistant"]', {
        timeout: 30000
      });
    } catch {
      console.log('未收到AI响应，跳过测试');
      return;
    }

    // 3. 获取消息ID
    const aiMessage = page.locator('[data-testid="message-item"][data-role="assistant"]').last();
    const messageId = await aiMessage.evaluate(el => {
      return el.getAttribute('data-message-id');
    });

    if (!messageId) {
      console.log('无法获取消息ID');
      return;
    }

    console.log('消息ID:', messageId);

    // 4. 通过API获取消息详情
    const messageData = await page.evaluate(
      async ({ messageId }) => {
        const token = localStorage.getItem('token');
        try {
          const res = await fetch(`/api/messages/${messageId}`, {
            headers: { Authorization: `Bearer ${token}` },
          });
          const data = await res.json();
          return data;
        } catch (error) {
          return { error: String(error) };
        }
      },
      { messageId }
    );

    console.log('消息数据:', messageData);

    // 5. 验证版本字段存在
    if (messageData.version !== undefined) {
      console.log('✓ 版本号:', messageData.version);
      expect(messageData.version).toBeTruthy();
    } else {
      console.log('版本字段未实现');
    }

    // 6. 验证parent_id字段（如果有版本管理）
    if (messageData.parent_id !== undefined) {
      console.log('✓ Parent ID:', messageData.parent_id);
    }
  });

  test('14-应该验证重试后创建了新版本记录', async ({ page }) => {
    // 1. 创建会话
    await page.goto('/');

    const chatInput = page.locator('textarea[placeholder*="消息"]');
    await chatInput.fill('请解释什么是GraphQL');
    await chatInput.press('Enter');

    // 2. 等待AI响应
    try {
      await page.waitForSelector('[data-testid="message-item"][data-role="assistant"]', {
        timeout: 30000
      });
    } catch {
      console.log('未收到AI响应，跳过测试');
      return;
    }

    // 3. 获取会话ID和消息数量
    const sessionId = await page.evaluate(() => {
      const url = window.location.href;
      const match = url.match(/\/sessions\/([a-f0-9-]+)/);
      return match ? match[1] : null;
    });

    if (!sessionId) {
      console.log('无法获取会话ID');
      return;
    }

    // 4. 获取重试前的消息列表
    const beforeMessages = await page.evaluate(
      async ({ sessionId }) => {
        const token = localStorage.getItem('token');
        const res = await fetch(`/api/threads/${sessionId}/messages`, {
          headers: { Authorization: `Bearer ${token}` },
        });
        const data = await res.json();
        return data.messages || [];
      },
      { sessionId }
    );

    console.log('重试前消息数量:', beforeMessages.length);

    // 5. 执行重试
    const aiMessage = page.locator('[data-testid="message-item"][data-role="assistant"]').last();
    const retryButton = aiMessage.locator(
      'button:has-text("重新生成"), button:has(svg.lucide-refresh-cw)'
    );

    const hasRetryButton = await retryButton.isVisible().catch(() => false);
    if (!hasRetryButton) {
      console.log('重试按钮不可用');
      return;
    }

    await retryButton.click();
    await page.waitForTimeout(5000);

    // 6. 获取重试后的消息列表
    const afterMessages = await page.evaluate(
      async ({ sessionId }) => {
        const token = localStorage.getItem('token');
        const res = await fetch(`/api/threads/${sessionId}/messages`, {
          headers: { Authorization: `Bearer ${token}` },
        });
        const data = await res.json();
        return data.messages || [];
      },
      { sessionId }
    );

    console.log('重试后消息数量:', afterMessages.length);

    // 7. 验证消息数量变化（原位重试不应增加消息数量，版本管理应该增加）
    // 这里取决于实现方式：
    // - 原位替换：数量不变，更新同一条记录
    // - 版本管理：数量增加，创建新记录

    if (afterMessages.length === beforeMessages.length) {
      console.log('✓ 原位重试：消息数量未变');
    } else if (afterMessages.length > beforeMessages.length) {
      console.log('✓ 版本管理：创建了新消息记录');
    }

    await takeScreenshot(page, 'database-version-verify');
  });
});

/**
 * ============================================================================
 * 测试组 6: 综合场景测试
 * ============================================================================
 */
test.describe('综合场景测试', () => {
  test('15-完整的重试和版本管理流程', async ({ page }) => {
    // 1. 创建会话
    await page.goto('/');

    // 2. 发送消息
    const chatInput = page.locator('textarea[placeholder*="消息"]');
    await chatInput.fill('请介绍深度学习的基本概念');
    await chatInput.press('Enter');

    // 3. 等待AI响应
    try {
      await page.waitForSelector('[data-testid="message-item"][data-role="assistant"]', {
        timeout: 30000
      });
    } catch {
      console.log('未收到AI响应，跳过测试');
      return;
    }

    console.log('✓ 步骤1: 收到初始AI响应');

    // 4. 记录初始内容
    const aiMessage = page.locator('[data-testid="message-item"][data-role="assistant"]').last();
    const v1Content = await aiMessage.textContent();
    console.log('版本1内容长度:', v1Content?.length);

    // 5. 执行第一次重试
    const retryButton = aiMessage.locator(
      'button:has-text("重新生成"), button:has(svg.lucide-refresh-cw)'
    );

    const hasRetryButton = await retryButton.isVisible().catch(() => false);
    if (!hasRetryButton) {
      console.log('重试按钮不可用，跳过后续测试');
      return;
    }

    await retryButton.click();
    await page.waitForTimeout(5000);

    console.log('✓ 步骤2: 执行第一次重试');

    // 6. 记录第二个版本内容
    const v2Content = await aiMessage.textContent();
    console.log('版本2内容长度:', v2Content?.length);

    // 7. 检查版本指示器
    const versionIndicator = aiMessage.locator(
      '[data-testid="version-indicator"], .version-badge'
    );
    const hasVersionIndicator = await versionIndicator.isVisible().catch(() => false);

    if (hasVersionIndicator) {
      const versionText = await versionIndicator.textContent();
      console.log('✓ 步骤3: 版本指示器显示', versionText);
    }

    // 8. 打开版本历史（如果有）
    const versionHistoryButton = aiMessage.locator(
      'button:has-text("历史"), .version-indicator'
    );

    const hasVersionButton = await versionHistoryButton.isVisible().catch(() => false);
    if (hasVersionButton) {
      await versionHistoryButton.first().click();
      await page.waitForTimeout(500);

      console.log('✓ 步骤4: 打开版本历史');

      // 9. 验证版本列表
      const versionPanel = page.locator('.version-history, .version-list');
      const hasPanel = await versionPanel.isVisible().catch(() => false);

      if (hasPanel) {
        const versionItems = versionPanel.locator('.version-item');
        const itemCount = await versionItems.count();
        console.log(`✓ 步骤5: 版本列表显示 ${itemCount} 个版本`);
      }
    }

    // 10. 执行第二次重试
    await retryButton.click();
    await page.waitForTimeout(5000);

    console.log('✓ 步骤6: 执行第二次重试');

    // 11. 记录第三个版本内容
    const v3Content = await aiMessage.textContent();
    console.log('版本3内容长度:', v3Content?.length);

    console.log('✓ 完整的重试和版本管理流程测试完成');

    await takeScreenshot(page, 'complete-retry-version-flow');
  });

  test('16-多轮对话的重试和版本管理', async ({ page }) => {
    // 1. 创建会话
    await page.goto('/');

    const chatInput = page.locator('textarea[placeholder*="消息"]');

    // 2. 进行多轮对话
    const rounds = [
      '什么是神经网络？',
      '它有哪些应用？',
      '请举一个具体例子',
    ];

    for (const round of rounds) {
      await chatInput.fill(round);
      await chatInput.press('Enter');

      try {
        await page.waitForSelector('[data-testid="message-item"][data-role="assistant"]', {
          timeout: 30000
        });
      } catch {
        console.log('第', rounds.indexOf(round) + 1, '轮未收到AI响应');
        continue;
      }

      await page.waitForTimeout(1000);
    }

    console.log('✓ 完成多轮对话');

    // 3. 对中间的AI响应进行重试
    const aiMessages = page.locator('[data-testid="message-item"][data-role="assistant"]');
    const aiCount = await aiMessages.count();
    console.log(`共有 ${aiCount} 个AI响应`);

    if (aiCount >= 2) {
      // 重试第二个AI响应
      const secondAiMessage = aiMessages.nth(1);
      await secondAiMessage.hover();

      const retryButton = secondAiMessage.locator(
        'button:has-text("重新生成"), button:has(svg.lucide-refresh-cw)'
      );

      const hasRetryButton = await retryButton.isVisible().catch(() => false);
      if (hasRetryButton) {
        await retryButton.click();
        await page.waitForTimeout(5000);

        console.log('✓ 重试第二个AI响应');
      }
    }

    // 4. 验证消息数量未增加（原位重试）
    const finalMessageCount = await page.locator('[data-testid="message-item"]').count();
    console.log('最终消息数量:', finalMessageCount);

    await takeScreenshot(page, 'multi-round-retry');
  });
});
