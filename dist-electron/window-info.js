"use strict";
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
Object.defineProperty(exports, "__esModule", { value: true });
exports.WindowInfoPoller = void 0;
exports.getActiveWindowInfo = getActiveWindowInfo;
exports.startWindowInfoPolling = startWindowInfoPolling;
exports.stopWindowInfoPolling = stopWindowInfoPolling;
const child_process_1 = require("child_process");
const os_1 = require("os");
/** 轮询默认间隔（毫秒） */
const DEFAULT_POLLING_INTERVAL_MS = 1000;
/** 创建空的 WindowInfo 对象 */
function createEmptyWindowInfo() {
    return {
        title: "",
        processName: "",
        x: 0,
        y: 0,
        width: 0,
        height: 0,
    };
}
/**
 * 执行命令行并返回 stdout。
 *
 * @param command - 要执行的命令
 * @param args - 命令参数
 * @param timeout - 超时时间（毫秒）
 * @returns stdout 字符串，失败时返回 null
 */
function execCommand(command, args, timeout = 5000) {
    return new Promise((resolve) => {
        (0, child_process_1.execFile)(command, args, { timeout }, (error, stdout) => {
            if (error) {
                resolve(null);
                return;
            }
            resolve(stdout.trim());
        });
    });
}
/**
 * Windows 平台：通过 PowerShell 获取活跃窗口信息。
 */
async function getActiveWindowWindows() {
    const psScript = `
Add-Type @'
using System;
using System.Runtime.InteropServices;
using System.Text;
public class WinAPI {
  [DllImport("user32.dll")]
  public static extern IntPtr GetForegroundWindow();
  [DllImport("user32.dll", CharSet=CharSet.Auto)]
  public static extern int GetWindowText(IntPtr hWnd, StringBuilder lpString, int nMaxCount);
  [DllImport("user32.dll")]
  public static extern bool GetWindowRect(IntPtr hWnd, out RECT lpRect);
  [DllImport("user32.dll", SetLastError=true)]
  public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint lpdwProcessId);
  public struct RECT { public int Left, Top, Right, Bottom; }
}
'@
$hwnd = [WinAPI]::GetForegroundWindow()
if ($hwnd -ne [IntPtr]::Zero) {
  $sb = New-Object System.Text.StringBuilder 256
  [WinAPI]::GetWindowText($hwnd, $sb, 256) | Out-Null
  $title = $sb.ToString()
  $rect = New-Object WinAPI+RECT
  [WinAPI]::GetWindowRect($hwnd, [ref]$rect) | Out-Null
  $pid = 0
  [WinAPI]::GetWindowThreadProcessId($hwnd, [ref]$pid) | Out-Null
  $proc = try { (Get-Process -Id $pid -ErrorAction SilentlyContinue).ProcessName } catch { "" }
  "$title|$proc|$($rect.Left)|$($rect.Top)|$($rect.Right - $rect.Left)|$($rect.Bottom - $rect.Top)"
}`.trim();
    const output = await execCommand("powershell", [
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        psScript,
    ]);
    if (!output || !output.includes("|")) {
        return createEmptyWindowInfo();
    }
    const parts = output.split("|");
    if (parts.length >= 6) {
        return {
            title: parts[0],
            processName: parts[1],
            x: parseInt(parts[2], 10) || 0,
            y: parseInt(parts[3], 10) || 0,
            width: parseInt(parts[4], 10) || 0,
            height: parseInt(parts[5], 10) || 0,
        };
    }
    return createEmptyWindowInfo();
}
/**
 * macOS 平台：通过 AppleScript 获取活跃窗口信息。
 */
async function getActiveWindowMacOS() {
    // 获取窗口标题和进程名
    const scriptTitle = 'tell application "System Events" to get {name, name of process whose frontmost is true} of front window';
    const output = await execCommand("osascript", ["-e", scriptTitle]);
    let title = "";
    let processName = "";
    if (output) {
        const parts = output.split(", ");
        if (parts.length >= 2) {
            title = parts[0].trim();
            processName = parts[1].trim();
        }
    }
    // 获取窗口位置和大小
    const scriptBounds = 'tell application "System Events" to get {position, size} of front window';
    const boundsOutput = await execCommand("osascript", ["-e", scriptBounds]);
    let x = 0;
    let y = 0;
    let width = 0;
    let height = 0;
    if (boundsOutput) {
        const values = boundsOutput
            .replace(/[{}]/g, "")
            .split(", ")
            .map((v) => parseInt(v.trim(), 10) || 0);
        if (values.length >= 4) {
            [x, y, width, height] = values;
        }
    }
    return { title, processName, x, y, width, height };
}
/**
 * Linux 平台：通过 xdotool 获取活跃窗口信息。
 */
async function getActiveWindowLinux() {
    // 获取活跃窗口 ID
    const windowId = await execCommand("xdotool", ["getactivewindow"]);
    if (!windowId) {
        return createEmptyWindowInfo();
    }
    // 获取窗口标题
    const title = (await execCommand("xdotool", [
        "getwindowname",
        windowId,
    ])) || "";
    // 获取窗口 PID -> 进程名
    const pid = await execCommand("xdotool", ["getwindowpid", windowId]);
    let processName = "";
    if (pid) {
        const procOutput = await execCommand("ps", ["-p", pid, "-o", "comm="]);
        processName = procOutput || "";
    }
    // 获取窗口几何信息
    const geoOutput = await execCommand("xdotool", [
        "getwindowgeometry",
        "--shell",
        windowId,
    ]);
    const geo = {};
    if (geoOutput) {
        for (const line of geoOutput.split("\n")) {
            const eqIndex = line.indexOf("=");
            if (eqIndex !== -1) {
                const key = line.substring(0, eqIndex).trim();
                const value = parseInt(line.substring(eqIndex + 1).trim(), 10);
                if (!isNaN(value)) {
                    geo[key] = value;
                }
            }
        }
    }
    return {
        title,
        processName,
        x: geo["X"] ?? 0,
        y: geo["Y"] ?? 0,
        width: geo["WIDTH"] ?? 0,
        height: geo["HEIGHT"] ?? 0,
    };
}
/**
 * 获取当前活跃窗口信息。
 *
 * 根据运行平台自动选择采集方式。
 *
 * @returns WindowInfo 对象，包含窗口标题、进程名和位置信息
 */
async function getActiveWindowInfo() {
    const system = (0, os_1.platform)();
    try {
        if (system === "win32") {
            return await getActiveWindowWindows();
        }
        else if (system === "darwin") {
            return await getActiveWindowMacOS();
        }
        else if (system === "linux") {
            return await getActiveWindowLinux();
        }
    }
    catch (err) {
        console.warn(`窗口信息采集失败 (${system}):`, err);
    }
    return createEmptyWindowInfo();
}
/** 轮询器状态 */
class WindowInfoPoller {
    /**
     * @param callback - 每次轮询后调用的回调函数
     * @param intervalMs - 轮询间隔毫秒数，默认 1000ms
     */
    constructor(callback, intervalMs = DEFAULT_POLLING_INTERVAL_MS) {
        this._timer = null;
        this._running = false;
        this._callback = callback;
        this._intervalMs = intervalMs;
    }
    /** 轮询器是否正在运行 */
    get isRunning() {
        return this._running;
    }
    /** 启动窗口信息轮询 */
    start() {
        if (this._running) {
            console.warn("轮询器已在运行，忽略重复启动");
            return;
        }
        this._running = true;
        console.info(`窗口信息轮询已启动，间隔 ${this._intervalMs}ms`);
        // 立即执行一次
        this._poll();
        // 定时轮询
        this._timer = setInterval(() => {
            this._poll();
        }, this._intervalMs);
    }
    /** 停止窗口信息轮询 */
    stop() {
        this._running = false;
        if (this._timer !== null) {
            clearInterval(this._timer);
            this._timer = null;
        }
        console.info("窗口信息轮询已停止");
    }
    /** 执行一次轮询 */
    _poll() {
        getActiveWindowInfo()
            .then((info) => {
            if (this._running) {
                this._callback(info);
            }
        })
            .catch((err) => {
            console.error("窗口信息采集异常:", err);
        });
    }
}
exports.WindowInfoPoller = WindowInfoPoller;
/**
 * 启动窗口信息轮询的便捷函数。
 *
 * @param mainWindow - 要发送窗口信息的 BrowserWindow
 * @param intervalMs - 轮询间隔毫秒数
 * @returns WindowInfoPoller 实例，可用于停止轮询
 */
function startWindowInfoPolling(mainWindow, intervalMs = DEFAULT_POLLING_INTERVAL_MS) {
    const poller = new WindowInfoPoller((info) => {
        if (!mainWindow.isDestroyed()) {
            mainWindow.webContents.send("window-info", info);
        }
    }, intervalMs);
    poller.start();
    return poller;
}
/**
 * 停止窗口信息轮询的便捷函数。
 *
 * @param poller - 要停止的轮询器实例
 */
function stopWindowInfoPolling(poller) {
    poller.stop();
}
//# sourceMappingURL=window-info.js.map