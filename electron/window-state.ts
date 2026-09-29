/**
 * 主窗口状态持久化（位置/尺寸/最大化）。
 *
 * 关闭窗口（收托盘）与退出应用前把当前状态写入 userData/window-state.json，
 * 下次启动恢复，使窗口跨启动记忆位置/尺寸/最大化。存的是「还原态」边界
 * （getNormalBounds）：最大化期间取到的仍是还原后应回到的普通边界，恢复侧
 * 先按普通边界建窗再 maximize，避免把最大化后的边界写死成普通边界。
 *
 * 恢复前做显示器校验：存档位置完全落在所有已接显示器工作区之外（拔显示器/
 * 改分辨率后常见）则丢弃位置保尺寸，交回 Electron 居中；档案缺失/损坏/
 * 形态不符一律按无存档回落。宽高合法性（最小值）由 BrowserWindow 的
 * minWidth/minHeight 在创建时执法，此处不重复。
 *
 * 本模块不导入 electron：屏幕矩形由调用方传入（main.ts 的 screen），
 * 窗口探针以最小接口注入，纯 Node 环境可单测。
 */
import * as fs from "fs";
import * as path from "path";

export const WINDOW_STATE_FILE = "window-state.json";

/** 存档形态（文件格式，字段全部必填）。 */
export interface SavedWindowState {
  x: number;
  y: number;
  width: number;
  height: number;
  isMaximized: boolean;
}

/** 平面矩形（与 Electron Rectangle 同构，避免依赖 electron 类型）。 */
export interface Rect {
  x: number;
  y: number;
  width: number;
  height: number;
}

/** 窗口探针：BrowserWindow 的最小切面（测试注入替身用）。 */
export interface WindowBoundsProbe {
  getNormalBounds(): Rect;
  isMaximized(): boolean;
}

/** 从目录读窗口状态存档；缺失/不可读/损坏/形态不符返回 null。 */
export function loadWindowState(dir: string): SavedWindowState | null {
  let raw: string;
  try {
    raw = fs.readFileSync(path.join(dir, WINDOW_STATE_FILE), "utf-8");
  } catch {
    return null;
  }
  try {
    return parseSavedWindowState(JSON.parse(raw));
  } catch {
    return null;
  }
}

function parseSavedWindowState(value: unknown): SavedWindowState | null {
  if (typeof value !== "object" || value === null) {
    return null;
  }
  const v = value as Record<string, unknown>;
  const finiteNumber = (x: unknown): x is number =>
    typeof x === "number" && Number.isFinite(x);
  if (
    !finiteNumber(v.x) ||
    !finiteNumber(v.y) ||
    !finiteNumber(v.width) ||
    !finiteNumber(v.height) ||
    v.width <= 0 ||
    v.height <= 0 ||
    typeof v.isMaximized !== "boolean"
  ) {
    return null;
  }
  return {
    x: v.x,
    y: v.y,
    width: v.width,
    height: v.height,
    isMaximized: v.isMaximized,
  };
}

/** 存档写入（同步写，与 auth-session 同一落盘口径）。调用方保证目录存在。 */
export function saveWindowState(dir: string, state: SavedWindowState): void {
  fs.writeFileSync(
    path.join(dir, WINDOW_STATE_FILE),
    JSON.stringify(state),
    "utf-8",
  );
}

/** 从存活窗口捕获当前状态；最大化时 getNormalBounds 即还原态边界。 */
export function captureWindowState(win: WindowBoundsProbe): SavedWindowState {
  const b = win.getNormalBounds();
  return {
    x: b.x,
    y: b.y,
    width: b.width,
    height: b.height,
    isMaximized: win.isMaximized(),
  };
}

/** BrowserWindow 构造选项形态（x/y 可缺省 = 由 Electron 居中）。 */
export interface RestoredWindowOptions {
  x?: number;
  y?: number;
  width: number;
  height: number;
  isMaximized: boolean;
}

/**
 * 把存档换算成 BrowserWindow 构造选项：
 *  - 位置仅在边界与至少一台显示器工作区相交时保留（部分跨界保留——贴边/
 *    双屏跨放是用户意图），否则丢位置保尺寸；
 *  - 无存档回落 fallback 尺寸、无位置、不最大化。
 */
export function restoredWindowOptions(
  saved: SavedWindowState | null,
  displays: Rect[],
  fallback: { width: number; height: number },
): RestoredWindowOptions {
  if (saved === null) {
    return {
      width: fallback.width,
      height: fallback.height,
      isMaximized: false,
    };
  }
  const bounds: Rect = {
    x: saved.x,
    y: saved.y,
    width: saved.width,
    height: saved.height,
  };
  const onScreen = displays.some((d) => rectsOverlap(bounds, d));
  return {
    x: onScreen ? bounds.x : undefined,
    y: onScreen ? bounds.y : undefined,
    width: bounds.width,
    height: bounds.height,
    isMaximized: saved.isMaximized,
  };
}

function rectsOverlap(a: Rect, b: Rect): boolean {
  return (
    a.x < b.x + b.width &&
    b.x < a.x + a.width &&
    a.y < b.y + b.height &&
    b.y < a.y + a.height
  );
}
