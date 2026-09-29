// @feature: FP-0.2.三 宿主接入 | @ci: frontend-test
/**
 * 编辑右键菜单 + 快捷键兜底测试（edit-menu.ts，装机版「无法右键复制」修复）。
 *
 * 行为契约：
 *  - buildEditContextMenuTemplate：四项固定齐全（剪切/复制/粘贴/全选），
 *    可用性逐项跟随 editFlags；全部不可用 → null（右键无动作，不弹空菜单）；
 *  - resolveEditShortcutAction：Ctrl/Cmd+C/X/V/A（keyDown、无 Alt/Shift）→
 *    对应编辑命令；其余形态（keyup、无主修饰键、带 Alt/Shift、非编辑键）→ null；
 *    主修饰键按平台 = 非 darwin 的 Ctrl / darwin 的 Cmd；
 *  - attachEditMenu：context-menu 事件按 flags 弹菜单（popup 以本窗为父），
 *    全不可用不构造菜单；withShortcuts=true 额外挂 before-input-event，
 *    命中快捷键 → preventDefault + 单次调用对应 webContents 编辑方法，
 *    未命中 → 原生路径放行（不 preventDefault）。
 *
 * Electron 运行时是本测试唯一 mock 的外部边界（与 notification.test.ts
 * 同口径）；模板/解析为纯函数直接单测，attach 用假 webContents 驱动
 * 已注册的事件 handler 断言副作用。
 */

import { afterAll, beforeEach, describe, expect, it, vi } from "vitest";

const electronMock = vi.hoisted(() => {
  const builtMenus: Array<{
    template: unknown;
    popup: ReturnType<typeof vi.fn>;
  }> = [];
  return {
    builtMenus,
    Menu: {
      buildFromTemplate: vi.fn(
        (template: unknown) => {
          const menu = { template, popup: vi.fn() };
          builtMenus.push(menu);
          return menu;
        },
      ),
    },
  };
});

vi.mock("electron", () => ({
  Menu: electronMock.Menu,
}));

const { buildEditContextMenuTemplate, resolveEditShortcutAction, attachEditMenu } =
  await import("../edit-menu");

const realPlatform = process.platform;
function setPlatform(platform: string): void {
  Object.defineProperty(process, "platform", { value: platform });
}
afterAll(() => {
  setPlatform(realPlatform);
});

/** 全能力 flags（输入框内已选中文本的右键形态） */
const ALL_ENABLED = {
  canCut: true,
  canCopy: true,
  canPaste: true,
  canSelectAll: true,
};

/** 假窗口：webContents 事件注册表 + 编辑方法 spy */
function makeFakeWindow() {
  const handlers = new Map<string, (...args: unknown[]) => void>();
  const webContents = {
    on: vi.fn((name: string, handler: (...args: unknown[]) => void) => {
      handlers.set(name, handler);
    }),
    copy: vi.fn(),
    cut: vi.fn(),
    paste: vi.fn(),
    selectAll: vi.fn(),
  };
  return { win: { webContents } as never, webContents, handlers };
}

/** 驱动 context-menu handler */
function fireContextMenu(
  handlers: Map<string, (...args: unknown[]) => void>,
  editFlags: Record<string, boolean>,
): void {
  const handler = handlers.get("context-menu");
  if (!handler) {
    throw new Error("context-menu handler 未注册");
  }
  handler({}, { editFlags });
}

/** 驱动 before-input-event handler */
function fireBeforeInput(
  handlers: Map<string, (...args: unknown[]) => void>,
  input: Record<string, unknown>,
): { preventDefault: ReturnType<typeof vi.fn> } {
  const handler = handlers.get("before-input-event");
  if (!handler) {
    throw new Error("before-input-event handler 未注册");
  }
  const event = { preventDefault: vi.fn() };
  handler(event, input);
  return event;
}

/** Ctrl+<key> 的标准 keyDown 输入形态 */
function ctrlKey(key: string, overrides: Record<string, unknown> = {}) {
  return {
    type: "keyDown",
    key,
    control: true,
    meta: false,
    alt: false,
    shift: false,
    ...overrides,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  electronMock.builtMenus.length = 0;
});

describe("buildEditContextMenuTemplate", () => {
  it("全能力（输入框内选中文本）→ 四项齐全、中文标签、role 对应、全可用", () => {
    const template = buildEditContextMenuTemplate(ALL_ENABLED);
    expect(template).not.toBeNull();
    expect(template).toHaveLength(4);
    expect(template!.map((item) => item.label)).toEqual([
      "剪切",
      "复制",
      "粘贴",
      "全选",
    ]);
    expect(template!.map((item) => item.role)).toEqual([
      "cut",
      "copy",
      "paste",
      "selectAll",
    ]);
    expect(template!.every((item) => item.enabled)).toBe(true);
  });

  it("非编辑区选中文本 → 复制/全选可用，剪切/粘贴置灰（可用性逐项跟随 flags）", () => {
    const template = buildEditContextMenuTemplate({
      canCut: false,
      canCopy: true,
      canPaste: false,
      canSelectAll: true,
    });
    expect(template).not.toBeNull();
    const byRole = new Map(template!.map((item) => [item.role, item.enabled]));
    expect(byRole.get("copy")).toBe(true);
    expect(byRole.get("selectAll")).toBe(true);
    expect(byRole.get("cut")).toBe(false);
    expect(byRole.get("paste")).toBe(false);
  });

  it.each([
    ["canCut", "剪切"],
    ["canCopy", "复制"],
    ["canPaste", "粘贴"],
    ["canSelectAll", "全选"],
  ])("仅 %s 可用 → 菜单成立且该项唯一可用（单项能力即成菜单）", (flag, label) => {
    const flags = {
      canCut: false,
      canCopy: false,
      canPaste: false,
      canSelectAll: false,
      [flag]: true,
    };
    const template = buildEditContextMenuTemplate(flags as Record<string, boolean>);
    expect(template).not.toBeNull();
    const enabledItems = template!.filter((item) => item.enabled);
    expect(enabledItems).toHaveLength(1);
    expect(enabledItems[0]!.label).toBe(label);
  });

  it("全部不可用 → 返回 null（不弹空菜单）", () => {
    expect(
      buildEditContextMenuTemplate({
        canCut: false,
        canCopy: false,
        canPaste: false,
        canSelectAll: false,
      }),
    ).toBeNull();
  });
});

describe("resolveEditShortcutAction（win32 口径）", () => {
  beforeEach(() => {
    setPlatform("win32");
  });

  it.each([
    ["c", "copy"],
    ["x", "cut"],
    ["v", "paste"],
    ["a", "selectAll"],
  ])("Ctrl+%s → %s（可枚举映射展开）", (key, action) => {
    expect(resolveEditShortcutAction(ctrlKey(key))).toBe(action);
  });

  it("键名大写归一：Ctrl+C（CapsLock 形态 key='C'）→ copy", () => {
    expect(resolveEditShortcutAction(ctrlKey("C"))).toBe("copy");
  });

  it.each([
    ["keyup 事件", ctrlKey("c", { type: "keyUp" })],
    ["无修饰键", ctrlKey("c", { control: false })],
    ["叠加 Shift（Ctrl+Shift+C 属浏览器级快捷键）", ctrlKey("c", { shift: true })],
    ["叠加 Alt", ctrlKey("v", { alt: true })],
    ["非编辑键 Ctrl+B", ctrlKey("b")],
    ["Meta 主修饰在 win32 不命中", ctrlKey("v", { control: false, meta: true })],
  ])("%s → null（原生路径放行）", (_name, input) => {
    expect(resolveEditShortcutAction(input as Parameters<typeof resolveEditShortcutAction>[0])).toBeNull();
  });
});

describe("resolveEditShortcutAction（darwin 口径）", () => {
  beforeEach(() => {
    setPlatform("darwin");
  });

  it("Cmd+V（meta）→ paste", () => {
    expect(
      resolveEditShortcutAction(ctrlKey("v", { control: false, meta: true })),
    ).toBe("paste");
  });

  it("Control+V 在 darwin 不命中（保留给系统级和弦）", () => {
    expect(resolveEditShortcutAction(ctrlKey("v", { meta: false }))).toBeNull();
  });
});

describe("attachEditMenu", () => {
  beforeEach(() => {
    // darwin 口径用例改写过 process.platform，此处复位（快捷键主修饰键按平台分派）
    setPlatform("win32");
  });

  it("withShortcuts=false → 只挂 context-menu（dev：菜单加速器已覆盖键盘）", () => {
    const { win, webContents, handlers } = makeFakeWindow();
    attachEditMenu(win, { withShortcuts: false });
    expect(webContents.on).toHaveBeenCalledTimes(1);
    expect(handlers.has("context-menu")).toBe(true);
    expect(handlers.has("before-input-event")).toBe(false);
  });

  it("withShortcuts=true → context-menu + before-input-event 双挂载", () => {
    const { win, webContents, handlers } = makeFakeWindow();
    attachEditMenu(win, { withShortcuts: true });
    expect(webContents.on).toHaveBeenCalledTimes(2);
    expect(handlers.has("context-menu")).toBe(true);
    expect(handlers.has("before-input-event")).toBe(true);
  });

  it("右键可复制处 → 构造菜单并以本窗为父 popup", () => {
    const { win, handlers } = makeFakeWindow();
    attachEditMenu(win, { withShortcuts: false });
    fireContextMenu(handlers, ALL_ENABLED);
    expect(electronMock.Menu.buildFromTemplate).toHaveBeenCalledTimes(1);
    const menu = electronMock.builtMenus[0]!;
    expect(menu.popup).toHaveBeenCalledWith({ window: win });
    const template = menu.template as Array<{ label: string; enabled: boolean }>;
    expect(template).toHaveLength(4);
    expect(template.every((item) => item.enabled)).toBe(true);
  });

  it("右键无任何可编辑语义 → 不构造菜单不 popup", () => {
    const { win, handlers } = makeFakeWindow();
    attachEditMenu(win, { withShortcuts: false });
    fireContextMenu(handlers, {
      canCut: false,
      canCopy: false,
      canPaste: false,
      canSelectAll: false,
    });
    expect(electronMock.Menu.buildFromTemplate).not.toHaveBeenCalled();
  });

  it("Ctrl+C keyDown → preventDefault + webContents.copy 单次执行", () => {
    const { win, handlers, webContents } = makeFakeWindow();
    attachEditMenu(win, { withShortcuts: true });
    const event = fireBeforeInput(handlers, ctrlKey("c"));
    expect(event.preventDefault).toHaveBeenCalledTimes(1);
    expect(webContents.copy).toHaveBeenCalledTimes(1);
    expect(webContents.cut).not.toHaveBeenCalled();
    expect(webContents.paste).not.toHaveBeenCalled();
  });

  it("Ctrl+V keyDown → preventDefault + paste；keyup 形态零动作", () => {
    const { win, handlers, webContents } = makeFakeWindow();
    attachEditMenu(win, { withShortcuts: true });
    const keyupEvent = fireBeforeInput(
      handlers,
      ctrlKey("v", { type: "keyUp" }),
    );
    expect(keyupEvent.preventDefault).not.toHaveBeenCalled();
    expect(webContents.paste).not.toHaveBeenCalled();
    fireBeforeInput(handlers, ctrlKey("v"));
    expect(webContents.paste).toHaveBeenCalledTimes(1);
  });

  it("非编辑快捷键（Ctrl+B）→ 不 preventDefault（原生路径放行）", () => {
    const { win, handlers } = makeFakeWindow();
    attachEditMenu(win, { withShortcuts: true });
    const event = fireBeforeInput(handlers, ctrlKey("b"));
    expect(event.preventDefault).not.toHaveBeenCalled();
  });
});
