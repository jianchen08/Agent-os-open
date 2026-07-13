/**
 * tray.ts 单元测试。
 *
 * 覆盖系统托盘的创建、菜单项点击和销毁逻辑。
 */

// Mock electron 模块
const mockTrayInstance = {
  setToolTip: jest.fn(),
  on: jest.fn(),
  setContextMenu: jest.fn(),
  destroy: jest.fn(),
};

const mockNativeImage = {
  resize: jest.fn().mockReturnValue("mock-image"),
};

const mockMenuBuildFromTemplate = jest.fn().mockReturnValue("mock-menu");

jest.mock("electron", () => ({
  Tray: jest.fn().mockImplementation(() => mockTrayInstance),
  Menu: {
    buildFromTemplate: (...args: unknown[]) => mockMenuBuildFromTemplate(...args),
  },
  nativeImage: {
    createFromPath: jest.fn().mockReturnValue(mockNativeImage),
    createFromBuffer: jest.fn().mockReturnValue(mockNativeImage),
  },
  app: {
    getAppPath: jest.fn().mockReturnValue("/mock/app/path"),
    quit: jest.fn(),
  },
  BrowserWindow: jest.fn(),
}));

jest.mock("fs", () => ({
  existsSync: jest.fn().mockReturnValue(false),
}));

import { Tray, Menu, app } from "electron";
import { createTray, destroyTray } from "../../electron/tray";

describe("Tray", () => {
  const mockMainWindow = {
    show: jest.fn(),
    hide: jest.fn(),
    focus: jest.fn(),
    isVisible: jest.fn().mockReturnValue(true),
    isDestroyed: jest.fn().mockReturnValue(false),
  } as unknown as Electron.BrowserWindow;

  beforeEach(() => {
    jest.clearAllMocks();
  });

  describe("createTray", () => {
    it("应创建 Tray 实例并设置工具提示", () => {
      const tray = createTray(mockMainWindow);

      expect(Tray).toHaveBeenCalled();
      expect(mockTrayInstance.setToolTip).toHaveBeenCalledWith("灵汐助手");
      expect(tray).toBeDefined();
    });

    it("应设置右键菜单（显示窗口、隐藏窗口、退出应用）", () => {
      createTray(mockMainWindow);

      expect(mockMenuBuildFromTemplate).toHaveBeenCalledTimes(1);

      const menuTemplate = mockMenuBuildFromTemplate.mock.calls[0][0] as Array<{
        label: string;
        type: string;
        click?: () => void;
      }>;

      const labels = menuTemplate
        .filter((item) => item.type === "normal")
        .map((item) => item.label);

      expect(labels).toContain("显示窗口");
      expect(labels).toContain("隐藏窗口");
      expect(labels).toContain("退出应用");
    });

    it("右键菜单应包含分隔符", () => {
      createTray(mockMainWindow);

      const menuTemplate = mockMenuBuildFromTemplate.mock.calls[0][0] as Array<{
        type: string;
      }>;

      const hasSeparator = menuTemplate.some((item) => item.type === "separator");
      expect(hasSeparator).toBe(true);
    });

    it("点击「显示窗口」应调用 show 和 focus", () => {
      createTray(mockMainWindow);

      const menuTemplate = mockMenuBuildFromTemplate.mock.calls[0][0] as Array<{
        label: string;
        click?: () => void;
      }>;

      const showItem = menuTemplate.find(
        (item) => item.label === "显示窗口"
      );
      showItem?.click?.();

      expect(mockMainWindow.show).toHaveBeenCalled();
      expect(mockMainWindow.focus).toHaveBeenCalled();
    });

    it("点击「隐藏窗口」应调用 hide", () => {
      createTray(mockMainWindow);

      const menuTemplate = mockMenuBuildFromTemplate.mock.calls[0][0] as Array<{
        label: string;
        click?: () => void;
      }>;

      const hideItem = menuTemplate.find(
        (item) => item.label === "隐藏窗口"
      );
      hideItem?.click?.();

      expect(mockMainWindow.hide).toHaveBeenCalled();
    });

    it("点击「退出应用」应调用 app.quit", () => {
      createTray(mockMainWindow);

      const menuTemplate = mockMenuBuildFromTemplate.mock.calls[0][0] as Array<{
        label: string;
        click?: () => void;
      }>;

      const quitItem = menuTemplate.find(
        (item) => item.label === "退出应用"
      );
      quitItem?.click?.();

      expect(app.quit).toHaveBeenCalled();
    });

    it("应注册点击事件用于切换窗口", () => {
      createTray(mockMainWindow);

      expect(mockTrayInstance.on).toHaveBeenCalledWith(
        "click",
        expect.any(Function)
      );
    });

    it("点击托盘图标：窗口可见时隐藏", () => {
      createTray(mockMainWindow);

      const clickHandler = mockTrayInstance.on.mock.calls.find(
        (call: unknown[]) => (call as [string, () => void])[0] === "click"
      )?.[1] as (() => void) | undefined;

      (mockMainWindow.isVisible as jest.Mock).mockReturnValue(true);
      clickHandler?.();

      expect(mockMainWindow.hide).toHaveBeenCalled();
    });

    it("点击托盘图标：窗口隐藏时显示并聚焦", () => {
      createTray(mockMainWindow);

      const clickHandler = mockTrayInstance.on.mock.calls.find(
        (call: unknown[]) => (call as [string, () => void])[0] === "click"
      )?.[1] as (() => void) | undefined;

      (mockMainWindow.isVisible as jest.Mock).mockReturnValue(false);
      clickHandler?.();

      expect(mockMainWindow.show).toHaveBeenCalled();
      expect(mockMainWindow.focus).toHaveBeenCalled();
    });

    it("窗口已销毁时点击菜单项不应崩溃", () => {
      const destroyedWindow = {
        show: jest.fn(),
        hide: jest.fn(),
        focus: jest.fn(),
        isVisible: jest.fn(),
        isDestroyed: jest.fn().mockReturnValue(true),
      } as unknown as Electron.BrowserWindow;

      createTray(destroyedWindow);

      const menuTemplate = mockMenuBuildFromTemplate.mock.calls[0][0] as Array<{
        label: string;
        click?: () => void;
      }>;

      const showItem = menuTemplate.find(
        (item) => item.label === "显示窗口"
      );
      expect(() => showItem?.click?.()).not.toThrow();
      expect(destroyedWindow.show).not.toHaveBeenCalled();
    });

    it("窗口已销毁时点击「隐藏窗口」不应崩溃", () => {
      const destroyedWindow = {
        show: jest.fn(),
        hide: jest.fn(),
        focus: jest.fn(),
        isVisible: jest.fn(),
        isDestroyed: jest.fn().mockReturnValue(true),
      } as unknown as Electron.BrowserWindow;

      createTray(destroyedWindow);

      const menuTemplate = mockMenuBuildFromTemplate.mock.calls[0][0] as Array<{
        label: string;
        click?: () => void;
      }>;

      const hideItem = menuTemplate.find(
        (item) => item.label === "隐藏窗口"
      );
      expect(() => hideItem?.click?.()).not.toThrow();
      expect(destroyedWindow.hide).not.toHaveBeenCalled();
    });

    it("窗口已销毁时点击托盘图标不应崩溃", () => {
      const destroyedWindow = {
        show: jest.fn(),
        hide: jest.fn(),
        focus: jest.fn(),
        isVisible: jest.fn(),
        isDestroyed: jest.fn().mockReturnValue(true),
      } as unknown as Electron.BrowserWindow;

      createTray(destroyedWindow);

      const clickHandler = mockTrayInstance.on.mock.calls.find(
        (call: unknown[]) => (call as [string, () => void])[0] === "click"
      )?.[1] as (() => void) | undefined;

      expect(() => clickHandler?.()).not.toThrow();
    });

    it("图标无文件时应创建占位图标并 resize 为 16x16", () => {
      createTray(mockMainWindow);

      // fs.existsSync 返回 false，走 createFromBuffer 分支
      const { nativeImage } = jest.requireMock("electron");
      expect(nativeImage.createFromBuffer).toHaveBeenCalled();
      expect(mockNativeImage.resize).toHaveBeenCalledWith({ width: 16, height: 16 });
    });
  });

  describe("destroyTray", () => {
    it("应销毁 Tray 实例", () => {
      createTray(mockMainWindow);
      destroyTray();

      expect(mockTrayInstance.destroy).toHaveBeenCalled();
    });

    it("重复调用不应崩溃", () => {
      createTray(mockMainWindow);
      destroyTray();
      expect(() => destroyTray()).not.toThrow();
    });
  });
});
