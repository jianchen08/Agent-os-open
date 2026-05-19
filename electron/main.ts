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
 * @param win - BrowserWindow 实例
 */
function loadFrontend(win: BrowserWindow): void {
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
}

/**
 * 清理所有资源（快捷键、托盘、轮询器），准备退出。
 */
function cleanup(): void {
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
