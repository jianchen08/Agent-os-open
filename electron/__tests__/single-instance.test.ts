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
import * as fs from "fs";
import * as os from "os";
import * as path from "path";
import { focusMainWindow } from "../main";

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
  safeStorage: undefined,
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

describe("focusMainWindow（second-instance 聚焦已有主窗口，BUG-86）", () => {
  /** 假主窗口：状态由参数决定，动作可观察 */
  const makeWin = (state: { destroyed?: boolean; minimized?: boolean; visible?: boolean }) => ({
    isDestroyed: () => state.destroyed ?? false,
    isMinimized: () => state.minimized ?? false,
    isVisible: () => state.visible ?? true,
    restore: vi.fn(),
    show: vi.fn(),
    focus: vi.fn(),
  });

  it("最小化窗口 → 还原→show→聚焦（show 无条件：Windows 前台锁下仅 focus() 置前被静默拒绝）", () => {
    const win = makeWin({ minimized: true, visible: true });
    focusMainWindow(win as never);
    expect(win.restore).toHaveBeenCalledTimes(1);
    expect(win.show).toHaveBeenCalledTimes(1);
    expect(win.focus).toHaveBeenCalledTimes(1);
    // 序列不变量：restore < focus
    expect(win.restore.mock.invocationCallOrder[0]).toBeLessThan(
      win.focus.mock.invocationCallOrder[0]!,
    );
  });

  it("隐藏窗口（收进托盘）→ show+focus，不做无谓 restore", () => {
    const win = makeWin({ visible: false });
    focusMainWindow(win as never);
    expect(win.restore).not.toHaveBeenCalled();
    expect(win.show).toHaveBeenCalledTimes(1);
    expect(win.focus).toHaveBeenCalledTimes(1);
  });

  it("后台可见窗口 → 也 show（借激活语义置前，show 对可见窗口无隐藏复显）+ focus", () => {
    const win = makeWin({});
    focusMainWindow(win as never);
    expect(win.restore).not.toHaveBeenCalled();
    expect(win.show).toHaveBeenCalledTimes(1);
    expect(win.focus).toHaveBeenCalledTimes(1);
  });

  it.each([
    ["窗口不存在（null）", null],
    ["窗口已销毁", makeWin({ destroyed: true })],
  ])("%s → 静默返回，零窗口调用", (_name, win) => {
    expect(() => focusMainWindow(win as never)).not.toThrow();
    if (win) {
      const w = win as ReturnType<typeof makeWin>;
      expect(w.restore).not.toHaveBeenCalled();
      expect(w.show).not.toHaveBeenCalled();
      expect(w.focus).not.toHaveBeenCalled();
    }
  });
});

describe("resolveAuthIdentityDir + migrateIdentityFile（认证身份与安装身份分家）", () => {
  it("身份目录=机器级稳定位：不同安装目录exe映射到同一身份目录", async () => {
    const { resolveAuthIdentityDir, resolvePackagedUserData } = await import("../main");
    const appData = path.join("C:", "ad");
    // 安装身份（Chromium profile）按安装目录哈希分家（BUG-50）
    expect(
      resolvePackagedUserData(path.join("C:", "inst", "a", "app.exe"), appData, "agent-os"),
    ).not.toBe(
      resolvePackagedUserData(path.join("C:", "inst", "b", "app.exe"), appData, "agent-os"),
    );
    // 认证身份（签名密钥/会话镜像）跨安装稳定：与安装目录无关
    expect(resolveAuthIdentityDir(appData, "agent-os")).toBe(
      path.join(appData, "agent-os", "identity"),
    );
  });

  it("迁移：稳定位缺失而旧 userData 有文件→种子拷贝；稳定位已有→不覆盖；旧位缺失→no-op", async () => {
    const { migrateIdentityFile } = await import("../main");
    const root = fs.mkdtempSync(path.join(os.tmpdir(), "auth-identity-"));
    try {
      const legacy = path.join(root, "legacy");
      const identity = path.join(root, "identity");
      fs.mkdirSync(legacy);
      fs.writeFileSync(path.join(legacy, "kernel-token-secret"), "legacy-secret-value-0123456789");

      migrateIdentityFile("kernel-token-secret", legacy, identity);
      expect(fs.readFileSync(path.join(identity, "kernel-token-secret"), "utf-8")).toBe(
        "legacy-secret-value-0123456789",
      );

      // 稳定位已存在（现役密钥）：旧文件轮换不覆盖——已发令牌的验证真值以稳定位为准
      fs.writeFileSync(path.join(legacy, "kernel-token-secret"), "rotated-legacy-0123456789");
      migrateIdentityFile("kernel-token-secret", legacy, identity);
      expect(fs.readFileSync(path.join(identity, "kernel-token-secret"), "utf-8")).toBe(
        "legacy-secret-value-0123456789",
      );

      // 旧位无文件：不建目录不落文件
      const identity2 = path.join(root, "identity2");
      migrateIdentityFile("kernel-token-secret", path.join(root, "no-such-legacy"), identity2);
      expect(fs.existsSync(identity2)).toBe(false);
    } finally {
      fs.rmSync(root, { recursive: true, force: true });
    }
  });

  it("迁移源候选：现役优先，其余哈希身份按目标文件 mtime 新→旧，非哈希目录排除", async () => {
    const { legacyIdentityCandidates } = await import("../main");
    const root = fs.mkdtempSync(path.join(os.tmpdir(), "identity-candidates-"));
    try {
      const userdata = path.join(root, "userdata");
      const own = path.join(userdata, "aaaaaaaaaaaa");
      const older = path.join(userdata, "bbbbbbbbbbbb");
      const newer = path.join(userdata, "cccccccccccc");
      const plain = path.join(userdata, "not-a-hash");
      for (const d of [own, older, newer, plain]) fs.mkdirSync(d, { recursive: true });
      const fileName = "kernel-token-secret";
      const write = (dir: string, when: Date) => {
        fs.writeFileSync(path.join(dir, fileName), "x");
        fs.utimesSync(path.join(dir, fileName), when, when);
      };
      write(older, new Date(1000));
      write(newer, new Date(2000));
      write(plain, new Date(3000)); // 非 12-hex 目录：永不入候选

      expect(legacyIdentityCandidates(own, fileName)).toEqual([
        own,
        path.join(newer, fileName),
        path.join(older, fileName),
      ]);
    } finally {
      fs.rmSync(root, { recursive: true, force: true });
    }
  });
});
