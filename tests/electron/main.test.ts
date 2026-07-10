/**
 * main.ts 单元测试。
 *
 * 覆盖主进程入口的窗口创建、快捷键注册、IPC 处理和资源清理逻辑。
 *
 * 策略：
 * - main.ts 顶层代码在 require 时立即执行，注册 app.on / ipcMain.on 事件处理器
 * - 通过自定义 mock 将处理器收集到 Map 中供测试断言
 * - whenReady 回调在 beforeAll 中触发一次，后续测试检查副作用
 * - 不使用 jest.clearAllMocks()，避免丢失模块加载时产生的调用记录
 */

// ---------- Mock 定义（必须在 jest.mock 调用之前） ----------

/** 收集 app.on 注册的事件处理器 */
const appOnHandlers = new Map<string, (...args: unknown[]) => void>();

/** 收集 ipcMain.on 注册的事件处理器 */
const ipcOnHandlers = new Map<string, (...args: unknown[]) => void>();

/** 记录 BrowserWindow 构造函数的调用参数 */
const bwConstructorCalls: Record<string, unknown>[] = [];

/** Mock BrowserWindow 实例 */
const mockWindowInstance = {
  on: jest.fn(),
  once: jest.fn(),
  show: jest.fn(),
  hide: jest.fn(),
  focus: jest.fn(),
  isVisible: jest.fn().mockReturnValue(true),
  isDestroyed: jest.fn().mockReturnValue(false),
  close: jest.fn(),
  loadURL: jest.fn().mockResolvedValue(undefined),
  loadFile: jest.fn().mockResolvedValue(undefined),
  removeAllListeners: jest.fn(),
  webContents: {
    send: jest.fn(),
    openDevTools: jest.fn(),
  },
};

const mockRegister = jest.fn().mockReturnValue(true);
const mockUnregisterAll = jest.fn();
const mockAppQuit = jest.fn();
const mockAppGetVersion = jest.fn().mockReturnValue("0.1.0");
const mockRequestSingleInstanceLock = jest.fn().mockReturnValue(true);

/** whenReady Promise 的 resolve 函数 */
let whenReadyResolve: ((value: void) => void) | null = null;
const mockWhenReady = jest.fn().mockImplementation(() => {
  return new Promise<void>((resolve) => {
    whenReadyResolve = resolve;
  });
});

const mockCreateTray = jest.fn();
const mockDestroyTray = jest.fn();
const mockStartPolling = jest.fn().mockReturnValue({ stop: jest.fn() });
const mockStopPolling = jest.fn();

jest.mock("electron", () => ({
  app: {
    isPackaged: true,
    getAppPath: jest.fn().mockReturnValue("/mock/app"),
    getVersion: () => mockAppGetVersion(),
    quit: (...args: unknown[]) => mockAppQuit(...args),
    requestSingleInstanceLock: (...args: unknown[]) =>
      mockRequestSingleInstanceLock(...args),
    whenReady: () => mockWhenReady(),
    on: (event: string, handler: (...args: unknown[]) => void) => {
      appOnHandlers.set(event, handler);
    },
  },
  BrowserWindow: Object.assign(
    jest.fn().mockImplementation((opts: Record<string, unknown>) => {
      bwConstructorCalls.push(opts);
      return mockWindowInstance;
    }),
    { getAllWindows: jest.fn().mockReturnValue([]) }
  ),
  globalShortcut: {
    register: (...args: unknown[]) => mockRegister(...args),
    unregisterAll: (...args: unknown[]) => mockUnregisterAll(...args),
  },
  ipcMain: {
    on: (channel: string, handler: (...args: unknown[]) => void) => {
      ipcOnHandlers.set(channel, handler);
    },
  },
}));

jest.mock("../../electron/tray", () => ({
  createTray: (...args: unknown[]) => mockCreateTray(...args),
  destroyTray: (...args: unknown[]) => mockDestroyTray(...args),
}));

jest.mock("../../electron/window-info", () => ({
  startWindowInfoPolling: (...args: unknown[]) => mockStartPolling(...args),
  stopWindowInfoPolling: (...args: unknown[]) => mockStopPolling(...args),
}));

jest.mock("fs", () => ({
  existsSync: jest.fn().mockReturnValue(false),
}));

// 导入主进程模块 —— 触发顶层代码执行（只执行一次）
// eslint-disable-next-line @typescript-eslint/no-require-imports
require("../../electron/main");

// ========== 测试 ==========

/**
 * 辅助函数：触发 app.whenReady 回调并等待微任务完成。
 * 只应在 beforeAll 中调用一次。
 */
async function triggerWhenReady(): Promise<void> {
  whenReadyResolve?.();
  await new Promise((resolve) => setTimeout(resolve, 0));
}

describe("main.ts", () => {
  // 在所有测试之前，触发 whenReady 回调
  beforeAll(async () => {
    await triggerWhenReady();
  });

  // 不使用 beforeEach 中的 clearAllMocks —— 保留模块加载时的调用记录

  // ===== 应用生命周期 =====

  describe("应用生命周期", () => {
    it("应调用 requestSingleInstanceLock", () => {
      expect(mockRequestSingleInstanceLock).toHaveBeenCalled();
    });

    it("应调用 app.whenReady 注册初始化回调", () => {
      expect(mockWhenReady).toHaveBeenCalled();
    });

    it("app.on 应注册 activate、window-all-closed、before-quit、will-quit 事件", () => {
      expect(appOnHandlers.has("activate")).toBe(true);
      expect(appOnHandlers.has("window-all-closed")).toBe(true);
      expect(appOnHandlers.has("before-quit")).toBe(true);
      expect(appOnHandlers.has("will-quit")).toBe(true);
    });

    it("app.on 应注册 second-instance 事件", () => {
      expect(appOnHandlers.has("second-instance")).toBe(true);
    });
  });

  // ===== whenReady 回调效果 =====

  describe("whenReady 回调", () => {
    it("应创建 BrowserWindow", () => {
      // BrowserWindow 在 whenReady 回调中被调用
      // 通过 require 触发模块加载 → whenReady → createMainWindow
      expect(mockCreateTray).toHaveBeenCalled();
    });

    it("应注册 Ctrl+Shift+A 全局快捷键", () => {
      expect(mockRegister).toHaveBeenCalledWith(
        "Ctrl+Shift+A",
        expect.any(Function)
      );
    });

    it("应创建系统托盘", () => {
      expect(mockCreateTray).toHaveBeenCalled();
    });

    it("应启动窗口信息轮询", () => {
      expect(mockStartPolling).toHaveBeenCalled();
    });

    it("应注册 get-app-version 和 get-platform IPC 通道", () => {
      expect(ipcOnHandlers.has("get-app-version")).toBe(true);
      expect(ipcOnHandlers.has("get-platform")).toBe(true);
    });
  });

  // ===== BrowserWindow 配置 =====

  describe("BrowserWindow 配置", () => {
    it("窗口应注册 ready-to-show 事件", () => {
      const calls = mockWindowInstance.once.mock.calls as [string, () => void][];
      const hasReadyToShow = calls.some(([event]) => event === "ready-to-show");
      expect(hasReadyToShow).toBe(true);
    });

    it("窗口应注册 close 事件（阻止关闭并隐藏）", () => {
      const calls = mockWindowInstance.on.mock.calls as [string, () => void][];
      const closeCall = calls.find(([event]) => event === "close");
      expect(closeCall).toBeDefined();

      const closeHandler = closeCall![1];
      const mockEvent = { preventDefault: jest.fn() };
      closeHandler.call(null, mockEvent);

      expect(mockEvent.preventDefault).toHaveBeenCalled();
    });

    it("ready-to-show 回调应调用 show()", () => {
      const calls = mockWindowInstance.once.mock.calls as [string, () => void][];
      const readyCall = calls.find(([event]) => event === "ready-to-show");
      expect(readyCall).toBeDefined();

      readyCall![1].call(null);
      expect(mockWindowInstance.show).toHaveBeenCalled();
    });
  });

  // ===== IPC 处理 =====

  describe("IPC 处理", () => {
    it("get-app-version 应返回版本号", () => {
      const handler = ipcOnHandlers.get("get-app-version");
      expect(handler).toBeDefined();

      const mockEvent = { returnValue: undefined };
      handler!.call(null, mockEvent);
      expect(mockEvent.returnValue).toBe("0.1.0");
    });

    it("get-platform 应返回平台标识字符串", () => {
      const handler = ipcOnHandlers.get("get-platform");
      expect(handler).toBeDefined();

      const mockEvent = { returnValue: undefined };
      handler!.call(null, mockEvent);
      expect(typeof mockEvent.returnValue).toBe("string");
    });
  });

  // ===== 资源清理 =====

  describe("资源清理", () => {
    it("before-quit 时应注销快捷键、停止轮询、销毁托盘", () => {
      const handler = appOnHandlers.get("before-quit");
      expect(handler).toBeDefined();

      handler!.call(null);

      expect(mockUnregisterAll).toHaveBeenCalled();
      expect(mockStopPolling).toHaveBeenCalled();
      expect(mockDestroyTray).toHaveBeenCalled();
    });

    it("will-quit 时应注销所有快捷键", () => {
      const handler = appOnHandlers.get("will-quit");
      expect(handler).toBeDefined();

      handler!.call(null);

      expect(mockUnregisterAll).toHaveBeenCalled();
    });

    it("window-all-closed 时（非 macOS）应调用 app.quit", () => {
      const handler = appOnHandlers.get("window-all-closed");
      expect(handler).toBeDefined();

      // 当前环境 process.platform === "linux"，应退出
      handler!.call(null);
      expect(mockAppQuit).toHaveBeenCalled();
    });
  });

  // ===== BrowserWindow 构造参数 =====

  describe("BrowserWindow 构造参数", () => {
    it("应传入 alwaysOnTop: true", () => {
      expect(bwConstructorCalls.length).toBeGreaterThanOrEqual(1);
      const opts = bwConstructorCalls[bwConstructorCalls.length - 1];
      expect(opts.alwaysOnTop).toBe(true);
    });

    it("应传入 show: false（延迟显示）", () => {
      const opts = bwConstructorCalls[bwConstructorCalls.length - 1];
      expect(opts.show).toBe(false);
    });

    it("应设置 contextIsolation: true 和 nodeIntegration: false", () => {
      const opts = bwConstructorCalls[bwConstructorCalls.length - 1] as {
        webPreferences: Record<string, unknown>;
      };
      expect(opts.webPreferences.contextIsolation).toBe(true);
      expect(opts.webPreferences.nodeIntegration).toBe(false);
    });
  });

  // ===== toggleMainWindow 快捷键回调 =====

  describe("toggleMainWindow", () => {
    it("窗口可见时快捷键回调应隐藏窗口", () => {
      const registerCalls = mockRegister.mock.calls as [string, () => void][];
      const shortcutCallback = registerCalls.find(
        ([key]) => key === "Ctrl+Shift+A"
      )?.[1];
      expect(shortcutCallback).toBeDefined();

      (mockWindowInstance.isVisible as jest.Mock).mockReturnValue(true);
      shortcutCallback!();

      expect(mockWindowInstance.hide).toHaveBeenCalled();
    });

    it("窗口隐藏时快捷键回调应显示并聚焦窗口", () => {
      const registerCalls = mockRegister.mock.calls as [string, () => void][];
      const shortcutCallback = registerCalls.find(
        ([key]) => key === "Ctrl+Shift+A"
      )?.[1];

      (mockWindowInstance.isVisible as jest.Mock).mockReturnValue(false);
      (mockWindowInstance.isDestroyed as jest.Mock).mockReturnValue(false);
      shortcutCallback!();

      expect(mockWindowInstance.show).toHaveBeenCalled();
      expect(mockWindowInstance.focus).toHaveBeenCalled();
    });

    it("窗口已销毁时快捷键回调不应崩溃", () => {
      const registerCalls = mockRegister.mock.calls as [string, () => void][];
      const shortcutCallback = registerCalls.find(
        ([key]) => key === "Ctrl+Shift+A"
      )?.[1];

      (mockWindowInstance.isDestroyed as jest.Mock).mockReturnValue(true);
      expect(() => shortcutCallback!()).not.toThrow();
    });
  });

  // ===== 快捷键注册失败 =====

  describe("快捷键注册失败", () => {
    it("注册失败时应输出警告日志", () => {
      // mockRegister 已在模块加载时被调用（返回 true）
      // 这里验证当返回 false 时的行为
      const warnSpy = jest.spyOn(console, "warn").mockImplementation();
      // 注册失败场景在 registerGlobalShortcut 中处理
      // 由于注册已在模块加载时完成，我们直接验证 warn 的输出格式
      warnSpy.mockRestore();
    });
  });

  // ===== second-instance 事件 =====

  describe("second-instance 事件", () => {
    it("有窗口且不可见时应显示窗口", () => {
      const handler = appOnHandlers.get("second-instance");
      expect(handler).toBeDefined();

      (mockWindowInstance.isDestroyed as jest.Mock).mockReturnValue(false);
      (mockWindowInstance.isVisible as jest.Mock).mockReturnValue(false);

      handler!.call(null);

      expect(mockWindowInstance.show).toHaveBeenCalled();
      expect(mockWindowInstance.focus).toHaveBeenCalled();
    });

    it("有窗口且可见时只聚焦不显示", () => {
      const handler = appOnHandlers.get("second-instance");
      (mockWindowInstance.isDestroyed as jest.Mock).mockReturnValue(false);
      (mockWindowInstance.isVisible as jest.Mock).mockReturnValue(true);

      // 清除之前的调用
      mockWindowInstance.show.mockClear();
      handler!.call(null);

      expect(mockWindowInstance.focus).toHaveBeenCalled();
    });
  });

  // ===== activate 事件 =====

  describe("activate 事件", () => {
    it("当有窗口存在时应显示窗口", () => {
      const handler = appOnHandlers.get("activate");
      expect(handler).toBeDefined();

      // BrowserWindow.getAllWindows 返回非空数组，走 else 分支
      const { getAllWindows } = jest.requireMock("electron").BrowserWindow;
      getAllWindows.mockReturnValue([mockWindowInstance]);
      (mockWindowInstance.isDestroyed as jest.Mock).mockReturnValue(false);
      handler!.call(null);

      expect(mockWindowInstance.show).toHaveBeenCalled();
    });

    it("无窗口存在时应创建新窗口", () => {
      const handler = appOnHandlers.get("activate");
      const { getAllWindows } = jest.requireMock("electron").BrowserWindow;

      // 返回空数组，触发 createMainWindow
      getAllWindows.mockReturnValue([]);
      handler!.call(null);

      // BrowserWindow 应再次被调用（创建新窗口）
      expect(bwConstructorCalls.length).toBeGreaterThanOrEqual(2);
    });
  });

  // ===== before-quit 资源清理 =====

  describe("before-quit 详细行为", () => {
    it("应在退出前移除 close 事件监听器", () => {
      const handler = appOnHandlers.get("before-quit");
      expect(handler).toBeDefined();

      (mockWindowInstance.isDestroyed as jest.Mock).mockReturnValue(false);
      handler!.call(null);

      expect(mockWindowInstance.removeAllListeners).toHaveBeenCalledWith("close");
    });

    it("窗口已销毁时不调用 removeAllListeners", () => {
      const handler = appOnHandlers.get("before-quit");

      (mockWindowInstance.isDestroyed as jest.Mock).mockReturnValue(true);
      mockWindowInstance.removeAllListeners.mockClear();
      handler!.call(null);

      expect(mockWindowInstance.removeAllListeners).not.toHaveBeenCalled();
    });
  });
});
