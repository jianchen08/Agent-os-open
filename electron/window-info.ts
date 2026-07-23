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

import { execFile } from "child_process";
import { platform } from "os";
import { BrowserWindow } from "electron";

/** 轮询默认间隔（毫秒） */
const DEFAULT_POLLING_INTERVAL_MS = 1000;

/**
 * 被识别为"宿主编辑器/引擎"的进程名关键字（大小写不敏感）。
 * 前台窗口的 processName 命中其中任一关键字时，悬浮窗将跟随该宿主窗口定位。
 */
const HOST_PROCESS_KEYWORDS: readonly string[] = ["code", "godot"];

/** 跟随宿主时悬浮窗相对宿主窗口右上角的水平/垂直留白（像素） */
const FOLLOW_MARGIN_X = 16;
const FOLLOW_MARGIN_Y = 16;

/** 跟随时悬浮窗的最大宽度（避免遮挡整个宿主窗口） */
const FOLLOW_MAX_WIDTH = 420;
const FOLLOW_DEFAULT_HEIGHT = 560;

/** 判断给定进程名是否为可跟随的宿主（VSCode / Godot）。大小写不敏感。 */
export function isHostProcess(processName: string): boolean {
  const lower = (processName || "").toLowerCase();
  if (!lower) {
    return false;
  }
  return HOST_PROCESS_KEYWORDS.some((kw) => lower.includes(kw));
}

/**
 * 计算悬浮窗跟随宿主窗口时的目标几何位置（贴宿主右上角）。
 *
 * @param host - 宿主窗口信息
 * @param overlayWidth - 悬浮窗期望宽度
 * @param overlayHeight - 悬浮窗期望高度
 * @returns 目标位置与尺寸（{ x, y, width, height }），单位像素
 */
export function computeFollowBounds(
  host: WindowInfo,
  overlayWidth: number = FOLLOW_MAX_WIDTH,
  overlayHeight: number = FOLLOW_DEFAULT_HEIGHT
): { x: number; y: number; width: number; height: number } {
  // 钳制宽度，避免过宽遮挡宿主
  const width = Math.min(overlayWidth, Math.max(200, host.width - FOLLOW_MARGIN_X * 2));
  const height = overlayHeight;
  // 贴宿主窗口右上角：右内缩 FOLLOW_MARGIN_X，顶内缩 FOLLOW_MARGIN_Y
  const x = host.x + host.width - width - FOLLOW_MARGIN_X;
  const y = host.y + FOLLOW_MARGIN_Y;
  return { x, y, width, height };
}

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

/** 创建空的 WindowInfo 对象 */
function createEmptyWindowInfo(): WindowInfo {
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
function execCommand(
  command: string,
  args: string[],
  timeout: number = 5000
): Promise<string | null> {
  return new Promise((resolve) => {
    execFile(command, args, { timeout }, (error, stdout) => {
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
async function getActiveWindowWindows(): Promise<WindowInfo> {
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
async function getActiveWindowMacOS(): Promise<WindowInfo> {
  // 获取窗口标题和进程名
  const scriptTitle =
    'tell application "System Events" to get {name, name of process whose frontmost is true} of front window';
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
  const scriptBounds =
    'tell application "System Events" to get {position, size} of front window';
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
async function getActiveWindowLinux(): Promise<WindowInfo> {
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

  const geo: Record<string, number> = {};
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
export async function getActiveWindowInfo(): Promise<WindowInfo> {
  const system = platform();
  try {
    if (system === "win32") {
      return await getActiveWindowWindows();
    } else if (system === "darwin") {
      return await getActiveWindowMacOS();
    } else if (system === "linux") {
      return await getActiveWindowLinux();
    }
  } catch (err) {
    console.warn(`窗口信息采集失败 (${system}):`, err);
  }
  return createEmptyWindowInfo();
}

/** 轮询器状态 */
export class WindowInfoPoller {
  private _intervalMs: number;
  private _callback: (info: WindowInfo) => void;
  private _timer: ReturnType<typeof setInterval> | null = null;
  private _running = false;

  /**
   * @param callback - 每次轮询后调用的回调函数
   * @param intervalMs - 轮询间隔毫秒数，默认 1000ms
   */
  constructor(
    callback: (info: WindowInfo) => void,
    intervalMs: number = DEFAULT_POLLING_INTERVAL_MS
  ) {
    this._callback = callback;
    this._intervalMs = intervalMs;
  }

  /** 轮询器是否正在运行 */
  get isRunning(): boolean {
    return this._running;
  }

  /** 启动窗口信息轮询 */
  start(): void {
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
  stop(): void {
    this._running = false;
    if (this._timer !== null) {
      clearInterval(this._timer);
      this._timer = null;
    }
    console.info("窗口信息轮询已停止");
  }

  /** 执行一次轮询 */
  private _poll(): void {
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

/**
 * 启动窗口信息轮询的便捷函数。
 *
 * 每次轮询回调中：
 * 1. 将窗口信息通过 IPC 推送给渲染进程（window-info 频道）；
 * 2. 若前台窗口为宿主（VSCode / Godot），按 processName 过滤后计算悬浮窗位置
 *    （贴宿主窗口右上角），并调用 mainWindow.setBounds() 让悬浮窗跟随宿主。
 *
 * @param mainWindow - 要发送窗口信息并跟随宿主的 BrowserWindow
 * @param intervalMs - 轮询间隔毫秒数
 * @param followHost - 是否启用悬浮窗跟随宿主，默认 true
 * @returns WindowInfoPoller 实例，可用于停止轮询
 */
export function startWindowInfoPolling(
  mainWindow: BrowserWindow,
  intervalMs: number = DEFAULT_POLLING_INTERVAL_MS,
  followHost: boolean = true
): WindowInfoPoller {
  const poller = new WindowInfoPoller((info) => {
    if (mainWindow.isDestroyed()) {
      return;
    }

    // 1. 推送窗口信息给渲染进程
    mainWindow.webContents.send("window-info", info);

    // 2. 悬浮窗跟随宿主窗口（VSCode / Godot）
    if (!followHost) {
      return;
    }
    // 只在宿主窗口有效几何信息且被识别为宿主进程时跟随
    if (
      info.width <= 0 ||
      info.height <= 0 ||
      !isHostProcess(info.processName)
    ) {
      return;
    }
    // 避免在窗口最小化 / 全屏时反复抢焦点移动
    if (mainWindow.isMinimized() || mainWindow.isFullScreen()) {
      return;
    }
    const bounds = computeFollowBounds(info);
    try {
      mainWindow.setBounds(bounds, false);
    } catch (err) {
      console.warn("悬浮窗跟随宿主失败:", err);
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
export function stopWindowInfoPolling(poller: WindowInfoPoller): void {
  poller.stop();
}
