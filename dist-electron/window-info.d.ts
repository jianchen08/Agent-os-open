/**
 * 窗口信息采集模块。
 *
 * 通过定时轮询采集当前前台窗口的标题、进程名、位置信息，
 * 并通过回调将窗口信息传递给调用方。
 *
 * 跨平台窗口信息采集依赖不同机制：
 * - Windows: 使用 PowerShell 命令查询
 * - macOS: 使用 AppleScript / osascript
 * - Linux: 使用 xdotool / wmctrl 等工具
 */
import { BrowserWindow } from "electron";
/** 活跃窗口信息数据结构 */
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
/**
 * 获取当前活跃窗口信息。
 *
 * 根据运行平台自动选择采集方式。
 *
 * @returns WindowInfo 对象，包含窗口标题、进程名和位置信息
 */
export declare function getActiveWindowInfo(): Promise<WindowInfo>;
/** 轮询器状态 */
export declare class WindowInfoPoller {
    private _intervalMs;
    private _callback;
    private _timer;
    private _running;
    /**
     * @param callback - 每次轮询后调用的回调函数
     * @param intervalMs - 轮询间隔毫秒数，默认 1000ms
     */
    constructor(callback: (info: WindowInfo) => void, intervalMs?: number);
    /** 轮询器是否正在运行 */
    get isRunning(): boolean;
    /** 启动窗口信息轮询 */
    start(): void;
    /** 停止窗口信息轮询 */
    stop(): void;
    /** 执行一次轮询 */
    private _poll;
}
/**
 * 启动窗口信息轮询的便捷函数。
 *
 * @param mainWindow - 要发送窗口信息的 BrowserWindow
 * @param intervalMs - 轮询间隔毫秒数
 * @returns WindowInfoPoller 实例，可用于停止轮询
 */
export declare function startWindowInfoPolling(mainWindow: BrowserWindow, intervalMs?: number): WindowInfoPoller;
/**
 * 停止窗口信息轮询的便捷函数。
 *
 * @param poller - 要停止的轮询器实例
 */
export declare function stopWindowInfoPolling(poller: WindowInfoPoller): void;
//# sourceMappingURL=window-info.d.ts.map