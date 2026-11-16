// @feature: FP-0.2.三 宿主接入 | @ci: frontend-test
/**
 * 编辑类右键菜单（装机版「无法右键复制」修复，2026-09-28）。
 *
 * 职责边界：本模块只承载右键菜单。Electron 的 webContents 没有内建
 * 右键菜单（dev 态前端跑在浏览器、菜单由浏览器自带），生产环境
 * Menu.setApplicationMenu(null)（自定义标题栏取代原生菜单）后由这里
 * 按 context-menu 事件的 editFlags 弹出剪切/复制/粘贴/全选；右键处
 * 无任何可编辑语义时不弹空菜单。
 *
 * 键盘 Ctrl/Cmd+C/X/V/A 一律走 Chromium 原生路径，不在
 * before-input-event 代理：原生路径在 Windows/Linux 独立于应用菜单生效
 * （Electron 39 无应用菜单实测粘贴/全选/复制/剪切、含选中非编辑区文本
 * 复制全部通过）；主进程代理会把粘贴逼进「renderer→主进程→renderer」
 * 异步绕路，与剪贴板写入型注入工具（语音鼠标等）竞态——工具写完剪贴板
 * 随即恢复原内容时，迟到的 paste 读到旧内容或空，正文不上屏。实测与
 * 裁决见 docs/decisions/2026-10-23-remove-edit-shortcut-keyboard-fallback.md。
 */
import { Menu, type BrowserWindow } from "electron";

/** 右键处编辑能力子集（context-menu 事件 params.editFlags 的消费面） */
export interface EditMenuFlags {
  canCut: boolean;
  canCopy: boolean;
  canPaste: boolean;
  canSelectAll: boolean;
}

/**
 * 构造编辑右键菜单模板（纯函数，便于单测）。
 *
 * 四项固定齐全、按 editFlags 置灰（与 Electron 官方 context-menu 示例
 * 同口径：禁用项灰显而非隐藏）；全部不可用返回 null（调用方不弹菜单）。
 */
export function buildEditContextMenuTemplate(
  flags: EditMenuFlags,
): Electron.MenuItemConstructorOptions[] | null {
  const template: Electron.MenuItemConstructorOptions[] = [
    { label: "剪切", role: "cut", enabled: flags.canCut },
    { label: "复制", role: "copy", enabled: flags.canCopy },
    { label: "粘贴", role: "paste", enabled: flags.canPaste },
    { label: "全选", role: "selectAll", enabled: flags.canSelectAll },
  ];
  if (!template.some((item) => item.enabled)) {
    return null;
  }
  return template;
}

function attachEditContextMenu(win: BrowserWindow): void {
  win.webContents.on("context-menu", (_event, params) => {
    const template = buildEditContextMenuTemplate(params.editFlags);
    if (!template) {
      return;
    }
    Menu.buildFromTemplate(template).popup({ window: win });
  });
}

/**
 * 给窗口挂编辑面（主窗口与子窗口同款，复制粘贴体验不因窗口形态漂移）。
 * 只挂右键菜单；键盘快捷键的承载方是 Chromium 原生路径（见头注释）。
 */
export function attachEditMenu(win: BrowserWindow): void {
  attachEditContextMenu(win);
}
