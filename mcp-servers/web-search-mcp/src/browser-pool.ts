import { chromium, firefox, webkit, Browser, BrowserContext } from 'playwright';
import * as path from 'path';
import * as fs from 'fs';

/**
 * 浏览器条目包装类型
 * 兼容普通浏览器模式和持久化上下文模式两种存储方式
 */
interface BrowserEntry {
  /** 浏览器实例（普通模式使用） */
  browser: Browser | null;
  /** 浏览器上下文（持久化模式使用，launchPersistentContext 返回） */
  context: BrowserContext | null;
  /** 是否为持久化上下文模式 */
  isPersistent: boolean;
}

/**
 * getBrowser() 返回的浏览器句柄
 * 调用方根据 isPersistent 判断使用 browser 还是 context
 */
export interface BrowserHandle {
  /** 浏览器实例（普通模式下可用，持久化模式下为 null） */
  browser: Browser | null;
  /** 浏览器上下文（持久化模式下可用，普通模式下为 null） */
  context: BrowserContext | null;
  /** 是否为持久化上下文模式 */
  isPersistent: boolean;
}

/**
 * 浏览器连接池
 * 支持 Chromium/Firefox/WebKit 三种引擎轮换，可选启用 Chromium 持久化上下文
 * 启用持久化后，Chromium 的 cookies、localStorage、sessionStorage、IndexedDB 等会自动保存到磁盘
 */
export class BrowserPool {
  /** 浏览器实例存储（按浏览器类型索引） */
  private browsers: Map<string, BrowserEntry> = new Map();
  /** 最大浏览器实例数 */
  private maxBrowsers: number;
  /** 可用浏览器类型列表 */
  private browserTypes: string[];
  /** 当前轮换索引 */
  private currentBrowserIndex = 0;
  /** 是否使用无头模式 */
  private headless: boolean;
  /** 上一次使用的浏览器类型 */
  private lastUsedBrowserType: string = '';
  /** 是否启用状态持久化（仅 Chromium 生效） */
  private persistState: boolean;
  /** 状态文件保存目录 */
  private stateDir: string;

  constructor() {
    // 从环境变量读取基础配置
    this.maxBrowsers = parseInt(process.env.MAX_BROWSERS || '3', 10);
    this.headless = process.env.BROWSER_HEADLESS !== 'false'; // 默认无头模式

    // 读取持久化配置
    this.persistState = process.env.BROWSER_PERSIST_STATE === 'true'; // 默认关闭
    this.stateDir = process.env.BROWSER_STATE_DIR || 'data/browser_state'; // 默认目录

    // 根据环境变量配置浏览器类型
    const browserTypesEnv = process.env.BROWSER_TYPES || 'chromium,firefox';
    this.browserTypes = browserTypesEnv.split(',').map(type => type.trim());

    console.error(`[BrowserPool] Configuration: maxBrowsers=${this.maxBrowsers}, headless=${this.headless}, types=${this.browserTypes.join(',')}, persistState=${this.persistState}, stateDir=${this.stateDir}`);
  }

  /**
   * 获取特定浏览器类型的状态目录路径
   * @param browserType 浏览器类型（如 chromium、firefox）
   * @returns 状态目录的绝对路径
   */
  getStateDir(browserType: string): string {
    return path.resolve(this.stateDir, browserType);
  }

  /**
   * 确保状态目录存在，不存在则递归创建
   * @param dir 目录路径
   */
  private ensureStateDir(dir: string): void {
    if (!fs.existsSync(dir)) {
      fs.mkdirSync(dir, { recursive: true });
    }
  }

  /**
   * 获取浏览器实例（轮换策略）
   * 当 persistState=true 且浏览器类型为 chromium 时，使用 launchPersistentContext 返回持久化上下文
   * 其他情况返回普通 Browser 实例
   * @returns 浏览器句柄，包含 browser/context 和模式标识
   */
  async getBrowser(): Promise<BrowserHandle> {
    // 轮换选择浏览器类型
    const browserType = this.browserTypes[this.currentBrowserIndex % this.browserTypes.length];
    this.currentBrowserIndex++;
    this.lastUsedBrowserType = browserType;

    // 检查是否已有该类型的实例
    if (this.browsers.has(browserType)) {
      const entry = this.browsers.get(browserType)!;

      try {
        if (entry.isPersistent && entry.context) {
          // 持久化上下文模式：通过创建并关闭测试页面来验证上下文有效性
          const testPage = await entry.context.newPage();
          await testPage.close();
          return { browser: null, context: entry.context, isPersistent: true };
        } else if (entry.browser) {
          // 普通模式：检查浏览器连接状态
          if (entry.browser.isConnected()) {
            // 快速健康检查：创建并关闭一个临时上下文
            const testContext = await entry.browser.newContext({
              userAgent: 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36'
            });
            await testContext.close();
            return { browser: entry.browser, context: null, isPersistent: false };
          }
        }
      } catch (error) {
        console.error(`[BrowserPool] Browser ${browserType} health check failed:`, error);
        // 实例不健康，移除并尝试关闭
        this.browsers.delete(browserType);
        try {
          if (entry.isPersistent && entry.context) {
            await entry.context.close();
          } else if (entry.browser) {
            await entry.browser.close();
          }
        } catch (closeError) {
          console.error(`[BrowserPool] Error closing unhealthy browser:`, closeError);
        }
      }
    }

    // 启动新浏览器实例
    console.error(`[BrowserPool] Launching new ${browserType} browser`);

    const launchOptions = {
      headless: this.headless,
      args: [
        '--no-sandbox',
        '--disable-blink-features=AutomationControlled',
        '--disable-dev-shm-usage',
        '--disable-gpu',
        '--no-first-run',
        '--no-default-browser-check',
        '--disable-default-apps',
        '--disable-extensions',
        '--disable-background-timer-throttling',
        '--disable-backgrounding-occluded-windows',
        '--disable-renderer-backgrounding',
        '--disable-features=TranslateUI',
        '--disable-ipc-flooding-protection',
      ],
    };

    // 判断是否使用持久化上下文（仅 Chromium 且开启了持久化配置）
    const usePersistent = this.persistState && browserType === 'chromium';

    let entry: BrowserEntry;

    try {
      if (usePersistent) {
        // 持久化上下文模式：使用 launchPersistentContext，状态自动保存到 userDataDir
        const userDataDir = this.getStateDir(browserType);
        this.ensureStateDir(userDataDir);

        const context = await chromium.launchPersistentContext(userDataDir, {
          ...launchOptions,
          // 持久化上下文可同时设置上下文级别的选项
          userAgent: 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36',
        });

        entry = { browser: null, context, isPersistent: true };
        console.error(`[BrowserPool] Launched persistent context for ${browserType} at ${userDataDir}`);
      } else {
        // 普通模式：使用标准 launch
        let browser: Browser;
        switch (browserType) {
          case 'chromium':
            browser = await chromium.launch(launchOptions);
            break;
          case 'firefox':
            browser = await firefox.launch(launchOptions);
            break;
          case 'webkit':
            browser = await webkit.launch(launchOptions);
            break;
          default:
            browser = await chromium.launch(launchOptions);
        }
        entry = { browser, context: null, isPersistent: false };
      }

      this.browsers.set(browserType, entry);

      // 超过最大实例数时，清理最旧的实例
      if (this.browsers.size > this.maxBrowsers) {
        const oldestEntry = this.browsers.entries().next().value;
        if (oldestEntry) {
          try {
            if (oldestEntry[1].isPersistent && oldestEntry[1].context) {
              await oldestEntry[1].context.close();
            } else if (oldestEntry[1].browser) {
              await oldestEntry[1].browser.close();
            }
          } catch (error) {
            console.error(`[BrowserPool] Error closing old browser:`, error);
          }
          this.browsers.delete(oldestEntry[0]);
        }
      }

      return { browser: entry.browser, context: entry.context, isPersistent: entry.isPersistent };
    } catch (error) {
      console.error(`[BrowserPool] Failed to launch ${browserType} browser:`, error);
      throw error;
    }
  }

  /**
   * 显式保存当前持久化上下文的 storageState 到文件
   * 仅对持久化上下文模式有效，普通模式下静默跳过
   * @param browserType 浏览器类型
   */
  async saveContextState(browserType: string): Promise<void> {
    const entry = this.browsers.get(browserType);
    if (!entry || !entry.isPersistent || !entry.context) {
      return;
    }

    try {
      const stateDir = this.getStateDir(browserType);
      this.ensureStateDir(stateDir);
      const statePath = path.join(stateDir, 'storage-state.json');
      const state = await entry.context.storageState();
      fs.writeFileSync(statePath, JSON.stringify(state, null, 2));
      console.error(`[BrowserPool] Saved context state for ${browserType} to ${statePath}`);
    } catch (error) {
      console.error(`[BrowserPool] Failed to save context state for ${browserType}:`, error);
    }
  }

  /**
   * 关闭所有浏览器实例
   * 持久化上下文在关闭前会自动保存 storageState
   */
  async closeAll(): Promise<void> {
    console.error(`[BrowserPool] Closing ${this.browsers.size} browsers`);

    const closePromises = Array.from(this.browsers.entries()).map(([type, entry]) => {
      if (entry.isPersistent && entry.context) {
        // 持久化上下文：先保存状态，再关闭上下文
        return entry.context.storageState()
          .then(state => {
            const stateDir = this.getStateDir(type);
            this.ensureStateDir(stateDir);
            fs.writeFileSync(path.join(stateDir, 'storage-state.json'), JSON.stringify(state, null, 2));
          })
          .catch(() => { /* 忽略保存失败 */ })
          .then(() => entry.context!.close())
          .catch(error => console.error('Error closing persistent context:', error));
      } else if (entry.browser) {
        // 普通模式：直接关闭浏览器
        return entry.browser.close().catch(error =>
          console.error('Error closing browser:', error)
        );
      }
      return Promise.resolve();
    });

    await Promise.all(closePromises);
    this.browsers.clear();
  }

  /**
   * 获取上一次使用的浏览器类型
   * @returns 浏览器类型名称
   */
  getLastUsedBrowserType(): string {
    return this.lastUsedBrowserType;
  }
}
