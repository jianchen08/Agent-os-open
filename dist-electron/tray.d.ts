/**
 * 系统托盘管理模块。
 *
 * 创建系统托盘图标，提供右键菜单（显示窗口、隐藏窗口、退出应用），
 * 以及点击托盘图标切换窗口显示/隐藏。
 */
import { Tray, BrowserWindow } from "electron";
/**
 * 创建系统托盘。
 *
 * @param mainWindow - 关联的 BrowserWindow 实例
 * @returns 创建的 Tray 实例
 */
export declare function createTray(mainWindow: BrowserWindow): Tray;
/**
 * 销毁系统托盘，释放资源。
 */
export declare function destroyTray(): void;
//# sourceMappingURL=tray.d.ts.map