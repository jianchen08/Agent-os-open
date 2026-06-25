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
} satisfies ElectronAPI);
