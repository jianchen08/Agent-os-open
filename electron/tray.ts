/**
 * 系统托盘管理模块。
 *
 * 创建系统托盘图标，提供右键菜单（显示窗口、隐藏窗口、退出应用），
 * 以及点击托盘图标切换窗口显示/隐藏。
 */

import { Tray, Menu, nativeImage, BrowserWindow, app, shell, dialog } from "electron";
import * as path from "path";

import { managedKernelLogDir, resolveKernelLogDirForTray } from "./kernel-manager";

/** 托盘图标资源路径（相对于编译后的 dist-electron 目录） */
const TRAY_ICON_RELATIVE_PATH = "../frontend/public/favicon.ico";

/** 托盘图标路径（macOS 使用模板图标） */
const TRAY_ICON_MAC_PATH = "../frontend/public/icon.png";

/** Tray 实例引用，用于后续销毁 */
let trayInstance: Tray | null = null;

/** 关联的 BrowserWindow 引用 */
let mainWindowRef: BrowserWindow | null = null;

/**
 * 解析托盘图标路径。
 *
 * 在开发环境和生产环境中都尝试查找可用的图标文件。
 * 如果找不到图标文件，创建一个简单的占位图标。
 *
 * @returns nativeImage 图标
 */
function resolveTrayIcon(): Electron.NativeImage {
  // 尝试加载图标文件
  const iconPaths = [
    path.join(__dirname, TRAY_ICON_RELATIVE_PATH),
    path.join(__dirname, TRAY_ICON_MAC_PATH),
    path.join(app.getAppPath(), "frontend", "public", "favicon.ico"),
    path.join(app.getAppPath(), "frontend", "public", "icon.png"),
    path.join(app.getAppPath(), "public", "favicon.ico"),
    path.join(app.getAppPath(), "build", "favicon.ico"),
  ];

  for (const iconPath of iconPaths) {
    try {
      const fs = require("fs") as typeof import("fs");
      if (fs.existsSync(iconPath)) {
        return nativeImage.createFromPath(iconPath);
      }
    } catch {
      // 忽略文件不存在的情况
    }
  }

  // 创建一个简单的占位图标（16x16 蓝色方块）
  const size = 16;
  const canvas = Buffer.alloc(size * size * 4, 0);
  for (let i = 0; i < size * size; i++) {
    const offset = i * 4;
    canvas[offset] = 0x42;     // R
    canvas[offset + 1] = 0x85; // G
    canvas[offset + 2] = 0xf4; // B
    canvas[offset + 3] = 0xff; // A
  }
  return nativeImage.createFromBuffer(canvas, {
    width: size,
    height: size,
  });
}

/**
 * 切换主窗口的显示/隐藏状态。
 */
function toggleMainWindow(): void {
  if (!mainWindowRef || mainWindowRef.isDestroyed()) {
    return;
  }
  if (mainWindowRef.isVisible()) {
    mainWindowRef.hide();
  } else {
    mainWindowRef.show();
    mainWindowRef.focus();
  }
}

/**
 * 显示主窗口。
 */
function showMainWindow(): void {
  if (!mainWindowRef || mainWindowRef.isDestroyed()) {
    return;
  }
  mainWindowRef.show();
  mainWindowRef.focus();
}

/**
 * 隐藏主窗口。
 */
function hideMainWindow(): void {
  if (!mainWindowRef || mainWindowRef.isDestroyed()) {
    return;
  }
  mainWindowRef.hide();
}

/**
 * 打开内核日志目录（P-22 日志可达性：日志的终点也应可达——用户自救与报障
 * 入口）。落点解析复用 kernel-manager 单点（受管内核真身优先，dev 回退同一
 * 资源解析）；打不开（目录不存在/无权限）显式报错框，不静默。
 */
async function openKernelLogDir(): Promise<void> {
  const dir = resolveKernelLogDirForTray(managedKernelLogDir(), app.getAppPath());
  const failure = await shell.openPath(dir);
  if (failure) {
    dialog.showErrorBox("无法打开日志目录", `${dir}\n${failure}`);
  }
}

/**
 * 创建系统托盘。
 *
 * @param mainWindow - 关联的 BrowserWindow 实例
 * @returns 创建的 Tray 实例
 */
export function createTray(mainWindow: BrowserWindow): Tray {
  mainWindowRef = mainWindow;

  const icon = resolveTrayIcon();
  const tray = new Tray(icon.resize({ width: 16, height: 16 }));
  trayInstance = tray;

  // 设置工具提示
  tray.setToolTip("灵汐助手");

  // 点击托盘图标切换窗口
  tray.on("click", () => {
    toggleMainWindow();
  });

  // 右键菜单
  const contextMenu = Menu.buildFromTemplate([
    {
      label: "显示窗口",
      type: "normal",
      click: () => {
        showMainWindow();
      },
    },
    {
      label: "隐藏窗口",
      type: "normal",
      click: () => {
        hideMainWindow();
      },
    },
    {
      label: "打开日志目录",
      type: "normal",
      click: () => {
        void openKernelLogDir();
      },
    },
    {
      type: "separator",
    },
    {
      label: "退出应用",
      type: "normal",
      click: () => {
        app.quit();
      },
    },
  ]);

  tray.setContextMenu(contextMenu);

  return tray;
}

/**
 * 销毁系统托盘，释放资源。
 */
export function destroyTray(): void {
  if (trayInstance !== null) {
    trayInstance.destroy();
    trayInstance = null;
  }
  mainWindowRef = null;
}
