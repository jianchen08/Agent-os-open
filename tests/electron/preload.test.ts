/**
 * preload.ts 单元测试。
 *
 * 验证预加载脚本通过 contextBridge 暴露的 API 接口正确性。
 * 注意：由于 preload 脚本在 Electron 环境中运行，这里主要测试
 * 导出的类型定义和逻辑约束。
 */

// Mock electron 的 contextBridge 和 ipcRenderer
const mockExposeInMainWorld = jest.fn();
const mockIpcRendererOn = jest.fn();
const mockIpcRendererRemoveListener = jest.fn();
const mockIpcRendererSendSync = jest.fn();

jest.mock("electron", () => ({
  contextBridge: {
    exposeInMainWorld: (...args: unknown[]) => mockExposeInMainWorld(...args),
  },
  ipcRenderer: {
    on: (...args: unknown[]) => mockIpcRendererOn(...args),
    removeListener: (...args: unknown[]) => mockIpcRendererRemoveListener(...args),
    sendSync: (...args: unknown[]) => mockIpcRendererSendSync(...args),
  },
}));

/** 从 exposeInMainWorld 调用中提取暴露的 API 对象 */
function getExposedApi(): Record<string, (...args: unknown[]) => unknown> {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const api = mockExposeInMainWorld.mock.calls[0][1] as any;
  return api as Record<string, (...args: unknown[]) => unknown>;
}

// 导入 preload 会执行 contextBridge.exposeInMainWorld
import "../../electron/preload";

describe("preload.ts", () => {
  beforeEach(() => {
    jest.clearAllMocks();
    // 重新加载模块以触发 exposeInMainWorld
    jest.resetModules();

    // 重新设置 mock
    jest.doMock("electron", () => ({
      contextBridge: {
        exposeInMainWorld: mockExposeInMainWorld,
      },
      ipcRenderer: {
        on: mockIpcRendererOn,
        removeListener: mockIpcRendererRemoveListener,
        sendSync: mockIpcRendererSendSync,
      },
    }));
  });

  describe("contextBridge.exposeInMainWorld", () => {
    it("应调用 exposeInMainWorld 暴露 electronAPI", () => {
      require("../../electron/preload");

      expect(mockExposeInMainWorld).toHaveBeenCalledWith(
        "electronAPI",
        expect.objectContaining({
          onWindowInfo: expect.any(Function),
          getAppVersion: expect.any(Function),
          getPlatform: expect.any(Function),
          on: expect.any(Function),
        })
      );
    });

    describe("onWindowInfo", () => {
      it("应注册 IPC window-info 事件监听器", () => {
        require("../../electron/preload");

        const api = getExposedApi();
        const callback = jest.fn();
        api.onWindowInfo(callback);

        expect(mockIpcRendererOn).toHaveBeenCalledWith(
          "window-info",
          expect.any(Function)
        );
      });

      it("返回的函数应能取消监听", () => {
        require("../../electron/preload");

        const api = getExposedApi();
        const unsubscribe = api.onWindowInfo(jest.fn()) as unknown as () => void;

        expect(typeof unsubscribe).toBe("function");
        unsubscribe();

        expect(mockIpcRendererRemoveListener).toHaveBeenCalledWith(
          "window-info",
          expect.any(Function)
        );
      });

      it("IPC 回调应将窗口信息传递给用户回调", () => {
        require("../../electron/preload");

        const api = getExposedApi();
        const userCallback = jest.fn();
        api.onWindowInfo(userCallback);

        // 获取注册到 ipcRenderer.on 的 handler
        const ipcHandler = mockIpcRendererOn.mock.calls[0][1] as (
          event: unknown,
          info: Record<string, unknown>
        ) => void;

        const mockInfo = {
          title: "Test",
          processName: "test-proc",
          x: 1,
          y: 2,
          width: 100,
          height: 200,
        };
        ipcHandler({}, mockInfo);

        expect(userCallback).toHaveBeenCalledWith(mockInfo);
      });
    });

    describe("getAppVersion", () => {
      it("应通过 sendSync 获取应用版本", () => {
        mockIpcRendererSendSync.mockReturnValue("1.0.0");
        require("../../electron/preload");

        const api = getExposedApi();
        const version = api.getAppVersion() as string;

        expect(mockIpcRendererSendSync).toHaveBeenCalledWith(
          "get-app-version"
        );
        expect(version).toBe("1.0.0");
      });
    });

    describe("getPlatform", () => {
      it("应通过 sendSync 获取平台信息", () => {
        mockIpcRendererSendSync.mockReturnValue("win32");
        require("../../electron/preload");

        const api = getExposedApi();
        const platformResult = api.getPlatform() as string;

        expect(mockIpcRendererSendSync).toHaveBeenCalledWith("get-platform");
        expect(platformResult).toBe("win32");
      });
    });

    describe("on", () => {
      it("白名单通道应注册监听", () => {
        require("../../electron/preload");

        const api = getExposedApi();
        api.on("window-info", jest.fn());

        expect(mockIpcRendererOn).toHaveBeenCalledWith(
          "window-info",
          expect.any(Function)
        );
      });

      it("app-version 白名单通道应注册监听", () => {
        require("../../electron/preload");

        const api = getExposedApi();
        const callback = jest.fn();
        api.on("app-version", callback);

        expect(mockIpcRendererOn).toHaveBeenCalledWith(
          "app-version",
          expect.any(Function)
        );
      });

      it("platform-info 白名单通道应注册监听", () => {
        require("../../electron/preload");

        const api = getExposedApi();
        const callback = jest.fn();
        api.on("platform-info", callback);

        expect(mockIpcRendererOn).toHaveBeenCalledWith(
          "platform-info",
          expect.any(Function)
        );
      });

      it("非白名单通道应忽略并输出警告", () => {
        const warnSpy = jest.spyOn(console, "warn").mockImplementation();
        require("../../electron/preload");

        const api = getExposedApi();
        const unsubscribe = api.on("dangerous-channel", jest.fn()) as unknown as () => void;

        expect(warnSpy).toHaveBeenCalledWith(
          expect.stringContaining("dangerous-channel")
        );
        expect(mockIpcRendererOn).not.toHaveBeenCalled();
        expect(typeof unsubscribe).toBe("function");

        warnSpy.mockRestore();
      });

      it("on 回调应将参数传递给用户回调", () => {
        require("../../electron/preload");

        const api = getExposedApi();
        const userCallback = jest.fn();
        api.on("window-info", userCallback);

        // 获取 ipcRenderer.on 注册的 handler
        const ipcHandler = mockIpcRendererOn.mock.calls[0][1] as (
          event: unknown,
          ...args: unknown[]
        ) => void;

        ipcHandler({}, "arg1", "arg2");
        expect(userCallback).toHaveBeenCalledWith("arg1", "arg2");
      });

      it("on 返回的取消函数应调用 removeListener", () => {
        require("../../electron/preload");

        const api = getExposedApi();
        const unsubscribe = api.on("window-info", jest.fn()) as unknown as () => void;
        unsubscribe();

        expect(mockIpcRendererRemoveListener).toHaveBeenCalledWith(
          "window-info",
          expect.any(Function)
        );
      });
    });
  });
});
