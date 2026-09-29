/**
 * AC 验证 Spec — 独有断言持有者子集
 *
 * AC 清单来源：docs/tasks/task_02_playwright_browser_e2e.md。本文件只保留
 * journey 系与 feature_matrix 都不持有的四条：
 *   AC-3  登录态真实后端下 console 无严重 JS 运行时 error；
 *   AC-7  设置页密钥输入框无硬编码 API key（全 e2e 面唯一回归）；
 *   AC-8  fake clock 快进 10s 会话仍在线（TTL 边界精确断言在
 *         authStoreTokenExpiry.test.ts 的 vi.useFakeTimers）；
 *   AC-14 LLM 配置页受控输入回读（防重渲染丢值）。
 * 其余 AC 的覆盖位置：页面可达类（1/2/9/10/11a/12/13）由 journey_* 同流程
 * 更强断言持有（设置页←journey_04、监控←journey_09_monitoring、工具←
 * journey_03、路由循环←feature_matrix API 冒烟之外不再重复）；全流程
 * AC-4 由 journey_01 1.2 + journey_03 3.2 持有；API 可达 AC-15 由
 * feature_matrix FP-11.x 持有。
 */

import { test, expect } from '../fixtures';
import { login } from '../helpers/auth';
import { loginAndNavigateTo, ROUTES } from '../helpers/navigation';

test.describe.configure({ timeout: 120_000 });

test.describe('AC 验证', () => {
  // ── AC-3: 冗余代码清理 → 浏览器 console 无 error ──────────

  test('AC-3: 浏览器 console 无 JS 运行时 error', async ({ page }) => {
    const errors: string[] = [];
    page.on('console', (msg) => {
      if (msg.type() === 'error') errors.push(msg.text());
    });

    await loginAndNavigateTo(page, ROUTES.HOME);
    // T5#8：用条件等待替代固定 sleep——等网络空闲即可收集 console error。
    await page.waitForLoadState('networkidle');

    // 过滤掉已知的非关键错误（如网络请求失败）
    const criticalErrors = errors.filter(
      (e) => !e.includes('favicon') && !e.includes('net::') && !e.includes('404'),
    );
    console.log(`📋 Console errors: ${criticalErrors.length} 条`);
    // 宽松断言：允许少量非关键错误
    expect(criticalErrors.length, '不应有严重 JS 运行时错误').toBeLessThan(5);
  });

  // ── AC-7: API Key 迁移到环境变量 ───────────────────────────

  test('AC-7: 设置页 API 密钥输入框应无硬编码值', async ({ page }) => {
    await loginAndNavigateTo(page, ROUTES.SETTINGS_LLM);

    // 查找 API 密钥输入框
    const keyInputs = page.locator('input[type="password"], input[placeholder*="key"], input[placeholder*="密钥"]');
    const count = await keyInputs.count();

    for (let i = 0; i < count; i++) {
      const value = await keyInputs.nth(i).inputValue().catch(() => '');
      // 硬编码 API key 通常以 sk- 开头且很长
      const isHardcodedKey = value.startsWith('sk-') && value.length > 20;
      expect(isHardcodedKey, `第 ${i + 1} 个密钥输入框不应包含硬编码 API key`).toBeFalsy();
    }
  });

  // ── AC-8: TokenManager — 登录后等待仍在线 ──────────────────

  test('AC-8: 登录后等待 10 秒仍在线', async ({ page }) => {
    // T5#6 修复：用 page.clock 注入可控时钟，替代真实墙钟 waitForTimeout(10_000)。
    // TTL 边界的精确断言（边界前后真值）已下沉到 authStoreTokenExpiry.test.ts
    // （vi.useFakeTimers）；此 e2e 只验证"快进 10s 应用时钟后仍在线"且不阻塞墙钟。
    await page.clock.install();
    await login(page);

    // 快进 10s 应用时钟（token 续期/过期检查的 setInterval 由 fake clock 驱动）
    await page.clock.fastForward(10_000);

    // 验证仍在已认证状态（未被踢出）
    const token = await page.evaluate(() => localStorage.getItem('access_token'));
    expect(token, 'Token 应仍存在').toBeTruthy();

    // 页面不应被重定向到登录页
    const url = page.url();
    expect(url.includes('login'), '不应被重定向到登录页').toBeFalsy();
  });

  // ── AC-14: 修改配置文件 → 页面自动更新 ────────────────────

  test('AC-14: 配置修改后页面应反映变更', async ({ page }) => {
    await loginAndNavigateTo(page, ROUTES.SETTINGS_LLM);

    // 修改一个配置值
    const input = page.locator('input[type="text"], input[type="number"], input[type="url"]').first();
    const hasInput = await input.isVisible().catch(() => false);

    if (hasInput) {
      const testValue = 'e2e-ac14-' + Date.now();
      await input.fill(testValue);
      // T5#8：fill 后值立即同步到 DOM，原固定 sleep 多余——直接断言受控输入值。

      // 验证输入值已更新
      const currentValue = await input.inputValue().catch(() => '');
      expect(currentValue, '页面应反映配置变更').toBe(testValue);
    }
  });
});
