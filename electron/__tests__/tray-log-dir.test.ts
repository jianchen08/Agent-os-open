// @ci: frontend-test
/**
 * 托盘「打开日志目录」测试（P-22 日志可达性）。
 *
 * 行为契约：
 * - 菜单含「打开日志目录」项，点击 → shell.openPath 打开日志目录；
 * - 落点解析（resolveKernelLogDirForTray）：受管内核日志真身优先（壳拉起过
 *   内核）；壳未拉起（dev 外部内核）回退同一资源解析（appPath 侧 kernel/logs）；
 * - openPath 失败（返回错误串）→ showErrorBox 显式报错，不静默。
 *
 * Electron 运行时是本测试唯一 mock 的外部边界（与 edit-menu.test.ts 同口径）。
 */

import { beforeEach, describe, expect, it, vi } from "vitest";

const electronMock = vi.hoisted(() => ({
  openPath: vi.fn(async () => ""),
  showErrorBox: vi.fn(),
  builtMenus: [] as Array<{ template: Array<Record<string, unknown>> }>,
  Tray: class {
    on = vi.fn();
    setToolTip = vi.fn();
    setContextMenu = vi.fn();
    destroy = vi.fn();
    resize = vi.fn(() => ({ width: 16 }));
  },
  nativeImage: {
    createFromPath: vi.fn(() => ({ isEmpty: () => false, resize: () => ({}) })),
    createFromBuffer: vi.fn(() => ({ resize: () => ({}) })),
  },
  app: {
    getAppPath: vi.fn(() => "/repo"),
    quit: vi.fn(),
  },
  Menu: {
    buildFromTemplate: vi.fn((template: Array<Record<string, unknown>>) => {
      electronMock.builtMenus.push({ template });
      return { template };
    }),
  },
  BrowserWindow: class {},
}));

vi.mock("electron", () => ({
  Tray: electronMock.Tray,
  Menu: electronMock.Menu,
  nativeImage: electronMock.nativeImage,
  app: electronMock.app,
  shell: { openPath: electronMock.openPath },
  dialog: { showErrorBox: electronMock.showErrorBox },
}));

const { createTray } = await import("../tray");
const { resolveKernelLogDirForTray } = await import("../kernel-manager");
import * as path from "path";

/** 从托盘菜单取「打开日志目录」项 */
function logDirItem(): { label: unknown; click: () => void } {
  const menu = electronMock.builtMenus.at(-1);
  expect(menu).toBeDefined();
  const item = menu!.template.find((entry) => entry.label === "打开日志目录");
  expect(item).toBeDefined();
  return item as { label: unknown; click: () => void };
}

/** 假窗口（tray 构造只存引用，不触碰 webContents） */
function fakeWindow(): never {
  return {} as never;
}

beforeEach(() => {
  vi.clearAllMocks();
  electronMock.builtMenus.length = 0;
  electronMock.openPath.mockResolvedValue("");
});

describe("resolveKernelLogDirForTray 落点解析", () => {
  it("受管内核日志真身优先（壳拉起过内核）", () => {
    expect(resolveKernelLogDirForTray("/managed/logs", "/repo")).toBe("/managed/logs");
  });

  it("壳未拉起内核（dev）→ 回退同一资源解析：appPath 侧 kernel/logs", () => {
    expect(resolveKernelLogDirForTray(null, "/repo")).toBe(
      path.join("/repo", "kernel", "logs"),
    );
  });
});

describe("托盘菜单「打开日志目录」", () => {
  it("菜单含该项；点击 → shell.openPath 打开解析出的目录", async () => {
    createTray(fakeWindow());
    const item = logDirItem();
    await item.click();

    expect(electronMock.openPath).toHaveBeenCalledTimes(1);
    // dev 形态（managedKernelLogDir 恒 null）→ appPath 侧解析
    expect(electronMock.openPath).toHaveBeenCalledWith(path.join("/repo", "kernel", "logs"));
  });

  it("openPath 失败 → showErrorBox 显式报错（含目录与原因），不静默", async () => {
    createTray(fakeWindow());
    electronMock.openPath.mockResolvedValue("目录不存在");
    await logDirItem().click();

    expect(electronMock.showErrorBox).toHaveBeenCalledTimes(1);
    const [title, detail] = electronMock.showErrorBox.mock.calls[0]!;
    expect(title).toBe("无法打开日志目录");
    expect(String(detail)).toContain(path.join("/repo", "kernel", "logs"));
    expect(String(detail)).toContain("目录不存在");
  });

  it("openPath 成功（空串）→ 不弹错误框", async () => {
    createTray(fakeWindow());
    await logDirItem().click();
    expect(electronMock.showErrorBox).not.toHaveBeenCalled();
  });
});
