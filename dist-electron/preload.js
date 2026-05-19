"use strict";
/**
 * Electron 预加载脚本。
 *
 * 通过 contextBridge 安全地将 Electron API 暴露给渲染进程，
 * 提供窗口信息监听、应用版本查询等基础能力。
 */
Object.defineProperty(exports, "__esModule", { value: true });
const electron_1 = require("electron");
// 通过 contextBridge 安全暴露 API
electron_1.contextBridge.exposeInMainWorld("electronAPI", {
    /**
     * 监听来自主进程的窗口信息更新事件。
     */
    onWindowInfo: (callback) => {
        const handler = (_event, info) => {
            callback(info);
        };
        electron_1.ipcRenderer.on("window-info", handler);
        // 返回取消监听函数
        return () => {
            electron_1.ipcRenderer.removeListener("window-info", handler);
        };
    },
    /**
     * 获取应用版本号。
     */
    getAppVersion: () => {
        return electron_1.ipcRenderer.sendSync("get-app-version");
    },
    /**
     * 获取当前运行平台。
     */
    getPlatform: () => {
        return electron_1.ipcRenderer.sendSync("get-platform");
    },
    /**
     * 监听 IPC 通用事件的便捷方法。
     * 仅允许监听预定义的安全通道。
     */
    on: (channel, callback) => {
        // 白名单通道，防止渲染进程监听任意 IPC 事件
        const allowedChannels = new Set([
            "window-info",
            "app-version",
            "platform-info",
        ]);
        if (!allowedChannels.has(channel)) {
            console.warn(`IPC 通道 "${channel}" 不在白名单中，已忽略`);
            return () => { };
        }
        const handler = (_event, ...args) => {
            callback(...args);
        };
        electron_1.ipcRenderer.on(channel, handler);
        return () => {
            electron_1.ipcRenderer.removeListener(channel, handler);
        };
    },
});
//# sourceMappingURL=preload.js.map