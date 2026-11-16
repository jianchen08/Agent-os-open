// @feature: FP-0.2.三 宿主接入 | @ci: frontend-test
/**
 * 编辑右键菜单测试（edit-menu.ts）。
 *
 * 行为契约：
 *  - buildEditContextMenuTemplate：四项固定齐全（剪切/复制/粘贴/全选），
 *    可用性逐项跟随 editFlags；全部不可用 → null（右键无动作，不弹空菜单）；
 *  - attachEditMenu：只挂 context-menu 事件（按 flags 弹菜单，popup 以本窗
 *    为父），全不可用不构造菜单；**不挂 before-input-event**——键盘
 *    Ctrl+C/X/V/A 走 Chromium 原生路径（独立于应用菜单生效），主进程代理
 *    会把粘贴逼进异步绕路、与剪贴板写入型注入工具（语音鼠标等）竞态，
 *    该负空间用「未注册 before-input-event」断言钉死。
 *
 * Electron 运行时是本测试唯一 mock 的外部边界（与 notification.test.ts
 * 同口径）；模板为纯函数直接单测，attach 用假 webContents 驱动已注册的
 * 事件 handler 断言副作用。
 */

import { beforeEach, describe, expect, it, vi } from "vitest";

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

const { buildEditContextMenuTemplate, attachEditMenu } = await import("../edit-menu");

/** 全能力 flags（输入框内已选中文本的右键形态） */
const ALL_ENABLED = {
  canCut: true,
  canCopy: true,
  canPaste: true,
  canSelectAll: true,
};

/** 假窗口：webContents 事件注册表 */
function makeFakeWindow() {
  const handlers = new Map<string, (...args: unknown[]) => void>();
  const webContents = {
    on: vi.fn((name: string, handler: (...args: unknown[]) => void) => {
      handlers.set(name, handler);
    }),
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

describe("attachEditMenu", () => {
  it("只挂 context-menu 事件；不挂 before-input-event（键盘原生路径负空间钉死）", () => {
    const { win, webContents, handlers } = makeFakeWindow();
    attachEditMenu(win);
    expect(webContents.on).toHaveBeenCalledTimes(1);
    expect(handlers.has("context-menu")).toBe(true);
    expect(handlers.has("before-input-event")).toBe(false);
  });

  it("右键可复制处 → 构造菜单并以本窗为父 popup", () => {
    const { win, handlers } = makeFakeWindow();
    attachEditMenu(win);
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
    attachEditMenu(win);
    fireContextMenu(handlers, {
      canCut: false,
      canCopy: false,
      canPaste: false,
      canSelectAll: false,
    });
    expect(electronMock.Menu.buildFromTemplate).not.toHaveBeenCalled();
  });
});
