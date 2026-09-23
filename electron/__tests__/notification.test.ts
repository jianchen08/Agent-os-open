// @feature: FP-0.2.五 审批闭环 | @ci: frontend-test
/**
 * 系统通知链路测试（main.ts 的 notification:show 支撑面）。
 *
 * 行为契约：
 *  - showSystemNotification：title 非空字符串才弹；body 非字符串折算为空串；
 *    宿主不支持（Notification.isSupported()=false）返回 false 不弹；弹出时
 *    一律 silent:true（提示音由渲染进程 Web Audio 统一合成，避免双重响），
 *    并注册 click → 聚焦主窗口（窗口未就绪时安全无操作）；
 *  - focusMainWindow：还原最小化 → 显示 → 聚焦，窗口缺失/已销毁静默返回
 *    （second-instance 与通知点击共用同一恢复序列）；
 *  - Windows toast 的进程身份：打包件 AUMID = electron-builder appId
 *    （com.agentos.assistant，与安装快捷方式一致），dev = exe 路径兜底
 *    （Electron 文档口径）；非 Windows 平台不设置。模块加载期设置一次，
 *    必须先于任何 Notification 弹出。
 *
 * Electron 运行时是本测试唯一 mock 的外部边界（与 single-instance.test.ts
 * 同口径）；showSystemNotification/focusMainWindow 为导出函数，直接单测，
 * 不经 whenReady（whenReady 注入永不 resolve 的 promise，维持启动序列可区分性）。
 */

import { afterAll, beforeEach, describe, expect, it, vi } from "vitest";

const electronMock = vi.hoisted(() => {
  const notificationInstances: Array<{
    opts: unknown;
    show: ReturnType<typeof vi.fn>;
    on: ReturnType<typeof vi.fn>;
  }> = [];

  class FakeNotification {
    static isSupported = vi.fn(() => true);
    opts: unknown;
    show = vi.fn();
    on = vi.fn();
    constructor(opts: unknown) {
      this.opts = opts;
      notificationInstances.push(this);
    }
  }

  const state = { isPackaged: true };
  const app = {
    get isPackaged() {
      return state.isPackaged;
    },
    requestSingleInstanceLock: vi.fn(() => true),
    setPath: vi.fn(),
    exit: vi.fn(),
    getPath: vi.fn((name: string) =>
      name === "exe"
        ? "C:\\inst\\app\\灵汐助手.exe"
        : "C:\\Users\\t\\AppData\\Roaming",
    ),
    getName: vi.fn(() => "agent-os"),
    quit: vi.fn(),
    on: vi.fn(),
    whenReady: vi.fn(() => new Promise(() => {})),
    getAppPath: vi.fn(() => "."),
    setAppUserModelId: vi.fn(),
  };

  return { state, app, FakeNotification, notificationInstances };
});

vi.mock("electron", () => ({
  app: electronMock.app,
  BrowserWindow: vi.fn(),
  Notification: electronMock.FakeNotification,
  dialog: { showErrorBox: vi.fn() },
  globalShortcut: { register: vi.fn(), unregister: vi.fn(), unregisterAll: vi.fn() },
  ipcMain: { handle: vi.fn(), on: vi.fn() },
  Menu: { setApplicationMenu: vi.fn(), buildFromTemplate: vi.fn() },
  shell: { openExternal: vi.fn() },
  protocol: { registerSchemesAsPrivileged: vi.fn(), handle: vi.fn() },
  net: {},
  Tray: vi.fn(),
  nativeImage: { createFromPath: vi.fn() },
}));

const realPlatform = process.platform;
function setPlatform(platform: string): void {
  Object.defineProperty(process, "platform", { value: platform });
}
afterAll(() => {
  setPlatform(realPlatform);
});

// vi.mock 提升到文件最前，顶层动态导入拿到的 main 模块已处于 mock 环境
const { showSystemNotification, focusMainWindow } = await import("../main");

/** 用假窗口对象驱动 focusMainWindow 的各分支 */
function makeFakeWin(overrides: Partial<Record<string, unknown>> = {}) {
  return {
    isDestroyed: vi.fn(() => false),
    isMinimized: vi.fn(() => false),
    isVisible: vi.fn(() => true),
    restore: vi.fn(),
    show: vi.fn(),
    focus: vi.fn(),
    ...overrides,
  };
}

describe("showSystemNotification", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    electronMock.notificationInstances.length = 0;
    electronMock.FakeNotification.isSupported.mockReturnValue(true);
  });

  it("title/body 合法 → 弹出（silent:true）、注册 click、返回 true", () => {
    const shown = showSystemNotification({ title: "审批", body: "请确认" });
    expect(shown).toBe(true);
    expect(electronMock.notificationInstances).toHaveLength(1);
    const instance = electronMock.notificationInstances[0]!;
    expect(instance.opts).toEqual({ title: "审批", body: "请确认", silent: true });
    expect(instance.on).toHaveBeenCalledWith("click", expect.any(Function));
    expect(instance.show).toHaveBeenCalledTimes(1);
  });

  it("body 非字符串 → 折算为空串照常弹出", () => {
    const shown = showSystemNotification({ title: "审批", body: 42 });
    expect(shown).toBe(true);
    expect(electronMock.notificationInstances[0]!.opts).toEqual({
      title: "审批",
      body: "",
      silent: true,
    });
  });

  it.each([
    ["opts 缺失", undefined],
    ["title 缺失", {}],
    ["title 空串", { title: "" }],
    ["title 非字符串", { title: 123 }],
  ])("%s → 返回 false 且不弹", (_name, opts) => {
    expect(
      showSystemNotification(opts as { title?: unknown; body?: unknown }),
    ).toBe(false);
    expect(electronMock.notificationInstances).toHaveLength(0);
  });

  it("宿主不支持系统通知 → 返回 false 且不构造通知", () => {
    electronMock.FakeNotification.isSupported.mockReturnValue(false);
    expect(showSystemNotification({ title: "审批", body: "x" })).toBe(false);
    expect(electronMock.notificationInstances).toHaveLength(0);
  });

  it("通知 click（主窗口未就绪）→ 安全无操作不抛错", () => {
    showSystemNotification({ title: "审批", body: "x" });
    const instance = electronMock.notificationInstances[0]!;
    const clickHandler = instance.on.mock.calls.find(
      ([event]) => event === "click",
    )?.[1] as () => void;
    expect(() => clickHandler()).not.toThrow();
  });
});

describe("focusMainWindow（恢复序列：还原 → 显示 → 聚焦）", () => {
  it("窗口缺失（null）→ 静默返回", () => {
    expect(() => focusMainWindow(null)).not.toThrow();
  });

  it("窗口已销毁 → 不做任何窗口操作", () => {
    const win = makeFakeWin({ isDestroyed: vi.fn(() => true) });
    focusMainWindow(win as never);
    expect(win.restore).not.toHaveBeenCalled();
    expect(win.show).not.toHaveBeenCalled();
    expect(win.focus).not.toHaveBeenCalled();
  });

  it("最小化且隐藏 → restore + show + focus 全序列", () => {
    const win = makeFakeWin({
      isMinimized: vi.fn(() => true),
      isVisible: vi.fn(() => false),
    });
    focusMainWindow(win as never);
    expect(win.restore).toHaveBeenCalledTimes(1);
    expect(win.show).toHaveBeenCalledTimes(1);
    expect(win.focus).toHaveBeenCalledTimes(1);
  });

  it("正常可见 → 只聚焦（不 restore 不 show）", () => {
    const win = makeFakeWin();
    focusMainWindow(win as never);
    expect(win.restore).not.toHaveBeenCalled();
    expect(win.show).not.toHaveBeenCalled();
    expect(win.focus).toHaveBeenCalledTimes(1);
  });
});

describe("Windows AUMID 进程身份（模块加载期设置一次）", () => {
  beforeEach(() => {
    vi.resetModules();
    vi.clearAllMocks();
  });

  it("win32 打包件 → setAppUserModelId(electron-builder appId)", async () => {
    setPlatform("win32");
    electronMock.state.isPackaged = true;
    await import("../main");
    expect(electronMock.app.setAppUserModelId).toHaveBeenCalledWith(
      "com.agentos.assistant",
    );
  });

  it("win32 dev（非打包）→ setAppUserModelId(exe 路径兜底)", async () => {
    setPlatform("win32");
    electronMock.state.isPackaged = false;
    await import("../main");
    expect(electronMock.app.setAppUserModelId).toHaveBeenCalledWith(
      process.execPath,
    );
  });

  it("非 win32（darwin/linux）→ 不设置 AUMID", async () => {
    setPlatform("darwin");
    electronMock.state.isPackaged = true;
    await import("../main");
    expect(electronMock.app.setAppUserModelId).not.toHaveBeenCalled();
  });
});
