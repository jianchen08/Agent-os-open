// @ci: frontend-test
/**
 * 单实例锁守卫回归（BUG-29 / BUG-50）+ 打包件 userData 安装身份隔离（BUG-50）。
 *
 * 行为契约：
 *  - requestSingleInstanceLock 返回 false（锁被首实例持有）时，第二实例必须在
 *    模块加载期同步 **app.exit(0)** 自退——不会走到 whenReady，因而不创建
 *    窗口/托盘、不拉内核。必须 exit 而非 quit：实测（lockprobe，真机）
 *    ready 之前的 app.quit() 不阻断启动序列，whenReady 照常触发（BUG-50
 *    双开第二实例完整启动的直接根因）；
 *  - 返回 true（首实例）时不退出，且 second-instance 聚焦处理器保持注册
 *    （还原最小化→显示→聚焦）；
 *  - 打包件（app.isPackaged）必须在抢锁之前 app.setPath("userData", 安装身份
 *    目录)——单实例锁键派生自 userData 目录，setPath 即改锁身份；dev 不重定位。
 *
 * Electron 运行时是本测试唯一 mock 的外部边界（单测环境无真实 app 生命周期
 * 可观察）；whenReady 注入永不 resolve 的 promise，使「锁失败即 exit」与
 * 「exit 被误放进 ready 之后」两种实现可区分。
 */

import { beforeEach, describe, expect, it, vi } from "vitest";
import * as crypto from "crypto";
import * as path from "path";

const electronMock = vi.hoisted(() => {
  const state = { lockGranted: false, isPackaged: true };
  const app = {
    get isPackaged() {
      return state.isPackaged;
    },
    requestSingleInstanceLock: vi.fn(() => state.lockGranted),
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
    electronMock.state.lockGranted = false;
    electronMock.state.isPackaged = true;
  });

  it("锁被首实例持有（返回 false）→ 第二实例模块加载期即 app.exit(0) 自退", async () => {
    await import("../main");
    expect(electronMock.app.requestSingleInstanceLock).toHaveBeenCalledTimes(1);
    expect(electronMock.app.exit).toHaveBeenCalledTimes(1);
    expect(electronMock.app.exit).toHaveBeenCalledWith(0);
    expect(electronMock.app.quit).not.toHaveBeenCalled();
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

describe("打包件 userData 安装身份隔离（BUG-50）", () => {
  beforeEach(() => {
    vi.resetModules();
    vi.clearAllMocks();
    electronMock.state.lockGranted = true;
    electronMock.state.isPackaged = true;
  });

  it("打包件：userData 在抢锁之前重定位到安装身份目录", async () => {
    const mod = await import("../main");
    expect(electronMock.app.setPath).toHaveBeenCalledTimes(1);
    const [name, value] = electronMock.app.setPath.mock.calls[0] as [string, string];
    expect(name).toBe("userData");
    expect(value).toBe(
      mod.resolvePackagedUserData(
        electronMock.app.getPath("exe"),
        electronMock.app.getPath("appData"),
        electronMock.app.getName(),
      ),
    );
    // 时序不变量：先 setPath（改锁身份）后抢锁
    expect(electronMock.app.setPath.mock.invocationCallOrder[0]).toBeLessThan(
      electronMock.app.requestSingleInstanceLock.mock.invocationCallOrder[0],
    );
  });

  it("dev（非打包）：不重定位 userData（沿用默认路径）", async () => {
    electronMock.state.isPackaged = false;
    await import("../main");
    expect(electronMock.app.setPath).not.toHaveBeenCalled();
  });
});

describe("resolvePackagedUserData（纯函数性质）", () => {
  const appData = "C:\\Users\\t\\AppData\\Roaming";
  const exeOf = (dir: string): string => path.join(dir, "灵汐助手.exe");
  const userData = async (
    exeDir: string,
    appName = "agent-os",
  ): Promise<string> => {
    const { resolvePackagedUserData } = await import("../main");
    return resolvePackagedUserData(exeOf(exeDir), appData, appName);
  };

  it("安装身份目录落在 %APPDATA%\\<appName>\\userdata\\ 下，末段为 12 位十六进制", async () => {
    const dir = await userData("C:\\Inst\\App");
    const relative = path.relative(appData, dir);
    const segments = relative.split(path.sep);
    expect(segments).toEqual(["agent-os", "userdata", expect.any(String)]);
    expect(segments[2]).toMatch(/^[0-9a-f]{12}$/);
  });

  it("安装身份 = 安装根（小写归一）的摘要：同路径恒等，盘符/目录大小写漂移不影响", async () => {
    const a = await userData("C:\\Inst\\App");
    const b = await userData("c:\\inst\\app");
    expect(a).toBe(b);
    expect(path.basename(a)).toBe(
      crypto
        .createHash("sha256")
        .update("c:\\inst\\app")
        .digest("hex")
        .slice(0, 12),
    );
  });

  it("不同副本（装机目录/win-unpacked）互相隔离：身份目录不同", async () => {
    const installed = await userData("C:\\Inst\\App");
    const unpacked = await userData("D:\\rel\\win-unpacked");
    expect(installed).not.toBe(unpacked);
  });

  it("身份只随安装根变化：appName 只改变目录前缀不改末段", async () => {
    const installed = await userData("C:\\Inst\\App");
    const other = await userData("C:\\Inst\\App", "other-name");
    expect(path.dirname(installed)).not.toBe(path.dirname(other));
    expect(path.basename(installed)).toBe(path.basename(other));
  });
});
