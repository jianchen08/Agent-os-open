/**
 * 单实例锁守卫回归（BUG-29）。
 *
 * 行为契约：requestSingleInstanceLock 返回 false（锁被首实例持有）时，
 * 第二实例必须在模块加载期同步 app.quit() 自退——不会走到 whenReady，
 * 因而不创建窗口/托盘、不拉内核；返回 true（首实例）时不退出，且
 * second-instance 聚焦处理器保持注册。
 *
 * Electron 运行时是本测试唯一 mock 的外部边界（单测环境无真实 app 生命周期
 * 可观察）；whenReady 注入永不 resolve 的 promise，使「锁失败即 quit」与
 * 「quit 被误放进 ready 之后」两种实现可区分。
 */

import { beforeEach, describe, expect, it, vi } from "vitest";

const electronMock = vi.hoisted(() => {
  const state = { lockGranted: false };
  const app = {
    isPackaged: true,
    requestSingleInstanceLock: vi.fn(() => state.lockGranted),
    quit: vi.fn(),
    on: vi.fn(),
    whenReady: vi.fn(() => new Promise(() => {})),
    getAppPath: vi.fn(() => "."),
  };
  return { state, app };
});

vi.mock("electron", () => ({
  app: electronMock.app,
  BrowserWindow: vi.fn(),
  dialog: { showErrorBox: vi.fn() },
  globalShortcut: { register: vi.fn(), unregister: vi.fn(), unregisterAll: vi.fn() },
  ipcMain: { handle: vi.fn() },
  Menu: { setApplicationMenu: vi.fn(), buildFromTemplate: vi.fn() },
  shell: { openExternal: vi.fn() },
  protocol: { registerSchemesAsPrivileged: vi.fn(), handle: vi.fn() },
  net: {},
  Tray: vi.fn(),
  nativeImage: { createFromPath: vi.fn() },
}));

describe("单实例锁守卫（BUG-29）", () => {
  beforeEach(() => {
    vi.resetModules();
    vi.clearAllMocks();
  });

  it("锁被首实例持有（返回 false）→ 第二实例模块加载期即 app.quit 自退", async () => {
    electronMock.state.lockGranted = false;
    await import("../main");
    expect(electronMock.app.requestSingleInstanceLock).toHaveBeenCalledTimes(1);
    expect(electronMock.app.quit).toHaveBeenCalledTimes(1);
  });

  it("锁获取成功（返回 true）→ 首实例不退出，second-instance 聚焦处理器保持注册", async () => {
    electronMock.state.lockGranted = true;
    await import("../main");
    expect(electronMock.app.quit).not.toHaveBeenCalled();
    const secondInstanceRegistered = electronMock.app.on.mock.calls.some(
      ([event]) => event === "second-instance",
    );
    expect(secondInstanceRegistered).toBe(true);
  });
});
