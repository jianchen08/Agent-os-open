/**
 * Electron 主进程入口。
 *
 * 创建 BrowserWindow 加载 React 前端（开发时加载 Vite dev server，
 * 生产时经 app:// 自定义协议加载 dist/index.html，见 app-protocol.ts），
 * 集成系统托盘、全局快捷键和窗口信息采集。
 */

import { app, BrowserWindow, dialog, globalShortcut, ipcMain, Menu, shell } from "electron";
import * as path from "path";

import {
  APP_BASE_URL,
  installAppProtocolHandler,
  registerAppSchemePrivileges,
} from "./app-protocol";
import {
  ensurePackagedKernelRunning,
  shutdownManagedKernel,
} from "./kernel-manager";
import { createTray, destroyTray } from "./tray";
import {
  startWindowInfoPolling,
  stopWindowInfoPolling,
  WindowInfoPoller,
} from "./window-info";

// 协议特权注册必须在 app ready 之前（模块加载即执行）
registerAppSchemePrivileges();

/** 开发环境下 Vite dev server 的 URL（端口 5188，与 vite.config.ts 一致） */
const VITE_DEV_SERVER_URL = "http://localhost:5188";

/** 全局快捷键：Ctrl+Shift+A 切换窗口显示/隐藏 */
const TOGGLE_SHORTCUT = "Ctrl+Shift+A";

/**
 * 子窗口标记（preload.ts 的 isChildWindow 依据同一字面量）。
 * 子窗口/悬浮窗经 webPreferences.additionalArguments 携带，前端 TitleBar
 * 据此只在主窗口渲染（子浮窗保持无边框悬浮组件形态，不出现标题栏）。
 */
const CHILD_WINDOW_ARG = "--agentos-child-window";

/** 最大化状态推送通道（preload.ts 的 windowControls.onMaximizedChange 监听同一通道） */
const MAXIMIZED_CHANGED_CHANNEL = "window:maximized-changed";

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
 * 应用自身源判定（安全审查 2026-08-19 B-5）：
 *  - dev：Vite dev server（localhost:5188 / 127.0.0.1:5188，兼容 5173 默认口）；
 *  - prod：app://（打包件自定义协议源，见 app-protocol.ts）。
 * 用于 window:open 子窗口 URL 白名单与 will-navigate 导航拦截。
 */
const APP_DEV_ORIGINS = new Set([
  "localhost:5188",
  "127.0.0.1:5188",
  "localhost:5173",
  "127.0.0.1:5173",
]);

function isAppSource(rawUrl: string): boolean {
  try {
    const u = new URL(rawUrl);
    if (u.protocol === "app:") {
      return true;
    }
    if (u.protocol !== "http:" && u.protocol !== "https:") {
      return false;
    }
    return APP_DEV_ORIGINS.has(u.host);
  } catch {
    return false;
  }
}

/**
 * 窗口导航硬化（安全审查 B-5）：
 *  - setWindowOpenHandler：target=_blank 类新窗请求一律拦截，http(s) 外链交给
 *    系统浏览器（协议白名单），其余协议（file:/javascript: 等）静默拒绝；
 *  - will-navigate：主 frame 导航离开应用源即阻止（防导航劫持后 preload
 *    随新页面存活）。
 */
function hardenWindowNavigation(win: BrowserWindow): void {
  win.webContents.setWindowOpenHandler(({ url }) => {
    if (/^https?:\/\//i.test(url)) {
      void shell.openExternal(url);
    }
    return { action: "deny" };
  });
  win.webContents.on("will-navigate", (event, url) => {
    if (!isAppSource(url)) {
      event.preventDefault();
    }
  });
}

/**
 * 创建主窗口。
 *
 * 开发环境加载 Vite dev server URL，生产环境加载构建后的 index.html。
 * 窗口默认置顶，创建后注册快捷键、初始化托盘和窗口信息轮询。
 *
 * @param loadContent - 首屏内容加载器（默认按 dev/prod 分流，见 loadInitialContent）
 */
function createMainWindow(
  loadContent: (win: BrowserWindow) => void = loadInitialContent,
): BrowserWindow {
  const win = new BrowserWindow({
    width: 1200,
    height: 800,
    minWidth: 800,
    minHeight: 600,
    alwaysOnTop: true,
    // 自定义标题栏：Windows/macOS 隐藏原生标题栏（保留系统边框/缩放/Snap），
    // Linux 不支持 titleBarStyle 用整体无边框；窗口控制（最小化/最大化/关闭）
    // 由前端 TitleBar 组件经 window:self:* IPC 承担
    ...(process.platform === "linux"
      ? { frame: false }
      : { titleBarStyle: "hidden" as const }),
    // 开发环境菜单栏默认隐藏（Alt 唤出，保留 DevTools/刷新快捷键）；
    // 生产环境在 whenReady 中整个移除应用菜单
    autoHideMenuBar: true,
    show: false, // 先隐藏，ready-to-show 后再显示
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
    // 窗口图标
    icon: resolveAppIcon(),
  });

  hardenWindowNavigation(win);

  // 最大化状态变化推送前端（TitleBar 的最大化/还原图标切换；
  // 双击拖拽区、Win+方向键等系统路径触发的变化同样覆盖）
  const sendMaximized = (maximized: boolean): void => {
    if (!win.isDestroyed()) {
      win.webContents.send(MAXIMIZED_CHANGED_CHANNEL, maximized);
    }
  };
  win.on("maximize", () => sendMaximized(true));
  win.on("unmaximize", () => sendMaximized(false));

  // 窗口准备好后显示
  win.once("ready-to-show", () => {
    win.show();
  });

  // 加载前端页面
  loadContent(win);

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
 * 开发环境加载 Vite dev server URL，生产环境经 app:// 协议加载 dist/index.html
 * （app-protocol.ts 负责静态伺服、SPA 深链回落与内核 API 代理）。
 *
 * 子窗口场景（opts.url 提供）：url 已是完整应用源深链（dev 为 hash 形态
 * http://localhost:5188/#/p/<pageId>，prod 为 pathname 形态 app://bundle/p/<pageId>，
 * 形态分派见前端 buildChildWindowUrl），直接 loadURL。
 *
 * @param win - BrowserWindow 实例
 * @param opts - 可选，url 为子窗口深链 URL
 */
function loadFrontend(
  win: BrowserWindow,
  opts?: { url?: string },
): void {
  if (opts?.url) {
    win
      .loadURL(opts.url)
      .catch((err) => {
        console.error("[Electron] 加载子窗口页面失败:", err);
      });
    console.info(`[Electron] 子窗口加载: ${opts.url}`);
    return;
  }

  if (isDevelopment()) {
    win
      .loadURL(VITE_DEV_SERVER_URL)
      .catch((err) => {
        console.error(`[Electron] 加载 Vite dev server 失败（dev server 未起？）: ${VITE_DEV_SERVER_URL}`, err);
      });
    // 开发环境打开 DevTools
    win.webContents.openDevTools({ mode: "detach" });
    console.info(`[Electron] 开发模式，加载 Vite dev server: ${VITE_DEV_SERVER_URL}`);
  } else {
    win
      .loadURL(`${APP_BASE_URL}/index.html`)
      .catch((err) => {
        console.error("[Electron] 加载前端页面失败:", err);
      });
    console.info(`[Electron] 生产模式，加载: ${APP_BASE_URL}/index.html`);
  }
}

/** 内核就绪前的加载态页面（纯静态 HTML+CSS，无脚本） */
const KERNEL_LOADING_HTML = `<!doctype html>
<html lang="zh-CN">
<head><meta charset="utf-8"><title>灵汐助手</title>
<style>
  html,body{height:100%;margin:0;background:#f5f6fa;font-family:"Microsoft YaHei",system-ui,sans-serif}
  .box{height:100%;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:18px;color:#4b5563}
  .spin{width:36px;height:36px;border:4px solid #d1d5db;border-top-color:#4f6ef7;border-radius:50%;animation:r 1s linear infinite}
  p{margin:0;font-size:14px}
  @keyframes r{to{transform:rotate(360deg)}}
</style></head>
<body><div class="box"><div class="spin"></div><p>正在启动灵汐助手内核，首次启动可能需要数十秒…</p></div></body>
</html>`;

/** 首屏加载分流：dev 直载前端；生产先显示加载态，内核就绪后再载前端 */
function loadInitialContent(win: BrowserWindow): void {
  if (isDevelopment()) {
    loadFrontend(win);
    return;
  }
  win
    .loadURL(`data:text/html;charset=utf-8,${encodeURIComponent(KERNEL_LOADING_HTML)}`)
    .catch((err) => {
      // ERR_ABORTED = 加载态被后续真实内容导航取代（复用模式下内核秒就绪），属预期
      if ((err as { code?: string })?.code !== "ERR_ABORTED") {
        console.error("[Electron] 加载内核启动加载态失败:", err);
      }
    });
  void bootPackagedKernelThenLoad(win);
}

/**
 * 拉起打包件内核（复用/拉起两态，见 kernel-manager）并在健康就绪后载入前端。
 * 失败（安装损坏/启动失败/就绪超时）一律显式错误对话框 + 退出，不静默白屏。
 */
async function bootPackagedKernelThenLoad(win: BrowserWindow): Promise<void> {
  try {
    const { mode } = await ensurePackagedKernelRunning({
      resourcesPath: process.resourcesPath,
    });
    console.info(`[Electron] 内核就绪（${mode === "reuse" ? "复用已运行内核" : "已拉起打包内核"}），加载前端`);
  } catch (err) {
    // 错误消息自带用户指引（缺失→重装 / 超时→看日志重试），此处原样呈现
    const message = err instanceof Error ? err.message : String(err);
    console.error("[Electron] 内核启动失败:", message);
    dialog.showErrorBox("灵汐助手 启动失败", message);
    app.exit(1);
    return;
  }
  if (!win.isDestroyed()) {
    loadFrontend(win);
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
      sandbox: true,
      // 子窗口标记：preload 据此暴露 isChildWindow=true，前端不渲染 TitleBar
      additionalArguments: [CHILD_WINDOW_ARG],
    },
  });

  hardenWindowNavigation(win);

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

  // ===== 主窗口自控 IPC（前端 TitleBar 组件的自定义标题栏按钮）=====
  // 按 event.sender 反查发起窗口：只作用于发起调用的窗口自身

  // 最小化
  ipcMain.handle("window:self:minimize", (event) => {
    BrowserWindow.fromWebContents(event.sender)?.minimize();
  });

  // 最大化/还原切换；返回切换后的最大化状态
  ipcMain.handle("window:self:maximize-toggle", (event) => {
    const win = BrowserWindow.fromWebContents(event.sender);
    if (!win) {
      return false;
    }
    if (win.isMaximized()) {
      win.unmaximize();
    } else {
      win.maximize();
    }
    return win.isMaximized();
  });

  // 关闭：主窗口 close 事件 preventDefault→hide（收进托盘），与原生 X 行为一致
  ipcMain.handle("window:self:close", (event) => {
    BrowserWindow.fromWebContents(event.sender)?.close();
  });

  // 查询当前最大化状态（TitleBar 挂载时同步初始图标）
  ipcMain.handle("window:self:is-maximized", (event) => {
    return BrowserWindow.fromWebContents(event.sender)?.isMaximized() ?? false;
  });

  // ===== P2/P3 多窗口 IPC（ipcMain.handle,支持 async 返回）=====

  // 创建子窗口/悬浮窗;id 重复则聚焦已有
  ipcMain.handle("window:open", (_event, opts: ChildWindowOptions) => {
    if (!opts || typeof opts.id !== "string" || typeof opts.url !== "string") {
      console.warn("[Electron] window:open 参数非法，需要 {id, url}", opts);
      return { id: opts?.id ?? "", success: false };
    }
    // 子窗口 URL 白名单（安全审查 B-5）：仅应用自身源（dev localhost / prod app:）
    if (!isAppSource(opts.url)) {
      console.warn(`[Electron] window:open 拒绝非应用源 url: ${opts.url}`);
      return { id: opts.id, success: false, reason: "url 不在应用源白名单" };
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

// 禁止多实例：抢锁失败（已有首实例持有锁）即退出，
// 否则第二实例会完整启动（双窗口/双托盘/共享 userData 竞争）。
// 此时 whenReady 不会触发，不会创建窗口/托盘；before-quit 清理路径
// 对「从未 ready」态均为安全空操作（受管内核不存在，shutdown 幂等 no-op）。
const gotTheLock = app.requestSingleInstanceLock();
if (!gotTheLock) {
  app.quit();
}

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

  // 生产环境移除默认应用菜单（File/Edit/View/... 由自定义标题栏取代）；
  // 开发环境保留（autoHideMenuBar 默认隐藏，Alt 唤出，DevTools/刷新快捷键可用）
  if (!isDevelopment()) {
    Menu.setApplicationMenu(null);
  }

  // 注册 app:// 协议处理器（必须先于窗口创建）
  installAppProtocolHandler();

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
  // 收受管内核进程树（taskkill /F /T 连带 sidecar；仅 spawn 模式有受管内核，
  // 复用模式的外部内核不杀；electron 崩溃路径孤儿兜底为遗留项）
  shutdownManagedKernel();
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
