// @feature: FP-0.2.三 宿主接入 | @ci: frontend-test
/**
 * 编辑类右键菜单 + 快捷键兜底（装机版「无法右键复制」修复，2026-09-28）。
 *
 * 背景：Electron 的 webContents 没有内建右键菜单，且 Windows/Linux 上
 * Ctrl+C/X/V/A 剪贴板快捷键依赖应用菜单的角色加速器——生产环境
 * Menu.setApplicationMenu(null)（自定义标题栏取代原生菜单）后两者一并失效。
 * 开发态前端多跑在浏览器（浏览器自带右键菜单），问题只在 Electron 壳显形。
 *
 *  - attachEditContextMenu：webContents 'context-menu' 事件弹出
 *    剪切/复制/粘贴/全选 菜单，可用性按 editFlags 判定（右键处无任何
 *    可编辑语义时不弹空菜单）；
 *  - attachEditShortcutsFallback：'before-input-event' 拦截 Ctrl/Cmd+
 *    C/X/V/A 并显式调 webContents 编辑方法。preventDefault 压掉原生
 *    路径后单次执行，无论宿主 Chromium 是否已内建处理都不会双触发。
 *    仅生产环境挂载（dev 保留默认菜单，加速器已存在，叠加有双触发风险）。
 */
import { Menu, type BrowserWindow } from "electron";

/** 右键处编辑能力子集（context-menu 事件 params.editFlags 的消费面） */
export interface EditMenuFlags {
  canCut: boolean;
  canCopy: boolean;
  canPaste: boolean;
  canSelectAll: boolean;
}

/** webContents 编辑命令（与 role 一一对应） */
export type EditAction = "copy" | "cut" | "paste" | "selectAll";

/** Ctrl/Cmd+字母 → 编辑命令映射（键 = input.key 小写归一） */
const EDIT_SHORTCUT_ACTIONS: Readonly<Record<string, EditAction>> = {
  c: "copy",
  x: "cut",
  v: "paste",
  a: "selectAll",
};

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

/**
 * 解析键盘输入是否命中编辑快捷键（纯函数，便于单测）。
 *
 * 主修饰键 = 非 darwin 的 Ctrl / darwin 的 Cmd；Alt/Shift 组合一律不命中
 * （保留给浏览器级快捷键与输入大写）。keyup/非编辑键返回 null。
 */
export function resolveEditShortcutAction(input: {
  type: string;
  key: string;
  control: boolean;
  meta: boolean;
  alt: boolean;
  shift: boolean;
}): EditAction | null {
  if (input.type !== "keyDown") {
    return null;
  }
  if (input.alt || input.shift) {
    return null;
  }
  const modifier = process.platform === "darwin" ? input.meta : input.control;
  if (!modifier) {
    return null;
  }
  return EDIT_SHORTCUT_ACTIONS[input.key.toLowerCase()] ?? null;
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

function attachEditShortcutsFallback(win: BrowserWindow): void {
  win.webContents.on("before-input-event", (event, input) => {
    const action = resolveEditShortcutAction(input);
    if (!action) {
      return;
    }
    event.preventDefault();
    win.webContents[action]();
  });
}

/**
 * 给窗口挂编辑面（主窗口与子窗口同款，复制粘贴体验不因窗口形态漂移）。
 *
 * @param opts.withShortcuts - 同时挂键盘兜底；仅生产环境传 true
 * （dev 默认菜单的角色加速器已覆盖，叠加有双触发风险）
 */
export function attachEditMenu(
  win: BrowserWindow,
  opts: { withShortcuts: boolean },
): void {
  attachEditContextMenu(win);
  if (opts.withShortcuts) {
    attachEditShortcutsFallback(win);
  }
}
