// @feature: FP-0.2.三 宿主接入 | @ci: frontend-test
/**
 * 应用壳进程树内存/CPU 链路测试（main.ts 的 app:metrics 支撑面）。
 *
 * 行为契约：
 *  - collectAppMetrics 汇总 getAppMetrics 全部进程的 workingSetSize（KB 求和）
 *    与进程数，供监控页全口径内存的应用壳段消费；
 *  - totalCpuPercent = 各进程 cpu.percentCPUUsage 求和（Electron CPUUsage
 *    口径：距上次查询的 CPU 百分比，首次查询为 0）；
 *  - 空进程树 → processCount 0 / totalWorkingSetKb 0 / totalCpuPercent 0
 *    （不虚构非零）；
 *  - 只取 workingSetSize 与 percentCPUUsage，进程对象其余字段（creationTime
 *    等）不进求和。
 *
 * Electron 运行时是本测试唯一 mock 的外部边界（与 dialog.test.ts 同口径）；
 * collectAppMetrics 为导出函数直接单测，不经 whenReady。
 */

import { beforeEach, describe, expect, it, vi } from "vitest";

const electronMock = vi.hoisted(() => {
  const state = {
    metrics: [] as Array<{
      memory: { workingSetSize: number };
      cpu?: { percentCPUUsage: number };
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
      { type: "browser", memory: { workingSetSize: 102400 }, cpu: { percentCPUUsage: 0.5 } },
      { type: "renderer", memory: { workingSetSize: 204800 }, cpu: { percentCPUUsage: 12.25 } },
      { type: "gpu-process", memory: { workingSetSize: 51200 }, cpu: { percentCPUUsage: 1.5 } },
    ];

    expect(collectAppMetrics()).toEqual({
      processCount: 3,
      totalWorkingSetKb: 102400 + 204800 + 51200,
      totalCpuPercent: 0.5 + 12.25 + 1.5,
    });
  });

  it("totalCpuPercent = 各进程 percentCPUUsage 求和（含 0 值进程不缺项）", () => {
    electronMock.state.metrics = [
      { memory: { workingSetSize: 1000 }, cpu: { percentCPUUsage: 0 } },
      { memory: { workingSetSize: 1000 }, cpu: { percentCPUUsage: 3.75 } },
      { memory: { workingSetSize: 1000 }, cpu: { percentCPUUsage: 0 } },
    ];
    const result = collectAppMetrics();
    expect(result.totalCpuPercent).toBe(3.75);
    expect(result.totalWorkingSetKb).toBe(3000);
  });

  it("空进程树 → processCount 0 / totalWorkingSetKb 0 / totalCpuPercent 0（不虚构非零）", () => {
    expect(collectAppMetrics()).toEqual({
      processCount: 0,
      totalWorkingSetKb: 0,
      totalCpuPercent: 0,
    });
  });

  it("单进程原样透出；字段名与 preload 契约一致（processCount/totalWorkingSetKb/totalCpuPercent）", () => {
    electronMock.state.metrics = [
      { memory: { workingSetSize: 716800 }, cpu: { percentCPUUsage: 2 } },
    ];
    const result = collectAppMetrics();
    expect(result).toEqual({ processCount: 1, totalWorkingSetKb: 716800, totalCpuPercent: 2 });
    expect(Object.keys(result).sort()).toEqual([
      "processCount",
      "totalCpuPercent",
      "totalWorkingSetKb",
    ]);
  });
});
