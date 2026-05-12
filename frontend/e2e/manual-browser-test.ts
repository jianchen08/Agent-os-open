/**
 * 手动浏览器测试工具
 *
 * 使用 Playwright 进行真实的浏览器操作测试
 * 可以模拟用户点击、输入、导航等操作
 */

import { chromium, Browser, Page, BrowserContext } from 'playwright';
import * as fs from 'fs';
import * as path from 'path';

export class ManualBrowserTester {
  private browser: Browser | null = null;
  private context: BrowserContext | null = null;
  private page: Page | null = null;
  private screenshots: string[] = [];
  private logs: string[] = [];
  private httpErrors: string[] = [];  // 记录 HTTP 错误
  private consoleErrors: string[] = [];  // 记录控制台错误

  /**
   * 启动浏览器
   */
  async launch(headless: boolean = false) {
    this.log(`正在启动浏览器 (headless: ${headless})...`);

    this.browser = await chromium.launch({
      headless,
      args: ['--start-maximized'],
    });

    this.context = await this.browser.newContext({
      viewport: { width: 1280, height: 720 },
      recordVideo: {
        dir: 'test-results/videos/',
        size: { width: 1280, height: 720 },
      },
    });

    this.page = await this.context.newPage();

    // 清空错误记录
    this.httpErrors = [];
    this.consoleErrors = [];

    // 监听控制台消息
    this.page.on('console', (msg) => {
      if (msg.type() === 'error') {
        const errorText = msg.text();
        this.log(`[浏览器错误] ${errorText}`);
        this.consoleErrors.push(errorText);
      }
    });

    // 监听页面错误
    this.page.on('pageerror', (error) => {
      this.log(`[页面错误] ${error.message}`);
    });

    // 监听响应,记录 HTTP 错误
    this.page.on('response', (response) => {
      const status = response.status();
      if (status >= 400) {
        const error = `HTTP ${status} - ${response.url()}`;
        this.log(`[HTTP错误] ${error}`);
        this.httpErrors.push(error);
      }
    });

    this.log('浏览器启动成功');
  }

  /**
   * 导航到指定 URL
   */
  async goto(url: string) {
    if (!this.page) {
      throw new Error('浏览器未启动，请先调用 launch()');
    }

    this.log(`导航到: ${url}`);
    const startTime = Date.now();

    await this.page.goto(url, {
      waitUntil: 'networkidle',
      timeout: 30000,
    });

    const loadTime = Date.now() - startTime;
    this.log(`页面加载完成，耗时: ${loadTime}ms`);

    // 自动截图
    await this.screenshot(`navigate-${Date.now()}`);
  }

  /**
   * 点击元素
   */
  async click(selector: string, options?: { timeout?: number }) {
    if (!this.page) {
      throw new Error('浏览器未启动');
    }

    this.log(`点击元素: ${selector}`);

    try {
      await this.page.click(selector, {
        timeout: options?.timeout || 10000,
      });
      this.log(`✓ 点击成功`);

      // 等待页面响应
      await this.page.waitForTimeout(500);
      await this.screenshot(`click-${Date.now()}`);
    } catch (error) {
      this.log(`✗ 点击失败: ${error}`);
      await this.screenshot(`click-failed-${Date.now()}`);
      throw error;
    }
  }

  /**
   * 输入文本
   */
  async fill(selector: string, value: string) {
    if (!this.page) {
      throw new Error('浏览器未启动');
    }

    this.log(`输入文本到 ${selector}: "${value}"`);

    try {
      await this.page.fill(selector, value);
      this.log(`✓ 输入成功`);

      await this.page.waitForTimeout(300);
      await this.screenshot(`fill-${Date.now()}`);
    } catch (error) {
      this.log(`✗ 输入失败: ${error}`);
      throw error;
    }
  }

  /**
   * 获取元素文本
   */
  async getText(selector: string): Promise<string> {
    if (!this.page) {
      throw new Error('浏览器未启动');
    }

    try {
      const element = await this.page.locator(selector).first();
      const text = await element.textContent();
      this.log(`获取文本 ${selector}: "${text}"`);
      return text || '';
    } catch (error) {
      this.log(`✗ 获取文本失败: ${error}`);
      return '';
    }
  }

  /**
   * 等待元素出现
   */
  async waitForSelector(selector: string, timeout: number = 10000) {
    if (!this.page) {
      throw new Error('浏览器未启动');
    }

    this.log(`等待元素: ${selector}`);

    try {
      await this.page.waitForSelector(selector, { timeout });
      this.log(`✓ 元素已出现`);
    } catch (error) {
      this.log(`✗ 等待超时: ${selector}`);
      throw error;
    }
  }

  /**
   * 检查元素是否存在
   */
  async exists(selector: string): Promise<boolean> {
    if (!this.page) {
      throw new Error('浏览器未启动');
    }

    const count = await this.page.locator(selector).count();
    const exists = count > 0;
    this.log(`元素检查 ${selector}: ${exists ? '存在' : '不存在'}`);
    return exists;
  }

  /**
   * 检查元素是否可见
   */
  async isVisible(selector: string): Promise<boolean> {
    if (!this.page) {
      throw new Error('浏览器未启动');
    }

    try {
      const element = this.page.locator(selector).first();
      const visible = await element.isVisible();
      this.log(`元素可见性 ${selector}: ${visible ? '可见' : '不可见'}`);
      return visible;
    } catch (error) {
      this.log(`元素可见性 ${selector}: 不可见 (异常)`);
      return false;
    }
  }

  /**
   * 获取元素的位置和大小
   */
  async getBoundingBox(selector: string) {
    if (!this.page) {
      throw new Error('浏览器未启动');
    }

    try {
      const element = this.page.locator(selector).first();
      const box = await element.boundingBox();
      this.log(`元素位置 ${selector}: ${JSON.stringify(box)}`);
      return box;
    } catch (error) {
      this.log(`获取元素位置失败 ${selector}: ${error}`);
      return null;
    }
  }

  /**
   * 检查页面加载是否成功（无严重错误）
   */
  async checkPageLoadSuccess(): Promise<{
    success: boolean;
    hasErrors: boolean;
    bodyEmpty: boolean;
    httpErrorCount: number;
    consoleErrorCount: number;
    errors: string[];
  }> {
    if (!this.page) {
      throw new Error('浏览器未启动');
    }

    this.log('检查页面加载状态...');

    const errors: string[] = [];
    let hasErrors = false;
    let bodyEmpty = false;

    // 收集所有已记录的错误
    const allErrors = [
      ...this.httpErrors,
      ...this.consoleErrors
    ];

    // 检查是否有 HTTP 500 错误
    const hasHttp500 = this.httpErrors.some(err =>
      err.includes('HTTP 500') ||
      err.includes('Internal Server Error')
    );

    if (hasHttp500) {
      this.log('⚠ 检测到 HTTP 500 错误');
      hasErrors = true;
      errors.push(...this.httpErrors.filter(err => err.includes('HTTP 500')));
    }

    // 检查其他 HTTP 错误
    const otherHttpErrors = this.httpErrors.filter(err => !err.includes('HTTP 500'));
    if (otherHttpErrors.length > 0) {
      this.log(`⚠ 检测到 ${otherHttpErrors.length} 个其他 HTTP 错误`);
      errors.push(...otherHttpErrors);
    }

    // 检查控制台错误
    if (this.consoleErrors.length > 0) {
      this.log(`⚠ 检测到 ${this.consoleErrors.length} 个控制台错误`);
      hasErrors = true;
      errors.push(...this.consoleErrors);
    }

    // 检查 body 是否为空
    const bodyInfo = await this.page.evaluate(() => {
      const body = document.body;
      return {
        innerHTML: body?.innerHTML || '',
        textContent: body?.textContent || '',
        childrenCount: body?.children.length || 0,
        scrollHeight: document.documentElement.scrollHeight,
      };
    });

    bodyEmpty = bodyInfo.childrenCount === 0 && bodyInfo.textContent.trim().length < 50;

    if (bodyEmpty) {
      this.log('⚠ 页面 body 为空');
      errors.push('页面 body 为空');
      hasErrors = true;
    }

    const success = !hasErrors && !bodyEmpty;

    this.log(`页面加载检查结果: ${success ? '✓ 成功' : '✗ 失败'}`);
    this.log(`  HTTP 错误: ${this.httpErrors.length} 个`);
    this.log(`  控制台错误: ${this.consoleErrors.length} 个`);
    this.log(`  Body 为空: ${bodyEmpty ? '是' : '否'}`);

    return {
      success,
      hasErrors,
      bodyEmpty,
      httpErrorCount: this.httpErrors.length,
      consoleErrorCount: this.consoleErrors.length,
      errors,
    };
  }

  /**
   * 清空错误记录（用于新的测试阶段）
   */
  clearErrors() {
    this.httpErrors = [];
    this.consoleErrors = [];
    this.log('错误记录已清空');
  }

  /**
   * 截图
   */
  async screenshot(name: string) {
    if (!this.page) {
      throw new Error('浏览器未启动');
    }

    const screenshotPath = path.join('test-results', 'screenshots', `${name}.png`);
    await this.page.screenshot({
      path: screenshotPath,
      fullPage: true,
    });

    this.screenshots.push(screenshotPath);
    this.log(`截图保存: ${screenshotPath}`);
  }

  /**
   * 执行 JavaScript 代码
   */
  async evaluate(script: string) {
    if (!this.page) {
      throw new Error('浏览器未启动');
    }

    this.log(`执行 JavaScript: ${script.substring(0, 50)}...`);

    try {
      const result = await this.page.evaluate(script);
      this.log(`✓ 执行成功，结果: ${JSON.stringify(result)}`);
      return result;
    } catch (error) {
      this.log(`✗ 执行失败: ${error}`);
      throw error;
    }
  }

  /**
   * 获取页面信息
   */
  async getPageInfo() {
    if (!this.page) {
      throw new Error('浏览器未启动');
    }

    const info = await this.page.evaluate(() => {
      return {
        url: window.location.href,
        title: document.title,
        viewport: {
          width: window.innerWidth,
          height: window.innerHeight,
        },
        // 获取所有按钮
        buttons: Array.from(document.querySelectorAll('button')).map((btn) => ({
          text: btn.textContent?.trim(),
          disabled: btn.disabled,
          visible: btn.offsetParent !== null,
        })),
        // 获取所有输入框
        inputs: Array.from(document.querySelectorAll('input')).map((input) => ({
          type: input.type,
          placeholder: input.placeholder,
          value: input.value,
        })),
      };
    });

    this.log(`页面信息: ${JSON.stringify(info, null, 2)}`);
    return info;
  }

  /**
   * 滚动页面
   */
  async scroll(direction: 'up' | 'down' | 'top' | 'bottom', distance?: number) {
    if (!this.page) {
      throw new Error('浏览器未启动');
    }

    let scrollScript = '';

    switch (direction) {
      case 'up':
        scrollScript = `window.scrollBy(0, -${distance || 300})`;
        break;
      case 'down':
        scrollScript = `window.scrollBy(0, ${distance || 300})`;
        break;
      case 'top':
        scrollScript = 'window.scrollTo(0, 0)';
        break;
      case 'bottom':
        scrollScript = 'window.scrollTo(0, document.body.scrollHeight)';
        break;
    }

    await this.page.evaluate(scrollScript);
    this.log(`滚动页面: ${direction}`);
    await this.page.waitForTimeout(500);
  }

  /**
   * 悬停在元素上
   */
  async hover(selector: string) {
    if (!this.page) {
      throw new Error('浏览器未启动');
    }

    this.log(`悬停在元素: ${selector}`);

    try {
      await this.page.hover(selector);
      this.log(`✓ 悬停成功`);

      await this.page.waitForTimeout(500);
      await this.screenshot(`hover-${Date.now()}`);
    } catch (error) {
      this.log(`✗ 悬停失败: ${error}`);
      throw error;
    }
  }

  /**
   * 等待指定时间
   */
  async wait(ms: number) {
    this.log(`等待 ${ms}ms`);
    await this.page?.waitForTimeout(ms);
  }

  /**
   * 记录日志
   */
  private log(message: string) {
    const timestamp = new Date().toISOString();
    const logMessage = `[${timestamp}] ${message}`;
    console.log(logMessage);
    this.logs.push(logMessage);
  }

  /**
   * 保存日志到文件
   */
  saveLogs(filename?: string) {
    const logPath =
      filename || path.join('test-results', `logs-${Date.now()}.txt`);
    fs.writeFileSync(logPath, this.logs.join('\n'));
    this.log(`日志已保存到: ${logPath}`);
  }

  /**
   * 关闭浏览器
   */
  async close() {
    this.log('正在关闭浏览器...');

    if (this.page) {
      await this.page.close();
    }

    if (this.context) {
      await this.context.close();
    }

    if (this.browser) {
      await this.browser.close();
    }

    this.log('浏览器已关闭');
  }

  /**
   * 运行测试脚本
   */
  async runTestScript(script: (tester: ManualBrowserTester) => Promise<void>) {
    try {
      await script(this);
      this.log('✓ 测试脚本执行成功');
    } catch (error) {
      this.log(`✗ 测试脚本执行失败: ${error}`);
      throw error;
    }
  }
}

/**
 * 示例测试脚本
 */
export async function exampleTest() {
  const tester = new ManualBrowserTester();

  try {
    // 启动浏览器（非无头模式，可以看到操作过程）
    await tester.launch(false);

    // 导航到首页
    await tester.goto('http://localhost:25731');

    // 等待页面加载
    await tester.wait(2000);

    // 获取页面信息
    const pageInfo = await tester.getPageInfo();
    console.log('页面信息:', JSON.stringify(pageInfo, null, 2));

    // 检查是否有登录按钮
    const hasLoginButton = await tester.exists('button:has-text("登录")');
    if (hasLoginButton) {
      await tester.click('button:has-text("登录")');
      await tester.wait(1000);
    }

    // 检查是否有输入框
    const hasInput = await tester.exists('input[type="text"]');
    if (hasInput) {
      await tester.fill('input[type="text"]', '测试文本');
      await tester.wait(1000);
    }

    // 截图
    await tester.screenshot('final-state');

    // 保存日志
    tester.saveLogs();

  } finally {
    // 关闭浏览器
    await tester.close();
  }
}

// 如果直接运行此文件，执行示例测试
// 注意：在 ES 模块中，使用 import.meta.url 检查是否为主模块
if (import.meta.url === `file://${process.argv[1].replace(/\\/g, '/')}`) {
  exampleTest().catch(console.error);
}
