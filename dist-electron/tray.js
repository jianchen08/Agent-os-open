"use strict";
/**
 * 系统托盘管理模块。
 *
 * 创建系统托盘图标，提供右键菜单（显示窗口、隐藏窗口、退出应用），
 * 以及点击托盘图标切换窗口显示/隐藏。
 */
var __createBinding = (this && this.__createBinding) || (Object.create ? (function(o, m, k, k2) {
    if (k2 === undefined) k2 = k;
    var desc = Object.getOwnPropertyDescriptor(m, k);
    if (!desc || ("get" in desc ? !m.__esModule : desc.writable || desc.configurable)) {
      desc = { enumerable: true, get: function() { return m[k]; } };
    }
    Object.defineProperty(o, k2, desc);
}) : (function(o, m, k, k2) {
    if (k2 === undefined) k2 = k;
    o[k2] = m[k];
}));
var __setModuleDefault = (this && this.__setModuleDefault) || (Object.create ? (function(o, v) {
    Object.defineProperty(o, "default", { enumerable: true, value: v });
}) : function(o, v) {
    o["default"] = v;
});
var __importStar = (this && this.__importStar) || (function () {
    var ownKeys = function(o) {
        ownKeys = Object.getOwnPropertyNames || function (o) {
            var ar = [];
            for (var k in o) if (Object.prototype.hasOwnProperty.call(o, k)) ar[ar.length] = k;
            return ar;
        };
        return ownKeys(o);
    };
    return function (mod) {
        if (mod && mod.__esModule) return mod;
        var result = {};
        if (mod != null) for (var k = ownKeys(mod), i = 0; i < k.length; i++) if (k[i] !== "default") __createBinding(result, mod, k[i]);
        __setModuleDefault(result, mod);
        return result;
    };
})();
Object.defineProperty(exports, "__esModule", { value: true });
exports.createTray = createTray;
exports.destroyTray = destroyTray;
const electron_1 = require("electron");
const path = __importStar(require("path"));
/** 托盘图标资源路径（相对于编译后的 dist-electron 目录） */
const TRAY_ICON_RELATIVE_PATH = "../frontend/public/favicon.ico";
/** 托盘图标路径（macOS 使用模板图标） */
const TRAY_ICON_MAC_PATH = "../frontend/public/icon.png";
/** Tray 实例引用，用于后续销毁 */
let trayInstance = null;
/** 关联的 BrowserWindow 引用 */
let mainWindowRef = null;
/**
 * 解析托盘图标路径。
 *
 * 在开发环境和生产环境中都尝试查找可用的图标文件。
 * 如果找不到图标文件，创建一个简单的占位图标。
 *
 * @returns nativeImage 图标
 */
function resolveTrayIcon() {
    // 尝试加载图标文件
    const iconPaths = [
        path.join(__dirname, TRAY_ICON_RELATIVE_PATH),
        path.join(__dirname, TRAY_ICON_MAC_PATH),
        path.join(electron_1.app.getAppPath(), "frontend", "public", "favicon.ico"),
        path.join(electron_1.app.getAppPath(), "frontend", "public", "icon.png"),
        path.join(electron_1.app.getAppPath(), "public", "favicon.ico"),
        path.join(electron_1.app.getAppPath(), "build", "favicon.ico"),
    ];
    for (const iconPath of iconPaths) {
        try {
            const fs = require("fs");
            if (fs.existsSync(iconPath)) {
                return electron_1.nativeImage.createFromPath(iconPath);
            }
        }
        catch {
            // 忽略文件不存在的情况
        }
    }
    // 创建一个简单的占位图标（16x16 蓝色方块）
    const size = 16;
    const canvas = Buffer.alloc(size * size * 4, 0);
    for (let i = 0; i < size * size; i++) {
        const offset = i * 4;
        canvas[offset] = 0x42; // R
        canvas[offset + 1] = 0x85; // G
        canvas[offset + 2] = 0xf4; // B
        canvas[offset + 3] = 0xff; // A
    }
    return electron_1.nativeImage.createFromBuffer(canvas, {
        width: size,
        height: size,
    });
}
/**
 * 切换主窗口的显示/隐藏状态。
 */
function toggleMainWindow() {
    if (!mainWindowRef || mainWindowRef.isDestroyed()) {
        return;
    }
    if (mainWindowRef.isVisible()) {
        mainWindowRef.hide();
    }
    else {
        mainWindowRef.show();
        mainWindowRef.focus();
    }
}
/**
 * 显示主窗口。
 */
function showMainWindow() {
    if (!mainWindowRef || mainWindowRef.isDestroyed()) {
        return;
    }
    mainWindowRef.show();
    mainWindowRef.focus();
}
/**
 * 隐藏主窗口。
 */
function hideMainWindow() {
    if (!mainWindowRef || mainWindowRef.isDestroyed()) {
        return;
    }
    mainWindowRef.hide();
}
/**
 * 创建系统托盘。
 *
 * @param mainWindow - 关联的 BrowserWindow 实例
 * @returns 创建的 Tray 实例
 */
function createTray(mainWindow) {
    mainWindowRef = mainWindow;
    const icon = resolveTrayIcon();
    const tray = new electron_1.Tray(icon.resize({ width: 16, height: 16 }));
    trayInstance = tray;
    // 设置工具提示
    tray.setToolTip("灵汐助手");
    // 点击托盘图标切换窗口
    tray.on("click", () => {
        toggleMainWindow();
    });
    // 右键菜单
    const contextMenu = electron_1.Menu.buildFromTemplate([
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
            type: "separator",
        },
        {
            label: "退出应用",
            type: "normal",
            click: () => {
                electron_1.app.quit();
            },
        },
    ]);
    tray.setContextMenu(contextMenu);
    return tray;
}
/**
 * 销毁系统托盘，释放资源。
 */
function destroyTray() {
    if (trayInstance !== null) {
        trayInstance.destroy();
        trayInstance = null;
    }
    mainWindowRef = null;
}
//# sourceMappingURL=tray.js.map