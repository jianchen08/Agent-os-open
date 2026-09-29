// @feature: FP-0.2.三 宿主接入 | @ci: frontend-test
/**
 * 应用壳进程树内存链路测试（main.ts 的 app:metrics 支撑面）。
 *
 * 行为契约：
 *  - collectAppMetrics 汇总 getAppMetrics 全部进程的 workingSetSize（KB 求和）
 *    与进程数，供监控页全口径内存的应用壳段消费；
 *  - 空进程树 → processCount 0 / totalWorkingSetKb 0（不虚构非零）；
 *  - 只取 workingSetSize，进程对象其余字段（cpu/creationTime 等）不进求和。
 *
 * Electron 运行时是本测试唯一 mock 的外部边界（与 dialog.test.ts 同口径）；
 * collectAppMetrics 为导出函数直接单测，不经 whenReady。
 */

import { beforeEach, describe, expect, it, vi } from "vitest";

const electronMock = vi.hoisted(() => {
  const state = {
    metrics: [] as Array<{
      memory: { workingSetSize: number };
      cpu?: unknown;
      type?: string;
    }>,
  };
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
    getAppMetrics: vi.fn(() => state.metrics),
  };
  return { app, state };
});

vi.mock("electron", () => ({
  safeStorage: undefined,
  app: electronMock.app,
  BrowserWindow: Object.assign(vi.fn(), { fromWebContents: vi.fn(() => null) }),
  Notification: vi.fn(),
  dialog: { showErrorBox: vi.fn(), showOpenDialog: vi.fn() },
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
const { collectAppMetrics } = await import("../main");

describe("collectAppMetrics", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    electronMock.state.metrics = [];
  });

  it("多进程 workingSetSize 逐项求和并报进程数（KB 口径原样透出）", () => {
    electronMock.state.metrics = [
      { type: "browser", memory: { workingSetSize: 102400 }, cpu: {} },
      { type: "renderer", memory: { workingSetSize: 204800 }, cpu: {} },
      { type: "gpu-process", memory: { workingSetSize: 51200 }, cpu: {} },
    ];

    expect(collectAppMetrics()).toEqual({
      processCount: 3,
      totalWorkingSetKb: 102400 + 204800 + 51200,
    });
  });

  it("空进程树 → processCount 0 / totalWorkingSetKb 0（不虚构非零）", () => {
    expect(collectAppMetrics()).toEqual({ processCount: 0, totalWorkingSetKb: 0 });
  });

  it("单进程原样透出；字段名与 preload 契约一致（processCount/totalWorkingSetKb）", () => {
    electronMock.state.metrics = [{ memory: { workingSetSize: 716800 } }];
    const result = collectAppMetrics();
    expect(result).toEqual({ processCount: 1, totalWorkingSetKb: 716800 });
    expect(Object.keys(result).sort()).toEqual(["processCount", "totalWorkingSetKb"]);
  });
});
