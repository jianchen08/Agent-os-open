// @ci: frontend-test
/**
 * 渲染层可见性失联自愈（visibility-recovery.ts）。
 *
 * 行为契约：
 *  - 微抖闸门（纯函数）：OS 层可见 + 未最小化 + 渲染层 hidden + 无修复链在跑
 *    + 预算未耗尽，五者缺一不动；
 *  - show 钩子：延迟校验一次；渲染层 hidden 即微抖（hide→show），复查恢复
 *    则预算清零收束；持续 hidden 则预算耗尽（首次 + 重试共 MAX 次）后停止，
 *    预算耗尽后闸门持续拒绝，直到下一个 show/restore episode 重置；
 *  - 微抖自身的 hide→show 回声出的 show 事件不重置预算、不重排校验
 *    （否则持续 hidden 时预算被反复清零、修复链无限循环）；
 *  - 巡检：仅 OS 层可见且未最小化时采样（真隐藏不空转）；连续 2 次 hidden
 *    才修复（夹一次 visible 即不算连续，防瞬时误报）；恢复后不再触发；
 *  - 渲染层查询异常/非字符串：跳过本轮校验，不得让修复链或主进程崩。
 *
 * 模块不导入 electron：窗口面以结构化子集注入，纯 Node 单测 +
 * vitest fake timers 驱动延迟校验与巡检节拍。
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  advanceHiddenStrikes,
  isVisibilityWatchdogEnabled,
  shouldNudgeRenderer,
  VisibilityRecovery,
  VISIBILITY_NUDGE_MAX_ATTEMPTS,
  type VisibilityWindow,
} from "../visibility-recovery";

/** 立即完成的等待注入：修复链内所有 delay 不占真实时钟 */
const immediateDelay = (): Promise<void> => Promise.resolve();

interface FakeWindowOptions {
  /** isVisible() 返回值，默认 true */
  visible?: boolean;
  /** isMinimized() 返回值，默认 false */
  minimized?: boolean;
  /** executeJavaScript 逐次返回值；耗尽后重复最后一项；Error 项表示拒绝 */
  states?: unknown[];
}

function makeFakeWindow(options?: FakeWindowOptions): {
  win: VisibilityWindow;
  counters: { hide: number; show: number; queries: number };
} {
  const counters = { hide: 0, show: 0, queries: 0 };
  const states = [...(options?.states ?? ["visible"])];
  const win: VisibilityWindow = {
    isDestroyed: () => false,
    isVisible: () => options?.visible ?? true,
    isMinimized: () => options?.minimized ?? false,
    hide: () => {
      counters.hide += 1;
    },
    show: () => {
      counters.show += 1;
    },
    webContents: {
      isDestroyed: () => false,
      executeJavaScript: () => {
        counters.queries += 1;
        const state = states[Math.min(counters.queries - 1, states.length - 1)];
        if (state instanceof Error) {
          return Promise.reject(state);
        }
        return Promise.resolve(state);
      },
    },
  };
  return { win, counters };
}

/** 可手动放行的 delay：让修复链停在微抖间隔中段，验证回声/重入语义 */
function makeManualDelay(): {
  delay: (ms: number) => Promise<void>;
  releaseAll: () => void;
  pending: () => number;
} {
  const resolvers: Array<() => void> = [];
  return {
    delay: () =>
      new Promise<void>((resolve) => {
        resolvers.push(resolve);
      }),
    releaseAll: () => {
      for (const resolve of resolvers.splice(0)) {
        resolve();
      }
    },
    pending: () => resolvers.length,
  };
}

/** 排空微任务队列（fake timers 下推进 0ms 即逐批 await 微任务） */
async function drain(): Promise<void> {
  await vi.advanceTimersByTimeAsync(0);
  await vi.advanceTimersByTimeAsync(0);
}

describe("shouldNudgeRenderer：微抖闸门真值表", () => {
  const base = {
    windowVisible: true,
    minimized: false,
    rendererHidden: true,
    inFlight: false,
    attempts: 0,
  };

  it("五条件齐备 → 允许（预算内各次均允许）", () => {
    for (const attempts of [0, 1, VISIBILITY_NUDGE_MAX_ATTEMPTS - 1]) {
      expect(shouldNudgeRenderer({ ...base, attempts })).toBe(true);
    }
  });

  it("预算耗尽 / 修复中 / OS 层不可见 / 最小化 / 渲染层未 hidden → 一律拒绝", () => {
    expect(
      shouldNudgeRenderer({ ...base, attempts: VISIBILITY_NUDGE_MAX_ATTEMPTS }),
    ).toBe(false);
    expect(shouldNudgeRenderer({ ...base, inFlight: true })).toBe(false);
    expect(shouldNudgeRenderer({ ...base, windowVisible: false })).toBe(false);
    expect(shouldNudgeRenderer({ ...base, minimized: true })).toBe(false);
    expect(shouldNudgeRenderer({ ...base, rendererHidden: false })).toBe(false);
  });
});

describe("advanceHiddenStrikes：连击累加与清零", () => {
  it("hidden 连续累加", () => {
    expect(advanceHiddenStrikes(0, true)).toBe(1);
    expect(advanceHiddenStrikes(1, true)).toBe(2);
  });

  it("任一次 visible 即清零（连续性被打断）", () => {
    expect(advanceHiddenStrikes(2, false)).toBe(0);
    expect(advanceHiddenStrikes(0, false)).toBe(0);
  });
});

describe("isVisibilityWatchdogEnabled：开关默认开，0/off/false 关", () => {
  it("未设置或其他值 → 开", () => {
    expect(isVisibilityWatchdogEnabled({})).toBe(true);
    expect(isVisibilityWatchdogEnabled({ AGENTOS_VISIBILITY_WATCHDOG: "1" })).toBe(true);
    expect(isVisibilityWatchdogEnabled({ AGENTOS_VISIBILITY_WATCHDOG: "" })).toBe(true);
  });

  it("0 / off / false（不分大小写）→ 关", () => {
    for (const value of ["0", "off", "false", "FALSE", "Off"]) {
      expect(isVisibilityWatchdogEnabled({ AGENTOS_VISIBILITY_WATCHDOG: value })).toBe(
        false,
      );
    }
  });
});

describe("VisibilityRecovery：show 钩子路径", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("正常恢复：hidden 触发一次微抖，复查 visible 后收束，巡检不再触发", async () => {
    const { win, counters } = makeFakeWindow({
      states: ["hidden", "hidden", "visible", "visible"],
    });
    const recovery = new VisibilityRecovery(win, { delay: immediateDelay });
    recovery.startPolling();

    recovery.onWindowShown();
    await vi.advanceTimersByTimeAsync(1500);

    expect(counters).toEqual({ hide: 1, show: 1, queries: 3 });

    // 恢复后的巡检采样全为 visible：不再微抖
    await vi.advanceTimersByTimeAsync(60_000);
    await vi.advanceTimersByTimeAsync(60_000);
    expect(counters.hide).toBe(1);
    expect(counters.show).toBe(1);
  });

  it("持续 hidden：预算内恰好 MAX 次微抖后停止，闸门持续拒绝后续触发", async () => {
    const { win, counters } = makeFakeWindow({ states: ["hidden"] });
    const recovery = new VisibilityRecovery(win, { delay: immediateDelay });
    recovery.startPolling();

    recovery.onWindowShown();
    await vi.advanceTimersByTimeAsync(1500);
    expect(counters.hide).toBe(VISIBILITY_NUDGE_MAX_ATTEMPTS);
    expect(counters.show).toBe(VISIBILITY_NUDGE_MAX_ATTEMPTS);

    // 巡检连击达到 2 也进不去：预算耗尽静默拒绝，不刷新微抖
    // （queries = 钩子采样 1 + 每次微抖前/后重读 6 + 巡检采样 2）
    await vi.advanceTimersByTimeAsync(60_000);
    await vi.advanceTimersByTimeAsync(60_000);
    expect(counters.hide).toBe(VISIBILITY_NUDGE_MAX_ATTEMPTS);
    expect(counters.queries).toBe(9);
  });

  it("微抖回声（修复链执行中的 show 事件）不重置预算、不重排校验", async () => {
    const manual = makeManualDelay();
    const { win, counters } = makeFakeWindow({ states: ["hidden"] });
    const recovery = new VisibilityRecovery(win, { delay: manual.delay });
    recovery.startPolling();

    recovery.onWindowShown();
    await vi.advanceTimersByTimeAsync(1500);
    // 修复链已启动并停在第一次微抖间隔中段
    expect(counters.hide).toBe(1);
    expect(manual.pending()).toBe(1);

    // 微抖自身的 hide→show 会回声出 show 事件：此刻必须被 inFlight 挡住
    recovery.onWindowShown();
    recovery.onWindowShown();

    // 放行全部间隔，让修复链走完预算
    for (let i = 0; i < 2 * VISIBILITY_NUDGE_MAX_ATTEMPTS + 2; i++) {
      manual.releaseAll();
      await drain();
    }
    expect(counters.hide).toBe(VISIBILITY_NUDGE_MAX_ATTEMPTS);

    // 回声若未被挡住会在此刻排出一次额外校验采样
    // （queries = 钩子采样 1 + 每次微抖前/后重读 6 = 7）
    await vi.advanceTimersByTimeAsync(1500);
    expect(counters).toEqual({ hide: 3, show: 3, queries: 7 });
  });

  it("同一 episode 内多次 show/restore 事件折叠为一次待决校验", async () => {
    const { win, counters } = makeFakeWindow({ states: ["visible"] });
    const recovery = new VisibilityRecovery(win, { delay: immediateDelay });

    recovery.onWindowShown();
    recovery.onWindowShown();
    recovery.onWindowShown();
    await vi.advanceTimersByTimeAsync(1500);

    expect(counters.queries).toBe(1);
    expect(counters.hide).toBe(0);
  });

  it("渲染层查询异常/非字符串：跳过本轮校验，不微抖不崩", async () => {
    const { win, counters } = makeFakeWindow({
      states: [new Error("renderer gone"), 42],
    });
    const recovery = new VisibilityRecovery(win, { delay: immediateDelay });

    recovery.onWindowShown();
    await vi.advanceTimersByTimeAsync(1500);
    expect(counters.hide).toBe(0);

    recovery.onWindowShown();
    await vi.advanceTimersByTimeAsync(1500);
    expect(counters.hide).toBe(0);
    expect(counters.queries).toBe(2);
  });
});

describe("VisibilityRecovery：巡检路径", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("OS 层隐藏/最小化期间不空转：不采样不微抖", async () => {
    const hidden = makeFakeWindow({ visible: false, states: ["hidden"] });
    const recoveryHidden = new VisibilityRecovery(hidden.win, {
      delay: immediateDelay,
    });
    recoveryHidden.startPolling();
    recoveryHidden.onWindowShown();
    await vi.advanceTimersByTimeAsync(1500 + 60_000 * 3);
    expect(hidden.counters.queries).toBe(0);
    expect(hidden.counters.hide).toBe(0);

    const minimized = makeFakeWindow({ minimized: true, states: ["hidden"] });
    const recoveryMin = new VisibilityRecovery(minimized.win, {
      delay: immediateDelay,
    });
    recoveryMin.startPolling();
    await vi.advanceTimersByTimeAsync(60_000 * 3);
    expect(minimized.counters.queries).toBe(0);
  });

  it("连续 2 次 hidden 才修复：夹一次 visible 即不算连续；恢复后不再触发", async () => {
    const { win, counters } = makeFakeWindow({
      states: [
        "hidden", // tick1：连击 1，不动
        "visible", // tick2：清零
        "hidden", // tick3：连击 1，不动
        "hidden", // tick4：连击 2 → 微抖
        "hidden", // 微抖前重读
        "visible", // 微抖后复查 → 恢复
        "visible", // tick5：恢复后采样
      ],
    });
    const recovery = new VisibilityRecovery(win, { delay: immediateDelay });
    recovery.startPolling();

    await vi.advanceTimersByTimeAsync(60_000);
    expect(counters.hide).toBe(0);
    await vi.advanceTimersByTimeAsync(60_000);
    expect(counters.hide).toBe(0);
    await vi.advanceTimersByTimeAsync(60_000);
    expect(counters.hide).toBe(0);

    await vi.advanceTimersByTimeAsync(60_000);
    expect(counters.hide).toBe(1);
    expect(counters.show).toBe(1);

    await vi.advanceTimersByTimeAsync(60_000);
    expect(counters).toEqual({ hide: 1, show: 1, queries: 7 });
  });

  it("stop 后巡检与待决校验全部停止（幂等）", async () => {
    const { win, counters } = makeFakeWindow({ states: ["hidden"] });
    const recovery = new VisibilityRecovery(win, { delay: immediateDelay });
    recovery.startPolling();
    recovery.onWindowShown();
    recovery.stop();
    recovery.stop();

    await vi.advanceTimersByTimeAsync(1500 + 60_000 * 3);
    expect(counters.queries).toBe(0);
    expect(counters.hide).toBe(0);
  });
});

describe("VisibilityRecovery：诊断落盘与读取超时", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("微抖链决策点逐行上报 sink（nudge-start/attempt/result）", async () => {
    const lines: string[] = [];
    const { win } = makeFakeWindow({ states: ["hidden", "hidden", "visible"] });
    const recovery = new VisibilityRecovery(win, {
      delay: immediateDelay,
      sink: (l) => lines.push(l),
    });
    recovery.onWindowShown();
    await vi.advanceTimersByTimeAsync(1500);

    const events = lines.map((l) => l.split(" ")[1]);
    expect(events).toContain("nudge-start");
    expect(events).toContain("nudge-attempt");
    expect(events).toContain("nudge-result");
    expect(lines.every((l) => l.length > 0)).toBe(true);
  });

  it("读取超时：永挂的渲染层查询被超时截断为 null，不微抖且 sink 留痕", async () => {
    const lines: string[] = [];
    const { win } = makeFakeWindow({ states: [] });
    const recovery = new VisibilityRecovery(win, {
      delay: immediateDelay,
      sink: (l) => lines.push(l),
      readTimeoutMs: 20,
      readRendererState: () => new Promise<string | null>(() => {}),
    });
    recovery.onWindowShown();
    await vi.advanceTimersByTimeAsync(1500 + 20);

    const events = lines.map((l) => l.split(" ")[1]);
    expect(events).toContain("read-timeout");
    expect(lines.some((l) => l.includes("nudge-attempt"))).toBe(false);
  });
});
