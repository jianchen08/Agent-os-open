/**
 * Electron 主进程入口。
 *
 * 创建 BrowserWindow 加载 React 前端（开发时加载 Vite dev server，
 * 生产时加载 dist/index.html），集成系统托盘、全局快捷键和窗口信息采集。
 */

import { app, BrowserWindow, globalShortcut, ipcMain } from "electron";
import * as path from "path";

import { createTray, destroyTray } from "./tray";
import {
  startWindowInfoPolling,
  stopWindowInfoPolling,
  WindowInfoPoller,
} from "./window-info";

/** 开发环境下 Vite dev server 的 URL（端口 5188，与 vite.config.ts 一致） */
const VITE_DEV_SERVER_URL = "http://localhost:5188";

/** 全局快捷键：Ctrl+Shift+A 切换窗口显示/隐藏 */
const TOGGLE_SHORTCUT = "Ctrl+Shift+A";

/** 主窗口引用 */
let mainWindow: BrowserWindow | null = null;

/** 窗口信息轮询器引用 */
let windowInfoPoller: WindowInfoPoller | null = null;

/**
 * 子窗口/悬浮窗注册表
 *
 * key = 前端传入的窗口 id；value = BrowserWindow。
 * 创建时入表，窗口 'closed' 事件触发时自动出表。
 */
const childWindows = new Map<string, BrowserWindow>();

/**
 * 创建子窗口/悬浮窗的参数（与前端 ElectronOpenWindowOptions 对齐）。
 * 由前端经 ipcRenderer.invoke('window:open', opts) 传入。
 */
export interface ChildWindowOptions {
  /** 窗口标识（前端传入，用于后续 close/focus） */
  id: string;
  /** 加载的 URL（如 'http://localhost:5188/#/p/my-page' 深链） */
  url: string;
  /** 窗口标题 */
  title?: string;
  /** 窗口宽度，默认 320 */
  width?: number;
  /** 窗口高度，默认 480 */
  height?: number;
  /** 窗口左上角 X（不传则居中） */
  x?: number;
  /** 窗口左上角 Y（不传则居中） */
  y?: number;
  /** 是否有边框，默认 false（悬浮组件样式） */
  frame?: boolean;
  /** 是否透明，默认 false */
  transparent?: boolean;
  /** 是否置顶，默认 false */
  alwaysOnTop?: boolean;
  /** 是否隐藏任务栏图标，默认 false */
  skipTaskbar?: boolean;
}

/** 子窗口默认宽度（与前端 toElectronOpenOptions 对齐） */
const DEFAULT_CHILD_WIDTH = 320;
/** 子窗口默认高度 */
const DEFAULT_CHILD_HEIGHT = 480;

/**
 * 把前端传入的 opts 合并默认值，生成最终的窗口参数（纯函数，便于单测）。
 *
 * - width/height 缺省时回落到 DEFAULT_CHILD_WIDTH/HEIGHT
 * - frame/transparent/alwaysOnTop/skipTaskbar 缺省时为 false
 * - x/y 允许缺省（由调用方决定居中策略）
 *
 * 不接触 BrowserWindow，故可在无 Electron 运行时环境下测试。
 */
export function resolveChildWindowOptions(opts: ChildWindowOptions): Required<
  Pick<ChildWindowOptions, "id" | "url" | "width" | "height" | "frame" | "transparent" | "alwaysOnTop" | "skipTaskbar">
> &
  Pick<ChildWindowOptions, "title" | "x" | "y"> {
  return {
    id: opts.id,
    url: opts.url,
    title: opts.title,
    width: opts.width ?? DEFAULT_CHILD_WIDTH,
    height: opts.height ?? DEFAULT_CHILD_HEIGHT,
    x: opts.x,
    y: opts.y,
    frame: opts.frame ?? false,
    transparent: opts.transparent ?? false,
    alwaysOnTop: opts.alwaysOnTop ?? false,
    skipTaskbar: opts.skipTaskbar ?? false,
  };
}

/**
 * 判断当前是否为开发环境。
 *
 * 通过 ELECTRON_IS_DEV 环境变量或 app.isPackaged 属性判断。
 */
function isDevelopment(): boolean {
  return (
    process.env.ELECTRON_IS_DEV === "1" || !app.isPackaged
  );
}

/**
 * 创建主窗口。
 *
 * 开发环境加载 Vite dev server URL，生产环境加载构建后的 index.html。
 * 窗口默认置顶，创建后注册快捷键、初始化托盘和窗口信息轮询。
 */
function createMainWindow(): BrowserWindow {
  const win = new BrowserWindow({
    width: 1200,
    height: 800,
    minWidth: 800,
    minHeight: 600,
    alwaysOnTop: true,
    show: false, // 先隐藏，ready-to-show 后再显示
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
    },
    // 窗口图标
    icon: resolveAppIcon(),
  });

  // 窗口准备好后显示
  win.once("ready-to-show", () => {
    win.show();
  });

  // 加载前端页面
  loadFrontend(win);

  // 窗口关闭时隐藏而非退出（配合托盘使用）
  win.on("close", (event) => {
    event.preventDefault();
    win.hide();
  });

  mainWindow = win;
  return win;
}

/**
 * 加载前端页面。
 *
 * 开发环境加载 Vite dev server URL，生产环境加载 dist/index.html。
 *
 * 子窗口场景（opts.url 提供）：开发环境直接 loadURL(opts.url)（带 hash 路由，
 * 如 http://localhost:5188/#/p/my-page）；生产环境从 url 提取 hash 部分，
 * 用 loadFile(indexPath, { hash }) 走文件协议 + hash 路由。
 *
 * @param win - BrowserWindow 实例
 * @param opts - 可选，url 为子窗口深链 URL
 */
function loadFrontend(
  win: BrowserWindow,
  opts?: { url?: string },
): void {
  if (opts?.url) {
    if (isDevelopment()) {
      win.loadURL(opts.url);
      console.info(`[Electron] 子窗口加载（开发模式）: ${opts.url}`);
    } else {
      // 生产模式：从 url 提取 hash（'#/p/pageId' → 'p/pageId'），走 loadFile + hash
      const hashMatch = opts.url.match(/#\/?(.*)$/);
      const hash = hashMatch ? hashMatch[1] : "";
      const indexPath = path.join(__dirname, "../frontend/dist/index.html");
      win
        .loadFile(indexPath, hash ? { hash } : undefined)
        .catch((err) => {
          console.error("[Electron] 加载子窗口页面失败:", err);
        });
      console.info(`[Electron] 子窗口加载（生产模式）: ${indexPath}#${hash}`);
    }
    return;
  }

  if (isDevelopment()) {
    win.loadURL(VITE_DEV_SERVER_URL);
    // 开发环境打开 DevTools
    win.webContents.openDevTools({ mode: "detach" });
    console.info(`[Electron] 开发模式，加载 Vite dev server: ${VITE_DEV_SERVER_URL}`);
  } else {
    const indexPath = path.join(__dirname, "../frontend/dist/index.html");
    win.loadFile(indexPath).catch((err) => {
      console.error("[Electron] 加载前端页面失败:", err);
    });
    console.info(`[Electron] 生产模式，加载: ${indexPath}`);
  }
}

/**
 * 解析应用图标路径。
 *
 * @returns 图标文件路径，找不到时返回 undefined
 */
function resolveAppIcon(): string | undefined {
  const iconCandidates = [
    path.join(app.getAppPath(), "frontend", "public", "favicon.ico"),
    path.join(app.getAppPath(), "frontend", "public", "icon.png"),
    path.join(app.getAppPath(), "build", "icon.png"),
    path.join(app.getAppPath(), "build", "favicon.ico"),
  ];

  for (const iconPath of iconCandidates) {
    try {
      const fs = require("fs") as typeof import("fs");
      if (fs.existsSync(iconPath)) {
        return iconPath;
      }
    } catch {
      // 忽略
    }
  }
  return undefined;
}

/**
 * 注册全局快捷键 Ctrl+Shift+A 切换窗口显示/隐藏。
 */
function registerGlobalShortcut(): void {
  const success = globalShortcut.register(TOGGLE_SHORTCUT, () => {
    toggleMainWindow();
  });

  if (!success) {
    console.warn(`[Electron] 快捷键 ${TOGGLE_SHORTCUT} 注册失败，可能已被占用`);
  } else {
    console.info(`[Electron] 快捷键 ${TOGGLE_SHORTCUT} 注册成功`);
  }
}

/**
 * 切换主窗口的显示/隐藏状态。
 */
function toggleMainWindow(): void {
  if (!mainWindow || mainWindow.isDestroyed()) {
    return;
  }
  if (mainWindow.isVisible()) {
    mainWindow.hide();
  } else {
    mainWindow.show();
    mainWindow.focus();
  }
}

/**
 * 创建子窗口/悬浮窗（P2/P3 多窗口基础设施）。
 *
 * - 复用主窗口的 preload + contextIsolation 安全模型
 * - 通过 loadFrontend(win, {url}) 加载前端深链 URL
 * - 不绑定 parent（默认独立）；如需模态父子关系可后续扩展 opts.modal/parent
 * - 注册到 childWindows，'closed' 事件触发时自动出表
 *
 * id 重复时不重建，而是聚焦已有窗口（幂等语义）。
 *
 * @returns 创建/复用的 BrowserWindow 及是否为新建
 */
function createChildWindow(opts: ChildWindowOptions): {
  win: BrowserWindow;
  created: boolean;
} {
  // 幂等：id 已存在则聚焦已有窗口
  const existing = childWindows.get(opts.id);
  if (existing && !existing.isDestroyed()) {
    if (existing.isMinimized()) {
      existing.restore();
    }
    existing.focus();
    return { win: existing, created: false };
  }

  const resolved = resolveChildWindowOptions(opts);

  const win = new BrowserWindow({
    width: resolved.width,
    height: resolved.height,
    x: resolved.x,
    y: resolved.y,
    frame: resolved.frame,
    transparent: resolved.transparent,
    alwaysOnTop: resolved.alwaysOnTop,
    skipTaskbar: resolved.skipTaskbar,
    title: resolved.title,
    show: false, // ready-to-show 后再显示，避免白屏闪烁
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
    },
  });

  win.once("ready-to-show", () => {
    win.show();
  });

  // 加载子窗口深链 URL（开发模式带 hash 路由）
  loadFrontend(win, { url: opts.url });

  // 关闭时自动从注册表移除（避免悬挂引用）
  win.on("closed", () => {
    childWindows.delete(opts.id);
  });

  childWindows.set(opts.id, win);
  return { win, created: true };
}

/**
 * 注册 IPC 处理程序。
 */
function registerIpcHandlers(): void {
  // 获取应用版本
  ipcMain.on("get-app-version", (event) => {
    event.returnValue = app.getVersion();
  });

  // 获取运行平台
  ipcMain.on("get-platform", (event) => {
    event.returnValue = process.platform;
  });

  // ===== P2/P3 多窗口 IPC（ipcMain.handle,支持 async 返回）=====

  // 创建子窗口/悬浮窗;id 重复则聚焦已有
  ipcMain.handle("window:open", (_event, opts: ChildWindowOptions) => {
    if (!opts || typeof opts.id !== "string" || typeof opts.url !== "string") {
      console.warn("[Electron] window:open 参数非法，需要 {id, url}", opts);
      return { id: opts?.id ?? "", success: false };
    }
    try {
      const { created } = createChildWindow(opts);
      console.info(
        `[Electron] window:open ${created ? "created" : "focused"} id="${opts.id}" url="${opts.url}"`,
      );
      return { id: opts.id, success: true };
    } catch (err) {
      console.error(`[Electron] window:open 创建失败 id="${opts.id}":`, err);
      return { id: opts.id, success: false };
    }
  });

  // 关闭指定子窗口并从注册表移除
  ipcMain.handle("window:close", (_event, id: string) => {
    const win = childWindows.get(id);
    if (win && !win.isDestroyed()) {
      // 移除 closed 监听以避免重复出表（destroy 会触发 closed）
      win.removeAllListeners("closed");
      win.close();
    }
    childWindows.delete(id);
    console.info(`[Electron] window:close id="${id}"`);
  });

  // 聚焦指定子窗口
  ipcMain.handle("window:focus", (_event, id: string) => {
    const win = childWindows.get(id);
    if (win && !win.isDestroyed()) {
      if (win.isMinimized()) {
        win.restore();
      }
      win.focus();
    }
  });

  // 移动指定子窗口
  ipcMain.handle(
    "window:move",
    (_event, id: string, pos: { x: number; y: number }) => {
      const win = childWindows.get(id);
      if (win && !win.isDestroyed() && pos && typeof pos.x === "number" && typeof pos.y === "number") {
        win.setPosition(Math.trunc(pos.x), Math.trunc(pos.y));
      }
    },
  );

  // 调整指定子窗口大小
  ipcMain.handle(
    "window:resize",
    (_event, id: string, size: { width: number; height: number }) => {
      const win = childWindows.get(id);
      if (
        win &&
        !win.isDestroyed() &&
        size &&
        typeof size.width === "number" &&
        typeof size.height === "number"
      ) {
        win.setSize(Math.trunc(size.width), Math.trunc(size.height));
      }
    },
  );
}

/**
 * 清理所有资源（快捷键、托盘、轮询器、子窗口），准备退出。
 */
function cleanup(): void {
  // 关闭所有子窗口（P2/P3 多窗口基础设施）
  try {
    for (const [id, win] of childWindows) {
      if (!win.isDestroyed()) {
        // 移除 closed 监听避免日志噪音
        win.removeAllListeners("closed");
        win.close();
      }
    }
    childWindows.clear();
    console.info("[Electron] 已关闭所有子窗口");
  } catch (err) {
    console.warn("[Electron] 关闭子窗口失败:", err);
  }

  // 注销所有全局快捷键
  try {
    globalShortcut.unregisterAll();
    console.info("[Electron] 已注销所有全局快捷键");
  } catch (err) {
    console.warn("[Electron] 注销快捷键失败:", err);
  }

  // 停止窗口信息轮询
  if (windowInfoPoller !== null) {
    stopWindowInfoPolling(windowInfoPoller);
    windowInfoPoller = null;
  }

  // 销毁托盘
  try {
    destroyTray();
    console.info("[Electron] 已销毁系统托盘");
  } catch (err) {
    console.warn("[Electron] 销毁托盘失败:", err);
  }
}

// ========== 应用生命周期 ==========

// 禁止多实例
app.requestSingleInstanceLock();

app.on("second-instance", () => {
  if (mainWindow && !mainWindow.isDestroyed()) {
    if (!mainWindow.isVisible()) {
      mainWindow.show();
    }
    mainWindow.focus();
  }
});

// 应用就绪后初始化
app.whenReady().then(() => {
  console.info("[Electron] 应用启动中...");

  // 注册 IPC
  registerIpcHandlers();

  // 创建主窗口
  const win = createMainWindow();

  // 注册全局快捷键
  registerGlobalShortcut();

  // 创建系统托盘
  createTray(win);

  // 启动窗口信息轮询
  windowInfoPoller = startWindowInfoPolling(win);

  console.info("[Electron] 应用启动完成");
});

// macOS 激活应用时重新创建窗口
app.on("activate", () => {
  if (BrowserWindow.getAllWindows().length === 0) {
    createMainWindow();
  } else if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.show();
  }
});

// 所有窗口关闭时退出应用（非 macOS）
app.on("window-all-closed", () => {
  if (process.platform !== "darwin") {
    app.quit();
  }
});

// 应用退出前清理资源
app.on("before-quit", () => {
  // 移除 close 事件的 preventDefault，允许窗口真正关闭
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.removeAllListeners("close");
  }
  cleanup();
});

// 快捷键注册失败处理
app.on("will-quit", () => {
  globalShortcut.unregisterAll();
});
