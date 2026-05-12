/**
 * 工具卡片渲染测试 (tool-card-rendering)
 *
 * 验证 ActivityCard 组件在实际浏览器中的渲染效果
 * 测试覆盖：
 * - 页面加载和基本渲染
 * - 不同状态卡片的样式
 * - 卡片展开/折叠功能
 * - 卡片内容正确显示
 * - 响应式布局
 */

import { test, expect } from '@playwright/test';
import { login, waitForPageLoad, takeScreenshot } from './helpers';

test.describe('工具卡片渲染测试套件', () => {
  // 每个测试前登录
  test.beforeEach(async ({ page }) => {
    await login(page);
  });

  test.describe('页面加载', () => {
    test('01-应该正确加载工具卡片测试页面', async ({ page }) => {
      await page.goto('/test/tool-cards');
      await waitForPageLoad(page);

      // 验证页面标题
      const pageTitle = page.locator('h1').filter({ hasText: /工具调用卡片测试/ });
      await expect(pageTitle, '页面标题应该可见').toBeVisible();

      // 验证测试说明存在
      const instructions = page.locator('text=测试说明');
      await expect(instructions, '测试说明应该可见').toBeVisible();

      await takeScreenshot(page, '01-tool-card-test-page-loaded');
    });

    test('02-应该显示三个测试用例', async ({ page }) => {
      await page.goto('/test/tool-cards');
      await waitForPageLoad(page);

      // 查找所有测试用例标题
      const testCases = page.locator('h2').filter({ hasText: /测试用例 \d:/ });
      const count = await testCases.count();

      expect(count, '应该有3个测试用例').toBe(3);

      // 验证每个测试用例的标题
      await expect(testCases.nth(0)).toContainText('task_submit');
      await expect(testCases.nth(1)).toContainText('file_read');
      await expect(testCases.nth(2)).toContainText('web_search');

      await takeScreenshot(page, '02-three-test-cases-visible');
    });
  });

  test.describe('已完成状态卡片 (test-1: task_submit)', () => {
    test('03-已完成卡片应该显示正确', async ({ page }) => {
      await page.goto('/test/tool-cards');
      await waitForPageLoad(page);

      // 查找第一个卡片（task_submit）
      const card = page.locator('[data-activity-id="test-1"]');
      await expect(card, '卡片应该存在').toBeVisible();

      // 验证标题
      await expect(card.locator('.font-medium.truncate')).toContainText('task_submit');

      // 验证状态文本
      await expect(card.locator('text=已完成')).toBeVisible();

      // 验证状态图标（CheckCircle2）
      const statusIcon = card.locator('svg').filter({ hasText: '' }).first();
      await expect(statusIcon).toBeVisible();

      // 验证持续时间显示
      await expect(card.locator(/1s/)).toBeVisible();

      // 验证绿色背景（completed 状态）
      await expect(card).toHaveAttribute('data-activity-status', 'completed');

      await takeScreenshot(page, '03-completed-card-display');
    });

    test('04-已完成卡片展开后应显示详情', async ({ page }) => {
      await page.goto('/test/tool-cards');
      await waitForPageLoad(page);

      const card = page.locator('[data-activity-id="test-1"]');

      // 点击展开卡片（如果未展开）
      const header = card.locator('.cursor-pointer');
      await header.click();
      await page.waitForTimeout(300);

      // 验证详情区域可见
      await expect(card.locator('text=参数')).toBeVisible();

      // 验证结果详情
      await expect(card.locator('text=结果')).toBeVisible();

      // 验证 JSON 内容显示
      await expect(card.locator('pre')).toBeVisible();

      await takeScreenshot(page, '04-completed-card-details');
    });
  });

  test.describe('运行中状态卡片 (test-2: file_read)', () => {
    test('05-运行中卡片应该显示正确', async ({ page }) => {
      await page.goto('/test/tool-cards');
      await waitForPageLoad(page);

      const card = page.locator('[data-activity-id="test-2"]');
      await expect(card, '运行中卡片应该存在').toBeVisible();

      // 验证标题
      await expect(card.locator('.font-medium.truncate')).toContainText('file_read');

      // 验证状态文本（运行中）
      const statusText = card.locator('text=运行中');
      await expect(statusText, '运行中状态应该可见').toBeVisible();

      // 验证旋转动画图标
      const spinner = card.locator('.animate-spin');
      await expect(spinner, '旋转图标应该存在').toBeVisible();

      // 验证蓝色背景
      await expect(card).toHaveAttribute('data-activity-status', 'running');

      await takeScreenshot(page, '05-running-card-display');
    });

    test('06-运行中卡片展开后应显示参数', async ({ page }) => {
      await page.goto('/test/tool-cards');
      await waitForPageLoad(page);

      const card = page.locator('[data-activity-id="test-2"]');

      // 点击展开
      const header = card.locator('.cursor-pointer');
      await header.click();
      await page.waitForTimeout(300);

      // 验证参数区域
      await expect(card.locator('text=参数')).toBeVisible();

      // 验证路径参数显示
      await expect(card.locator('text=/path/to/file.txt')).toBeVisible();

      await takeScreenshot(page, '06-running-card-details');
    });
  });

  test.describe('失败状态卡片 (test-3: web_search)', () => {
    test('07-失败卡片应该显示正确', async ({ page }) => {
      await page.goto('/test/tool-cards');
      await waitForPageLoad(page);

      const card = page.locator('[data-activity-id="test-3"]');
      await expect(card, '失败卡片应该存在').toBeVisible();

      // 验证标题
      await expect(card.locator('.font-medium.truncate')).toContainText('web_search');

      // 验证失败状态
      await expect(card.locator('text=失败')).toBeVisible();

      // 验证错误图标
      await expect(card).toHaveAttribute('data-activity-status', 'failed');

      await takeScreenshot(page, '07-failed-card-display');
    });

    test('08-失败卡片应显示错误信息', async ({ page }) => {
      await page.goto('/test/tool-cards');
      await waitForPageLoad(page);

      const card = page.locator('[data-activity-id="test-3"]');

      // 点击展开
      const header = card.locator('.cursor-pointer');
      await header.click();
      await page.waitForTimeout(300);

      // 验证错误区域标题
      await expect(card.locator('text=错误')).toBeVisible();

      // 验证错误消息
      await expect(card.locator('text=网络连接超时')).toBeVisible();

      // 验证错误样式（红色）
      const errorBlock = card.locator('.text-red-500, .text-red-400');
      await expect(errorBlock, '错误文本应该是红色').toBeVisible();

      await takeScreenshot(page, '08-failed-card-error');
    });
  });

  test.describe('卡片交互', () => {
    test('09-卡片可以展开和折叠', async ({ page }) => {
      await page.goto('/test/tool-cards');
      await waitForPageLoad(page);

      const card = page.locator('[data-activity-id="test-1"]');

      // 默认应该是展开的（defaultExpanded=true）
      await expect(card.locator('text=参数')).toBeVisible();

      // 点击折叠
      const header = card.locator('.cursor-pointer');
      await header.click();
      await page.waitForTimeout(300);

      // 详情应该隐藏
      await expect(card.locator('text=参数')).not.toBeVisible();

      // 再次点击展开
      await header.click();
      await page.waitForTimeout(300);

      // 详情应该重新显示
      await expect(card.locator('text=参数')).toBeVisible();

      await takeScreenshot(page, '09-card-expand-collapse');
    });

    test('10-展开箭头应该旋转', async ({ page }) => {
      await page.goto('/test/tool-cards');
      await waitForPageLoad(page);

      const card = page.locator('[data-activity-id="test-1"]');
      const chevron = card.locator('.transition-transform'); // ChevronDown 图标

      // 展开状态时应该旋转
      const initialTransform = await chevron.getAttribute('class');
      expect(initialTransform).toContain('rotate-180');

      // 点击折叠
      const header = card.locator('.cursor-pointer');
      await header.click();
      await page.waitForTimeout(300);

      // 折叠后旋转应该移除
      const collapsedTransform = await chevron.getAttribute('class');
      expect(collapsedTransform).not.toContain('rotate-180');

      await takeScreenshot(page, '10-chevron-rotation');
    });

    test('11-详情区块可以折叠', async ({ page }) => {
      await page.goto('/test/tool-cards');
      await waitForPageLoad(page);

      const card = page.locator('[data-activity-id="test-1"]');

      // 确保卡片展开
      const header = card.locator('.cursor-pointer');
      await header.click();
      await page.waitForTimeout(300);

      // 参数区块应该可见
      await expect(card.locator('text=参数')).toBeVisible();

      // 点击参数折叠按钮
      const paramToggle = card.locator('button').filter({ hasText: '参数' });
      await paramToggle.click();
      await page.waitForTimeout(300);

      // 参数内容应该隐藏
      await expect(card.locator('pre').filter({ hasText: 'title' })).not.toBeVisible();

      // 再次点击展开
      await paramToggle.click();
      await page.waitForTimeout(300);

      // 参数内容应该重新显示
      await expect(card.locator('pre').filter({ hasText: 'title' })).toBeVisible();

      await takeScreenshot(page, '11-detail-block-collapse');
    });
  });

  test.describe('卡片样式验证', () => {
    test('12-卡片应该有正确的边框', async ({ page }) => {
      await page.goto('/test/tool-cards');
      await waitForPageLoad(page);

      const cards = page.locator('[data-activity-id]');

      const count = await cards.count();
      for (let i = 0; i < count; i++) {
        const card = cards.nth(i);
        const classes = await card.getAttribute('class');

        // 验证有边框
        expect(classes, '卡片应该有边框类').toContain('border');
        expect(classes, '卡片应该是圆角').toContain('rounded-xl');
      }

      await takeScreenshot(page, '12-card-border-style');
    });

    test('13-卡片图标应该有正确的背景色', async ({ page }) => {
      await page.goto('/test/tool-cards');
      await waitForPageLoad(page);

      // 已完成 - 绿色背景
      const completedCard = page.locator('[data-activity-id="test-1"]');
      const iconBg = completedCard.locator('.w-7.h-7');
      const completedClasses = await iconBg.getAttribute('class');
      expect(completedClasses).toMatch(/green-100|green-900/);

      // 运行中 - 蓝色背景
      const runningCard = page.locator('[data-activity-id="test-2"]');
      const runningIconBg = runningCard.locator('.w-7.h-7');
      const runningClasses = await runningIconBg.getAttribute('class');
      expect(runningClasses).toMatch(/blue-100|blue-900/);

      // 失败 - 红色背景
      const failedCard = page.locator('[data-activity-id="test-3"]');
      const failedIconBg = failedCard.locator('.w-7.h-7');
      const failedClasses = await failedIconBg.getAttribute('class');
      expect(failedClasses).toMatch(/red-100|red-900/);

      await takeScreenshot(page, '13-icon-background-colors');
    });

    test('14-卡片应该有悬停效果', async ({ page }) => {
      await page.goto('/test/tool-cards');
      await waitForPageLoad(page);

      const card = page.locator('[data-activity-id="test-1"]');
      const header = card.locator('.cursor-pointer');

      // 悬停前
      const beforeHover = await header.getAttribute('class');

      // 悬停
      await header.hover();
      await page.waitForTimeout(200);

      // 悬停后应该有 hover:bg-muted/40 类
      const afterHover = await header.getAttribute('class');
      expect(afterHover).toContain('hover:bg-muted');

      await takeScreenshot(page, '14-card-hover-effect');
    });
  });

  test.describe('原始数据显示', () => {
    test('15-应该可以查看原始数据', async ({ page }) => {
      await page.goto('/test/tool-cards');
      await waitForPageLoad(page);

      // 查找"查看原始数据"details元素
      const rawJsonDetails = page.locator('summary').filter({ hasText: '查看原始数据' });

      // 默认应该是折叠的
      const count = await rawJsonDetails.count();
      expect(count, '每个测试用例都应该有原始数据').toBe(3);

      // 点击第一个展开
      await rawJsonDetails.first().click();
      await page.waitForTimeout(300);

      // 验证 JSON 显示
      const jsonDisplay = page.locator('pre').filter({ hasText: /call_id/ });
      await expect(jsonDisplay, '原始 JSON 应该显示').toBeVisible();

      // 验证包含关键字段
      await expect(jsonDisplay.locator('text=tool_name')).toBeVisible();
      await expect(jsonDisplay.locator('text=status')).toBeVisible();

      await takeScreenshot(page, '15-raw-json-display');
    });
  });

  test.describe('响应式布局', () => {
    test('16-在移动端应该正确显示', async ({ page }) => {
      await page.setViewportSize({ width: 375, height: 667 });
      await page.goto('/test/tool-cards');
      await waitForPageLoad(page);

      // 验证页面标题可见
      await expect(page.locator('h1').filter({ hasText: /工具调用卡片测试/ }))
        .toBeVisible();

      // 验证卡片可见
      await expect(page.locator('[data-activity-id="test-1"]')).toBeVisible();

      await takeScreenshot(page, '16-mobile-layout');
    });

    test('17-在平板端应该正确显示', async ({ page }) => {
      await page.setViewportSize({ width: 768, height: 1024 });
      await page.goto('/test/tool-cards');
      await waitForPageLoad(page);

      await expect(page.locator('[data-activity-id]')).toHaveCount(3);

      await takeScreenshot(page, '17-tablet-layout');
    });

    test('18-在桌面端应该正确显示', async ({ page }) => {
      await page.setViewportSize({ width: 1920, height: 1080 });
      await page.goto('/test/tool-cards');
      await waitForPageLoad(page);

      await expect(page.locator('[data-activity-id]')).toHaveCount(3);

      await takeScreenshot(page, '18-desktop-layout');
    });
  });

  test.describe('控制台验证', () => {
    test('19-不应该有控制台错误', async ({ page }) => {
      const errors: string[] = [];

      page.on('console', (msg) => {
        if (msg.type() === 'error') {
          errors.push(msg.text());
        }
      });

      await page.goto('/test/tool-cards');
      await waitForPageLoad(page);
      await page.waitForTimeout(2000);

      if (errors.length > 0) {
        console.log('发现控制台错误:', errors);
      }

      expect(errors.length, '不应该有控制台错误').toBe(0);

      await takeScreenshot(page, '19-no-console-errors');
    });

    test('20-应该有组件日志输出', async ({ page }) => {
      const logs: string[] = [];

      page.on('console', (msg) => {
        if (msg.type() === 'log') {
          logs.push(msg.text());
        }
      });

      await page.goto('/test/tool-cards');
      await waitForPageLoad(page);
      await page.waitForTimeout(1000);

      // 验证有 ActivityCard 相关的日志
      const activityLogs = logs.filter(log =>
        log.includes('[ActivityCard]') || log.includes('[ToolCardTest]')
      );

      console.log('组件日志:', activityLogs);

      expect(activityLogs.length, '应该有组件渲染日志').toBeGreaterThan(0);

      await takeScreenshot(page, '20-component-logs');
    });
  });

  test.describe('可访问性', () => {
    test('21-卡片应该有正确的语义标签', async ({ page }) => {
      await page.goto('/test/tool-cards');
      await waitForPageLoad(page);

      // 验证 data 属性
      await expect(page.locator('[data-activity-type="tool_call"]')).toHaveCount(3);
      await expect(page.locator('[data-activity-status="completed"]')).toHaveCount(1);
      await expect(page.locator('[data-activity-status="running"]')).toHaveCount(1);
      await expect(page.locator('[data-activity-status="failed"]')).toHaveCount(1);

      await takeScreenshot(page, '21-semantic-attributes');
    });

    test('22-文本应该清晰可读', async ({ page }) => {
      await page.goto('/test/tool-cards');
      await waitForPageLoad(page);

      // 检查标题文本
      const titles = page.locator('.font-medium.truncate');
      const count = await titles.count();
      expect(count).toBe(3);

      // 验证每个标题都有文本
      for (let i = 0; i < count; i++) {
        const text = await titles.nth(i).textContent();
        expect(text?.trim().length).toBeGreaterThan(0);
      }

      await takeScreenshot(page, '22-readable-text');
    });
  });

  test.describe('性能测试', () => {
    test('23-页面应该快速加载', async ({ page }) => {
      const startTime = Date.now();

      await page.goto('/test/tool-cards');
      await waitForPageLoad(page);

      const loadTime = Date.now() - startTime;

      console.log(`工具卡片测试页面加载时间: ${loadTime}ms`);

      expect(loadTime, '页面应该在 3 秒内加载').toBeLessThan(3000);

      await takeScreenshot(page, '23-page-load-time');
    });
  });
});
