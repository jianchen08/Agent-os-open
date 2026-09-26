// @ci: frontend-test
/**
 * 内核启动失败的用户可见反馈（BUG-86）。
 *
 * 行为契约（main.ts bootPackagedKernelThenLoad）：
 *  - ensurePackagedKernelRunning 失败（内核秒退/端口占用/组件缺失/就绪超时）
 *    → dialog.showErrorBox("灵汐助手 启动失败", <原因>) + app.exit(1)：
 *    失败必须以弹窗呈现原因，绝不静默留孤儿窗口（用户视角=「双击打开却
 *    永远连接不上内核」而无任何解释）；
 *  - ensure 成功 → 不弹窗不退出，主窗口加载前端（app://bundle/index.html）。
 *
 * kernel-manager 是本测试 mock 的外部边界（真实拉起属 kernel-manager.test.ts
 * 职责）；Electron 运行时同口径 mock（与 dialog.test.ts 一致）。
 */

import { beforeEach, describe, expect, it, vi } from "vitest";

const electronMock = vi.hoisted(() => {
  const app = {
    get isPackaged() {
      return true;
    },
    requestSingleInstanceLock: vi.fn(() => true),
    setPath: vi.fn(),
    exit: vi.fn(),
    getPath: vi.fn(() => "C:\\Users\\t\\AppData\\Roaming"),
    getName: vi.fn(() => "agent-os"),
    quit: vi.fn(),
    on: vi.fn(),
    whenReady: vi.fn(() => new Promise(() => {})),
    getAppPath: vi.fn(() => "."),
    setAppUserModelId: vi.fn(),
  };
  return { app };
});

const kernelManagerMock = vi.hoisted(() => {
  const state = {
    ensureResult: "ok" as "ok" | "reject",
    ensureError: new Error("内核进程在启动期间退出（code=1）。请重试。"),
  };
  const ensurePackagedKernelRunning = vi.fn(() =>
    state.ensureResult === "ok"
      ? Promise.resolve({ mode: "spawned" as const, pid: 4321 })
      : Promise.reject(state.ensureError),
  );
  return { state, ensurePackagedKernelRunning };
});

vi.mock("electron", () => ({
  safeStorage: undefined,
  app: electronMock.app,
  BrowserWindow: vi.fn(),
  Notification: vi.fn(),
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

// 部分 mock：app-protocol 等兄弟模块消费 kernel-manager 的常量导出（KERNEL_PORT 等），
// 只替换启动副作用入口，其余保持真实实现
vi.mock("../kernel-manager", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../kernel-manager")>()),
  ensurePackagedKernelRunning: kernelManagerMock.ensurePackagedKernelRunning,
  shutdownManagedKernel: vi.fn(),
}));

// vi.mock 提升到文件最前，顶层动态导入拿到的 main 模块已处于 mock 环境
const { bootPackagedKernelThenLoad } = await import("../main");
// dialog mock 在 vi.mock 工厂内联，经模块实例取回同一实例断言
const electronActual = (await import("electron")) as unknown as {
  dialog: { showErrorBox: ReturnType<typeof vi.fn> };
};

/** 造一个可观察 loadURL 的假主窗口（boot 成功路径会经 loadFrontend 载前端） */
const makeFakeWin = () => ({
  isDestroyed: () => false,
  loadURL: vi.fn(() => Promise.resolve()),
});

describe("bootPackagedKernelThenLoad（内核启动失败可见反馈，BUG-86）", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    kernelManagerMock.state.ensureResult = "ok";
    kernelManagerMock.state.ensureError = new Error("内核进程在启动期间退出（code=1）。请重试。");
  });

  it("内核秒退（ensure 拒绝）→ showErrorBox 弹出原因 + app.exit(1)，不静默", async () => {
    kernelManagerMock.state.ensureResult = "reject";
    kernelManagerMock.state.ensureError = new Error(
      "内核进程在启动期间退出（code=-1073741515）。请重试；若反复出现，请反馈内核日志。",
    );

    await bootPackagedKernelThenLoad(makeFakeWin() as never);

    expect(electronActual.dialog.showErrorBox).toHaveBeenCalledTimes(1);
    const [title, message] = electronActual.dialog.showErrorBox.mock.calls[0] as [string, string];
    expect(title).toBe("灵汐助手 启动失败");
    expect(message).toContain("启动期间退出");
    expect(electronMock.app.exit).toHaveBeenCalledTimes(1);
    expect(electronMock.app.exit).toHaveBeenCalledWith(1);
  });

  it.each([
    ["KernelPortBusyError（端口被占用）", new Error("端口 9101 已被其他内核占用。")],
    ["KernelMissingError（组件缺失）", new Error("应用安装损坏：缺少内核组件。请重新安装。")],
    ["就绪超时", new Error("内核在 300 秒内未就绪，应用将退出。")],
  ])("%s → 同样弹窗带原因并退出", async (_name, err) => {
    kernelManagerMock.state.ensureResult = "reject";
    kernelManagerMock.state.ensureError = err;

    await bootPackagedKernelThenLoad(makeFakeWin() as never);

    expect(electronActual.dialog.showErrorBox).toHaveBeenCalledWith(
      "灵汐助手 启动失败",
      err.message,
    );
    expect(electronMock.app.exit).toHaveBeenCalledWith(1);
  });

  it("非 Error 拒绝值（字符串）→ 原样字符串化呈现，仍退出", async () => {
    kernelManagerMock.state.ensureResult = "reject";
    kernelManagerMock.state.ensureError = "spawn EFTYPE" as unknown as Error;

    await bootPackagedKernelThenLoad(makeFakeWin() as never);

    expect(electronActual.dialog.showErrorBox).toHaveBeenCalledWith(
      "灵汐助手 启动失败",
      "spawn EFTYPE",
    );
    expect(electronMock.app.exit).toHaveBeenCalledWith(1);
  });

  it("内核就绪（ensure 成功）→ 不弹窗不退出，主窗口加载 app:// 前端", async () => {
    const win = makeFakeWin();

    await bootPackagedKernelThenLoad(win as never);

    expect(kernelManagerMock.ensurePackagedKernelRunning).toHaveBeenCalledTimes(1);
    expect(electronActual.dialog.showErrorBox).not.toHaveBeenCalled();
    expect(electronMock.app.exit).not.toHaveBeenCalled();
    expect(win.loadURL).toHaveBeenCalledWith("app://bundle/index.html");
  });
});
