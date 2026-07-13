/**
 * window-info.ts 单元测试。
 *
 * 覆盖窗口信息采集、轮询器启停、跨平台分发等核心逻辑。
 */

import { platform } from "os";
import { execFile } from "child_process";

// Mock child_process 和 os
jest.mock("child_process");
jest.mock("os");
jest.mock("electron", () => ({
  BrowserWindow: jest.fn().mockImplementation(() => ({
    isDestroyed: jest.fn().mockReturnValue(false),
    webContents: {
      send: jest.fn(),
    },
  })),
}));

import { BrowserWindow } from "electron";
import {
  getActiveWindowInfo,
  WindowInfoPoller,
  startWindowInfoPolling,
  stopWindowInfoPolling,
} from "../../electron/window-info";

const mockedExecFile = execFile as jest.MockedFunction<typeof execFile>;
const mockedPlatform = platform as jest.MockedFunction<typeof platform>;

describe("WindowInfo", () => {
  beforeEach(() => {
    jest.clearAllMocks();
    jest.useFakeTimers();
  });

  afterEach(() => {
    jest.useRealTimers();
  });

  // ========== getActiveWindowInfo ==========

  describe("getActiveWindowInfo", () => {
    it("Windows 平台：应解析 PowerShell 输出为 WindowInfo", async () => {
      mockedPlatform.mockReturnValue("win32");
      mockedExecFile.mockImplementation(
        (_cmd: string, _args: string[], _opts: unknown, cb: unknown) => {
          const callback = cb as (
            err: null,
            stdout: string
          ) => void;
          callback(null, "VS Code|Code|100|200|800|600\n");
          return {} as never;
        }
      );

      const info = await getActiveWindowInfo();
      expect(info.title).toBe("VS Code");
      expect(info.processName).toBe("Code");
      expect(info.x).toBe(100);
      expect(info.y).toBe(200);
      expect(info.width).toBe(800);
      expect(info.height).toBe(600);
    });

    it("Windows 平台：PowerShell 返回空结果时返回空 WindowInfo", async () => {
      mockedPlatform.mockReturnValue("win32");
      mockedExecFile.mockImplementation(
        (_cmd: string, _args: string[], _opts: unknown, cb: unknown) => {
          const callback = cb as (
            err: null,
            stdout: string
          ) => void;
          callback(null, "");
          return {} as never;
        }
      );

      const info = await getActiveWindowInfo();
      expect(info.title).toBe("");
      expect(info.processName).toBe("");
      expect(info.x).toBe(0);
      expect(info.y).toBe(0);
    });

    it("Windows 平台：PowerShell 执行失败时返回空 WindowInfo", async () => {
      mockedPlatform.mockReturnValue("win32");
      mockedExecFile.mockImplementation(
        (_cmd: string, _args: string[], _opts: unknown, cb: unknown) => {
          const callback = cb as (
            err: Error,
            stdout: string
          ) => void;
          callback(new Error("timeout"), "");
          return {} as never;
        }
      );

      const info = await getActiveWindowInfo();
      expect(info.title).toBe("");
      expect(info.processName).toBe("");
    });

    it("Linux 平台：应解析 xdotool 输出为 WindowInfo", async () => {
      mockedPlatform.mockReturnValue("linux");
      const calls: { cmd: string; args: string[]; output: string }[] = [
        { cmd: "xdotool", args: ["getactivewindow"], output: "12345" },
        {
          cmd: "xdotool",
          args: ["getwindowname", "12345"],
          output: "Terminal",
        },
        { cmd: "xdotool", args: ["getwindowpid", "12345"], output: "999" },
        {
          cmd: "ps",
          args: ["-p", "999", "-o", "comm="],
          output: "gnome-terminal",
        },
        {
          cmd: "xdotool",
          args: ["getwindowgeometry", "--shell", "12345"],
          output: "X=50\nY=100\nWIDTH=800\nHEIGHT=600\n",
        },
      ];

      let callIndex = 0;
      mockedExecFile.mockImplementation(
        (cmd: string, args: string[], _opts: unknown, cb: unknown) => {
          const callback = cb as (
            err: null,
            stdout: string
          ) => void;
          const match = calls[callIndex];
          callIndex++;
          if (match && cmd === match.cmd) {
            callback(null, match.output);
          } else {
            callback(null, "");
          }
          return {} as never;
        }
      );

      const info = await getActiveWindowInfo();
      expect(info.title).toBe("Terminal");
      expect(info.processName).toBe("gnome-terminal");
      expect(info.x).toBe(50);
      expect(info.y).toBe(100);
      expect(info.width).toBe(800);
      expect(info.height).toBe(600);
    });

    it("Linux 平台：xdotool 未安装时返回空 WindowInfo", async () => {
      mockedPlatform.mockReturnValue("linux");
      mockedExecFile.mockImplementation(
        (_cmd: string, _args: string[], _opts: unknown, cb: unknown) => {
          const callback = cb as (
            err: Error,
            stdout: string
          ) => void;
          callback(new Error("ENOENT"), "");
          return {} as never;
        }
      );

      const info = await getActiveWindowInfo();
      expect(info.title).toBe("");
    });

    it("macOS 平台：应解析 osascript 输出为 WindowInfo", async () => {
      mockedPlatform.mockReturnValue("darwin");
      const calls = [
        { output: "Safari, Safari" },
        { output: "100, 200, 800, 600" },
      ];
      let callIndex = 0;

      mockedExecFile.mockImplementation(
        (_cmd: string, _args: string[], _opts: unknown, cb: unknown) => {
          const callback = cb as (
            err: null,
            stdout: string
          ) => void;
          const call = calls[callIndex++];
          callback(null, call?.output ?? "");
          return {} as never;
        }
      );

      const info = await getActiveWindowInfo();
      expect(info.title).toBe("Safari");
      expect(info.processName).toBe("Safari");
      expect(info.x).toBe(100);
      expect(info.y).toBe(200);
      expect(info.width).toBe(800);
      expect(info.height).toBe(600);
    });

    it("Windows 平台：PowerShell 输出部分字段（少于 6 个）时返回空 WindowInfo", async () => {
      mockedPlatform.mockReturnValue("win32");
      mockedExecFile.mockImplementation(
        (_cmd: string, _args: string[], _opts: unknown, cb: unknown) => {
          const callback = cb as (
            err: null,
            stdout: string
          ) => void;
          callback(null, "Title|Proc|100"); // 只有 3 个字段
          return {} as never;
        }
      );

      const info = await getActiveWindowInfo();
      expect(info.title).toBe("");
      expect(info.processName).toBe("");
    });

    it("不支持的平台应返回空 WindowInfo", async () => {
      mockedPlatform.mockReturnValue("freebsd" as NodeJS.Platform);
      const info = await getActiveWindowInfo();
      expect(info.title).toBe("");
      expect(info.processName).toBe("");
    });
  });

  // ========== WindowInfoPoller ==========

  describe("WindowInfoPoller", () => {
    it("启动后应定期调用回调", async () => {
      const callback = jest.fn();
      const poller = new WindowInfoPoller(callback, 100);

      mockedPlatform.mockReturnValue("win32");
      mockedExecFile.mockImplementation(
        (_cmd: string, _args: string[], _opts: unknown, cb: unknown) => {
          const fn = cb as (err: null, stdout: string) => void;
          fn(null, "Title|Proc|0|0|100|100");
          return {} as never;
        }
      );

      poller.start();
      expect(poller.isRunning).toBe(true);

      // 等待第一次异步轮询完成
      await jest.advanceTimersByTimeAsync(10);

      expect(callback).toHaveBeenCalledTimes(1);
      expect(callback).toHaveBeenCalledWith(
        expect.objectContaining({ title: "Title", processName: "Proc" })
      );

      // 推进定时器触发第二次轮询
      await jest.advanceTimersByTimeAsync(100);
      expect(callback).toHaveBeenCalledTimes(2);

      poller.stop();
    });

    it("停止后不应再调用回调", async () => {
      const callback = jest.fn();
      const poller = new WindowInfoPoller(callback, 100);

      mockedPlatform.mockReturnValue("win32");
      mockedExecFile.mockImplementation(
        (_cmd: string, _args: string[], _opts: unknown, cb: unknown) => {
          const fn = cb as (err: null, stdout: string) => void;
          fn(null, "T|P|0|0|0|0");
          return {} as never;
        }
      );

      poller.start();
      await jest.advanceTimersByTimeAsync(10);
      expect(callback).toHaveBeenCalledTimes(1);

      poller.stop();
      expect(poller.isRunning).toBe(false);

      await jest.advanceTimersByTimeAsync(200);
      // 停止后不应再有新的调用
      expect(callback).toHaveBeenCalledTimes(1);
    });

    it("重复启动应忽略并输出警告", () => {
      const callback = jest.fn();
      const poller = new WindowInfoPoller(callback, 100);

      mockedPlatform.mockReturnValue("win32");
      mockedExecFile.mockImplementation(
        (_cmd: string, _args: string[], _opts: unknown, cb: unknown) => {
          const fn = cb as (err: null, stdout: string) => void;
          fn(null, "");
          return {} as never;
        }
      );

      const warnSpy = jest.spyOn(console, "warn").mockImplementation();

      poller.start();
      poller.start(); // 重复启动

      expect(warnSpy).toHaveBeenCalledWith(
        "轮询器已在运行，忽略重复启动"
      );

      poller.stop();
      warnSpy.mockRestore();
    });

    it("采集异常时不应崩溃，轮询器继续运行", async () => {
      const callback = jest.fn();
      const poller = new WindowInfoPoller(callback, 100);

      mockedPlatform.mockReturnValue("win32");
      // execFile 返回错误，getActiveWindowInfo 内部 catch 后返回空 WindowInfo
      // poller._poll 仍会调用 callback（传入空 WindowInfo）
      mockedExecFile.mockImplementation(
        (_cmd: string, _args: string[], _opts: unknown, cb: unknown) => {
          const fn = cb as (err: Error, stdout: string) => void;
          fn(new Error("unexpected"), "");
          return {} as never;
        }
      );

      const warnSpy = jest.spyOn(console, "warn").mockImplementation();

      poller.start();
      await jest.advanceTimersByTimeAsync(10);

      // 回调被调用（传入空 WindowInfo），轮询器仍应运行
      expect(callback).toHaveBeenCalledWith(
        expect.objectContaining({ title: "", processName: "" })
      );
      expect(poller.isRunning).toBe(true);

      poller.stop();
      warnSpy.mockRestore();
    });
  });

  // ========== 便捷函数 ==========

  describe("startWindowInfoPolling / stopWindowInfoPolling", () => {
    it("应创建轮询器并通过 BrowserWindow 发送信息", async () => {
      const mockWebContents = { send: jest.fn() };
      const mockWindow = {
        isDestroyed: jest.fn().mockReturnValue(false),
        webContents: mockWebContents,
      } as unknown as BrowserWindow;

      mockedPlatform.mockReturnValue("win32");
      mockedExecFile.mockImplementation(
        (_cmd: string, _args: string[], _opts: unknown, cb: unknown) => {
          const fn = cb as (err: null, stdout: string) => void;
          fn(null, "Chrome|chrome|10|20|1920|1080");
          return {} as never;
        }
      );

      const poller = startWindowInfoPolling(mockWindow, 50);

      await jest.advanceTimersByTimeAsync(10);

      expect(mockWebContents.send).toHaveBeenCalledWith(
        "window-info",
        expect.objectContaining({
          title: "Chrome",
          processName: "chrome",
        })
      );

      stopWindowInfoPolling(poller);
      expect(poller.isRunning).toBe(false);
    });

    it("窗口已销毁时不应发送信息", async () => {
      const mockWebContents = { send: jest.fn() };
      const mockWindow = {
        isDestroyed: jest.fn().mockReturnValue(true),
        webContents: mockWebContents,
      } as unknown as BrowserWindow;

      mockedPlatform.mockReturnValue("win32");
      mockedExecFile.mockImplementation(
        (_cmd: string, _args: string[], _opts: unknown, cb: unknown) => {
          const fn = cb as (err: null, stdout: string) => void;
          fn(null, "T|P|0|0|0|0");
          return {} as never;
        }
      );

      const poller = startWindowInfoPolling(mockWindow, 50);
      await jest.advanceTimersByTimeAsync(10);

      expect(mockWebContents.send).not.toHaveBeenCalled();

      stopWindowInfoPolling(poller);
    });
  });

  // ========== WindowInfo 数据结构验证 ==========

  describe("WindowInfo 数据结构", () => {
    it("空 WindowInfo 应包含所有必要字段", async () => {
      mockedPlatform.mockReturnValue("freebsd" as NodeJS.Platform);
      const info = await getActiveWindowInfo();

      expect(info).toHaveProperty("title");
      expect(info).toHaveProperty("processName");
      expect(info).toHaveProperty("x");
      expect(info).toHaveProperty("y");
      expect(info).toHaveProperty("width");
      expect(info).toHaveProperty("height");
    });

    it("Windows 解析结果应包含正确的数值类型", async () => {
      mockedPlatform.mockReturnValue("win32");
      mockedExecFile.mockImplementation(
        (_cmd: string, _args: string[], _opts: unknown, cb: unknown) => {
          const fn = cb as (err: null, stdout: string) => void;
          fn(null, "Title|Proc|10|20|300|400");
          return {} as never;
        }
      );

      const info = await getActiveWindowInfo();
      expect(typeof info.title).toBe("string");
      expect(typeof info.processName).toBe("string");
      expect(typeof info.x).toBe("number");
      expect(typeof info.y).toBe("number");
      expect(typeof info.width).toBe("number");
      expect(typeof info.height).toBe("number");
    });
  });

  // ========== macOS 边界场景 ==========

  describe("macOS 边界场景", () => {
    it("macOS：osascript 返回空结果时返回空 WindowInfo", async () => {
      mockedPlatform.mockReturnValue("darwin");
      mockedExecFile.mockImplementation(
        (_cmd: string, _args: string[], _opts: unknown, cb: unknown) => {
          const fn = cb as (err: Error, stdout: string) => void;
          fn(new Error("no window"), "");
          return {} as never;
        }
      );

      const info = await getActiveWindowInfo();
      expect(info.title).toBe("");
      expect(info.processName).toBe("");
    });

    it("macOS：bounds 输出不完整时默认为 0", async () => {
      mockedPlatform.mockReturnValue("darwin");
      const calls = [
        { output: "Test, TestProc" },
        { output: "100, 200" }, // 只有 2 个值，不够 4 个
      ];
      let callIndex = 0;

      mockedExecFile.mockImplementation(
        (_cmd: string, _args: string[], _opts: unknown, cb: unknown) => {
          const fn = cb as (err: null, stdout: string) => void;
          fn(null, calls[callIndex++]?.output ?? "");
          return {} as never;
        }
      );

      const info = await getActiveWindowInfo();
      expect(info.title).toBe("Test");
      expect(info.x).toBe(0);
      expect(info.y).toBe(0);
      expect(info.width).toBe(0);
      expect(info.height).toBe(0);
    });
  });

  // ========== Linux 边界场景 ==========

  describe("Linux 边界场景", () => {
    it("Linux：xdotool getactivewindow 返回空时返回空 WindowInfo", async () => {
      mockedPlatform.mockReturnValue("linux");
      mockedExecFile.mockImplementation(
        (_cmd: string, _args: string[], _opts: unknown, cb: unknown) => {
          const fn = cb as (err: Error, stdout: string) => void;
          fn(new Error("no active window"), "");
          return {} as never;
        }
      );

      const info = await getActiveWindowInfo();
      expect(info.title).toBe("");
    });

    it("Linux：getwindowpid 返回空时 processName 应为空", async () => {
      mockedPlatform.mockReturnValue("linux");
      const calls: { cmd: string; args: string[]; output: string; isError?: boolean }[] = [
        { cmd: "xdotool", args: ["getactivewindow"], output: "12345" },
        { cmd: "xdotool", args: ["getwindowname", "12345"], output: "Terminal" },
        { cmd: "xdotool", args: ["getwindowpid", "12345"], output: "", isError: true },
        { cmd: "xdotool", args: ["getwindowgeometry", "--shell", "12345"], output: "X=0\nY=0\nWIDTH=800\nHEIGHT=600\n" },
      ];

      let callIndex = 0;
      mockedExecFile.mockImplementation(
        (cmd: string, args: string[], _opts: unknown, cb: unknown) => {
          const fn = cb as (err: Error | null, stdout: string) => void;
          const match = calls[callIndex++];
          if (match?.isError) {
            fn(new Error("no pid"), "");
          } else if (match && cmd === match.cmd) {
            fn(null, match.output);
          } else {
            fn(null, "");
          }
          return {} as never;
        }
      );

      const info = await getActiveWindowInfo();
      expect(info.title).toBe("Terminal");
      expect(info.processName).toBe("");
      expect(info.width).toBe(800);
    });

    it("Linux：getwindowgeometry 输出不完整时缺失字段默认为 0", async () => {
      mockedPlatform.mockReturnValue("linux");
      const calls: { cmd: string; args: string[]; output: string }[] = [
        { cmd: "xdotool", args: ["getactivewindow"], output: "12345" },
        { cmd: "xdotool", args: ["getwindowname", "12345"], output: "Win" },
        { cmd: "xdotool", args: ["getwindowpid", "12345"], output: "999" },
        { cmd: "ps", args: ["-p", "999", "-o", "comm="], output: "test-proc" },
        { cmd: "xdotool", args: ["getwindowgeometry", "--shell", "12345"], output: "X=50\nY=100\n" },
      ];

      let callIndex = 0;
      mockedExecFile.mockImplementation(
        (cmd: string, args: string[], _opts: unknown, cb: unknown) => {
          const fn = cb as (err: null, stdout: string) => void;
          const match = calls[callIndex++];
          if (match && cmd === match.cmd) {
            fn(null, match.output);
          } else {
            fn(null, "");
          }
          return {} as never;
        }
      );

      const info = await getActiveWindowInfo();
      expect(info.x).toBe(50);
      expect(info.y).toBe(100);
      expect(info.width).toBe(0);
      expect(info.height).toBe(0);
    });
  });
});
