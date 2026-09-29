// @ci: frontend-test
/**
 * 主窗口状态持久化（window-state.ts）。
 *
 * 行为契约：
 *  - 存档读写往返等值；缺失/损坏 JSON/形态不符一律按无存档（null）回落，
 *    任何坏档案不得让启动失败；
 *  - 恢复选项：存档位置与任一显示器工作区相交则保留（部分跨界保留——贴边/
 *    双屏跨放是用户意图），完全屏外丢位置保尺寸交回 Electron 居中；最大化
 *    标志透传；无存档回落 fallback 尺寸、无位置、不最大化；
 *  - 捕获：最大化窗口存的是还原态边界（getNormalBounds），而非最大化后的
 *    边界——否则重启后窗口被写死成巨大尺寸。
 *
 * 纯 Node 单测：模块不导入 electron，屏幕矩形与窗口探针均为注入值。
 */

import * as fs from "fs";
import * as os from "os";
import * as path from "path";

import { describe, expect, it } from "vitest";

import {
  captureWindowState,
  loadWindowState,
  restoredWindowOptions,
  saveWindowState,
  type Rect,
  type SavedWindowState,
  type WindowBoundsProbe,
} from "../window-state";

/** 1080p 主屏工作区（高 1040 = 扣任务栏，与真机形态同构）。 */
const DISPLAY_MAIN: Rect = { x: 0, y: 0, width: 1920, height: 1040 };
/** 右侧副屏工作区（2560×1440 扣任务栏，起点 x=1920）。 */
const DISPLAY_RIGHT: Rect = { x: 1920, y: 0, width: 2560, height: 1392 };

function tmpDir(): string {
  return fs.mkdtempSync(path.join(os.tmpdir(), "win-state-"));
}

function state(overrides?: Partial<SavedWindowState>): SavedWindowState {
  return {
    x: 100,
    y: 80,
    width: 1200,
    height: 800,
    isMaximized: false,
    ...overrides,
  };
}

function probe(
  bounds: Rect,
  isMaximized: boolean,
): WindowBoundsProbe {
  return { getNormalBounds: () => bounds, isMaximized: () => isMaximized };
}

describe("loadWindowState：坏档案一律回落 null，不抛错", () => {
  it.each([
    ["目录中无档案", null],
    ["档案不是合法 JSON", "{not json{{"],
    ["JSON 不是对象", "[1,2,3]"],
    ["缺 y 字段", { x: 1, width: 100, height: 100, isMaximized: false }],
    ["width 非数字", { x: 0, y: 0, width: "100", height: 100, isMaximized: false }],
    ["width 非正数", { x: 0, y: 0, width: 0, height: 100, isMaximized: false }],
    ["坐标为 NaN 字面量", { x: "NaN", y: 0, width: 100, height: 100, isMaximized: false }],
    ["isMaximized 非布尔", { x: 0, y: 0, width: 100, height: 100, isMaximized: 1 }],
  ])("%s → null", (_label, content) => {
    const dir = tmpDir();
    if (content !== null) {
      fs.writeFileSync(path.join(dir, "window-state.json"), JSON.stringify(content));
    }
    expect(loadWindowState(dir)).toBeNull();
  });
});

describe("saveWindowState → loadWindowState 往返", () => {
  // 两组量级相反的输入（左上小窗 vs 右下大屏窗）防拟合
  it.each([
    [state()],
    [
      state({
        x: -1920,
        y: 3000,
        width: 2560,
        height: 1392,
        isMaximized: true,
      }),
    ],
  ])("%j 往返等值", (expected) => {
    const dir = tmpDir();
    saveWindowState(dir, expected);
    expect(loadWindowState(dir)).toEqual(expected);
  });

  it("重复覆写取最后一次（幂等落盘）", () => {
    const dir = tmpDir();
    saveWindowState(dir, state({ x: 10, y: 10 }));
    saveWindowState(dir, state({ x: 20, y: 20 }));
    const loaded = loadWindowState(dir);
    expect(loaded?.x).toBe(20);
    expect(loaded?.y).toBe(20);
  });
});

describe("restoredWindowOptions：显示器校验与回落", () => {
  it("无存档 → fallback 尺寸、无位置、不最大化", () => {
    expect(
      restoredWindowOptions(null, [DISPLAY_MAIN], { width: 1200, height: 800 }),
    ).toEqual({ x: undefined, y: undefined, width: 1200, height: 800, isMaximized: false });
  });

  it("存档位置在显示器内 → 位置尺寸原样保留，最大化标志透传", () => {
    const saved = state({ x: 300, y: 200, width: 1440, height: 900, isMaximized: true });
    const restored = restoredWindowOptions(saved, [DISPLAY_MAIN], {
      width: 1200,
      height: 800,
    });
    expect(restored).toEqual({ x: 300, y: 200, width: 1440, height: 900, isMaximized: true });
  });

  it("存档完全屏外（远超所有显示器）→ 丢位置保尺寸", () => {
    const saved = state({ x: 20000, y: -30000, width: 1200, height: 800 });
    const restored = restoredWindowOptions(saved, [DISPLAY_MAIN, DISPLAY_RIGHT], {
      width: 1200,
      height: 800,
    });
    expect(restored.x).toBeUndefined();
    expect(restored.y).toBeUndefined();
    expect(restored.width).toBe(1200);
    expect(restored.height).toBe(800);
  });

  it("部分跨界（横跨主屏与右副屏）→ 位置保留（贴边/双屏跨放是用户意图）", () => {
    const saved = state({ x: 1700, y: 100, width: 1200, height: 800 });
    const restored = restoredWindowOptions(saved, [DISPLAY_MAIN, DISPLAY_RIGHT], {
      width: 1200,
      height: 800,
    });
    expect(restored.x).toBe(1700);
    expect(restored.y).toBe(100);
  });

  it("仅剩副屏（主屏已拔）→ 落在副屏内的存档位置保留", () => {
    const saved = state({ x: 2400, y: 120, width: 1000, height: 700 });
    const restored = restoredWindowOptions(saved, [DISPLAY_RIGHT], {
      width: 1200,
      height: 800,
    });
    expect(restored.x).toBe(2400);
    expect(restored.y).toBe(120);
  });
});

describe("captureWindowState：最大化时存还原态边界", () => {
  it("最大化窗口 → 输出 getNormalBounds（还原态）+ isMaximized=true", () => {
    const captured = captureWindowState(
      probe({ x: 40, y: 30, width: 1280, height: 720 }, true),
    );
    expect(captured).toEqual({
      x: 40,
      y: 30,
      width: 1280,
      height: 720,
      isMaximized: true,
    });
  });

  it("普通窗口 → 边界与最大化标志等值透传", () => {
    const bounds: Rect = { x: -500, y: 600, width: 1000, height: 640 };
    expect(captureWindowState(probe(bounds, false))).toEqual({
      ...bounds,
      isMaximized: false,
    });
  });
});
