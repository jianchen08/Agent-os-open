/**
 * Electron 预加载脚本。
 *
 * 通过 contextBridge 安全地将 Electron API 暴露给渲染进程，
 * 提供窗口信息监听、应用版本查询等基础能力。
 */

import { contextBridge, ipcRenderer } from "electron";

/** 窗口信息数据结构，与 window-info.ts 中的 WindowInfo 对应 */
export interface WindowInfo {
  /** 窗口标题 */
  title: string;
  /** 进程名称 */
  processName: string;
  /** 窗口左上角 X 坐标 */
  x: number;
  /** 窗口左上角 Y 坐标 */
  y: number;
  /** 窗口宽度 */
  width: number;
  /** 窗口高度 */
  height: number;
}

/** 通过 contextBridge 暴露给渲染进程的 API 接口 */
export interface ElectronAPI {
  /**
   * 监听窗口信息更新。
   *
   * @param callback - 接收 WindowInfo 的回调函数
   * @returns 取消监听的函数
   */
  onWindowInfo(callback: (info: WindowInfo) => void): () => void;

  /**
   * 获取应用版本号。
   *
   * @returns 版本号字符串
   */
  getAppVersion(): string;

  /**
   * 获取当前运行平台。
   *
   * @returns 平台标识字符串（win32 / darwin / linux）
   */
  getPlatform(): string;

  /**
   * 监听 IPC 通用事件。
   *
   * @param channel - 事件通道名称
   * @param callback - 事件回调函数
   * @returns 取消监听的函数
   */
  on(channel: string, callback: (...args: unknown[]) => void): () => void;

  /**
   * 窗口管理子 API（P2/P3 多窗口基础设施）。
   *
   * 所有方法均通过 ipcRenderer.invoke 异步调用主进程的 window:* 通道，
   * 不经过 `on` 方法的白名单（白名单只作用于 ipcRenderer.on 监听通道）。
   */
  window: {
    /** 创建并返回子窗口；id 重复时聚焦已有窗口 */
    open(opts: ChildWindowOpenOptions): Promise<{ id: string; success: boolean }>;
    /** 关闭指定窗口并从注册表移除 */
    close(id: string): Promise<void>;
    /** 聚焦指定窗口 */
    focus(id: string): Promise<void>;
    /** 移动指定窗口 */
    move(id: string, pos: { x: number; y: number }): Promise<void>;
    /** 调整指定窗口大小 */
    resize(id: string, size: { width: number; height: number }): Promise<void>;
  };
}

/** window:open 的参数（与 main.ts 的 ChildWindowOptions 对齐） */
export interface ChildWindowOpenOptions {
  /** 窗口标识（前端传入，用于后续 close/focus） */
  id: string;
  /** 加载的 URL（如 'http://localhost:5188/#/p/my-page' 深链） */
  url: string;
  title?: string;
  width?: number;
  height?: number;
  x?: number;
  y?: number;
  frame?: boolean;
  transparent?: boolean;
  alwaysOnTop?: boolean;
  skipTaskbar?: boolean;
}

// 通过 contextBridge 安全暴露 API
contextBridge.exposeInMainWorld("electronAPI", {
  /**
   * 监听来自主进程的窗口信息更新事件。
   */
  onWindowInfo: (callback: (info: WindowInfo) => void): (() => void) => {
    const handler = (_event: Electron.IpcRendererEvent, info: WindowInfo): void => {
      callback(info);
    };
    ipcRenderer.on("window-info", handler);

    // 返回取消监听函数
    return () => {
      ipcRenderer.removeListener("window-info", handler);
    };
  },

  /**
   * 获取应用版本号。
   */
  getAppVersion: (): string => {
    return ipcRenderer.sendSync("get-app-version") as string;
  },

  /**
   * 获取当前运行平台。
   */
  getPlatform: (): string => {
    return ipcRenderer.sendSync("get-platform") as string;
  },

  /**
   * 监听 IPC 通用事件的便捷方法。
   * 仅允许监听预定义的安全通道。
   */
  on: (channel: string, callback: (...args: unknown[]) => void): (() => void) => {
    // 白名单通道，防止渲染进程监听任意 IPC 事件
    // 注意：window:open 等使用 ipcRenderer.invoke（见下方 window 子 API），
    // 不经此白名单；此白名单只约束 ipcRenderer.on 监听通道。
    const allowedChannels = new Set([
      "window-info",
      "app-version",
      "platform-info",
    ]);

    if (!allowedChannels.has(channel)) {
      console.warn(`IPC 通道 "${channel}" 不在白名单中，已忽略`);
      return () => {};
    }

    const handler = (_event: Electron.IpcRendererEvent, ...args: unknown[]): void => {
      callback(...args);
    };
    ipcRenderer.on(channel, handler);

    return () => {
      ipcRenderer.removeListener(channel, handler);
    };
  },

  /**
   * 窗口管理子 API（P2/P3 多窗口基础设施）。
   *
   * 所有方法封装 ipcRenderer.invoke('window:*')，主进程由 main.ts 的
   * ipcMain.handle 注册。invoke 绕过 on 的白名单（白名单只约束 on 监听），
   * 安全性由主进程的参数校验保证。
   */
  window: {
    open: (opts: ChildWindowOpenOptions) => {
      return ipcRenderer.invoke("window:open", opts) as Promise<{
        id: string;
        success: boolean;
      }>;
    },
    close: (id: string) => {
      return ipcRenderer.invoke("window:close", id) as Promise<void>;
    },
    focus: (id: string) => {
      return ipcRenderer.invoke("window:focus", id) as Promise<void>;
    },
    move: (id: string, pos: { x: number; y: number }) => {
      return ipcRenderer.invoke("window:move", id, pos) as Promise<void>;
    },
    resize: (id: string, size: { width: number; height: number }) => {
      return ipcRenderer.invoke("window:resize", id, size) as Promise<void>;
    },
  },
} satisfies ElectronAPI);
