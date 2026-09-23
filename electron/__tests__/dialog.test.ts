// @feature: FP-0.2.三 宿主接入 | @ci: frontend-test
/**
 * 原生目录选择链路测试（main.ts 的 dialog:pick-directory 支撑面）。
 *
 * 行为契约：
 *  - pickDirectoryViaDialog：showOpenDialog 以 openDirectory 模式弹出，
 *    发起方窗口为父窗（模态）；返回所选绝对路径（单一选择，无多选）；
 *  - 用户取消（canceled）/未选任何目录（filePaths 空）→ 返回 null；
 *  - 发起方窗口缺失（fromWebContents=null，webContents 已销毁等）→
 *    以无父窗形态仍可弹出选择（功能不随窗口销毁失效）。
 *
 * Electron 运行时是本测试唯一 mock 的外部边界（与 notification.test.ts
 * 同口径）；pickDirectoryViaDialog 为导出函数直接单测，不经 whenReady。
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

  const state = {
    dialogResult: { canceled: false, filePaths: [] as string[] },
    dialogCalls: [] as Array<{ args: unknown[] }>,
  };

  const browserWindowStatics = { fromWebContents: vi.fn(() => null) };

  const BrowserWindow = Object.assign(vi.fn(), {
    fromWebContents: browserWindowStatics.fromWebContents,
  });

  const dialog = {
    showErrorBox: vi.fn(),
    showOpenDialog: vi.fn((...args: unknown[]) => {
      state.dialogCalls.push({ args });
      return Promise.resolve(state.dialogResult);
    }),
  };

  return { app, state, browserWindowStatics, BrowserWindow, dialog };
});

vi.mock("electron", () => ({
  app: electronMock.app,
  BrowserWindow: electronMock.BrowserWindow,
  Notification: vi.fn(),
  dialog: electronMock.dialog,
  globalShortcut: { register: vi.fn(), unregister: vi.fn(), unregisterAll: vi.fn() },
  ipcMain: { handle: vi.fn(), on: vi.fn() },
  Menu: { setApplicationMenu: vi.fn(), buildFromTemplate: vi.fn() },
  shell: { openExternal: vi.fn() },
  protocol: { registerSchemesAsPrivileged: vi.fn(), handle: vi.fn() },
  net: {},
  Tray: vi.fn(),
  nativeImage: { createFromPath: vi.fn() },
}));

// vi.mock 提升到文件最前，顶层动态导入拿到的 main 模块已处于 mock 环境
const { pickDirectoryViaDialog } = await import("../main");

const fakeEvent = { sender: { id: 1 } } as never;

describe("pickDirectoryViaDialog", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    electronMock.state.dialogCalls.length = 0;
    electronMock.state.dialogResult = { canceled: false, filePaths: [] };
    electronMock.browserWindowStatics.fromWebContents.mockReturnValue(null);
  });

  it("选中目录 → 返回绝对路径；openDirectory 模态弹出且发起窗为父窗", async () => {
    electronMock.browserWindowStatics.fromWebContents.mockReturnValue({ fakeWin: true });
    electronMock.state.dialogResult = { canceled: false, filePaths: ["D:\\proj"] };

    await expect(pickDirectoryViaDialog(fakeEvent)).resolves.toBe("D:\\proj");

    const args = electronMock.state.dialogCalls.at(-1)!.args;
    expect(args[0]).toEqual({ fakeWin: true });
    expect(args[1]).toEqual({ properties: ["openDirectory", "createDirectory"] });
  });

  it.each([
    ["用户取消（canceled）", { canceled: true, filePaths: [] }],
    ["未选任何目录（canceled=false 且 filePaths 空）", { canceled: false, filePaths: [] }],
  ])("%s → 返回 null 不回填", async (_name, result) => {
    electronMock.state.dialogResult = result;
    await expect(pickDirectoryViaDialog(fakeEvent)).resolves.toBeNull();
  });

  it("发起窗口缺失（fromWebContents=null）→ 无父窗弹出（单参 options），功能不失效", async () => {
    electronMock.state.dialogResult = { canceled: false, filePaths: ["C:\\ws"] };

    await expect(pickDirectoryViaDialog(fakeEvent)).resolves.toBe("C:\\ws");

    const args = electronMock.state.dialogCalls.at(-1)!.args;
    expect(args).toHaveLength(1);
    expect(args[0]).toEqual({ properties: ["openDirectory", "createDirectory"] });
  });

  it("多路径返回只取首个（单一目录选择契约）", async () => {
    electronMock.state.dialogResult = {
      canceled: false,
      filePaths: ["C:\\a", "C:\\b"],
    };
    await expect(pickDirectoryViaDialog(fakeEvent)).resolves.toBe("C:\\a");
  });
});
