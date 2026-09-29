/**
 * 渲染层可见性失联自愈（ghost window）。
 *
 * 契约：OS 层窗口可见（IsWindowVisible=true、未最小化）时，渲染层
 * document.visibilityState 必须为 "visible"。失联即两者脱钩——Chromium
 * 按自身可见性管线（遮挡跟踪/合成器状态）判定 hidden，与 OS 层窗口状态
 * 之间没有自动同步回路，常规 show/restore/reload 都不重建该管线；唯一
 * 可靠复位是让窗口走一次完整的隐藏→显示通知序列（hide→show 微抖），
 * 遮挡跟踪重新评估后 visibilityState 翻回 visible。
 *
 * 两个触发源共用同一修复链：
 *  - show/restore 事件钩子：显示/还原后延迟校验渲染层自报状态（启动冷启
 *    即 hidden 的场景经 ready-to-show → show 事件同样进入）；
 *  - 兜底巡检：窗口 OS 层可见期间周期采样，连续多次 hidden 才触发修复
 *    （单次采样可能是瞬时未同步）。
 *
 * 微抖以重试预算为界（单 episode 最多 MAX 次），show/restore 钩子与巡检
 * 经 inFlight 顶点防抖互斥——同一时刻只允许一条修复链在跑。
 */

/** show/restore 后等 Chromium 可见性状态传播完成再查渲染层的延迟（毫秒） */
export const VISIBILITY_VERIFY_DELAY_MS = 1500;

/** 微抖 hide→show 的间隔：给合成器留出处理两条窗口通知的时间（毫秒） */
export const VISIBILITY_NUDGE_GAP_MS = 120;

/** 微抖后复查渲染层的等待，兼作重试间隔（毫秒） */
export const VISIBILITY_NUDGE_RECHECK_MS = 3000;

/** 单个 episode 的微抖预算（首次 + 重试 2 次） */
export const VISIBILITY_NUDGE_MAX_ATTEMPTS = 3;

/** 兜底巡检间隔（毫秒） */
export const VISIBILITY_POLL_INTERVAL_MS = 60_000;

/** 巡检触发修复所需的连续 hidden 采样次数 */
export const VISIBILITY_POLL_STRIKES = 2;

/** 自愈开关环境变量：置 0/off/false 关闭（默认开） */
export const VISIBILITY_WATCHDOG_ENV = "AGENTOS_VISIBILITY_WATCHDOG";

/**
 * 微抖闸门（纯函数）：仅当 OS 层可见、未最小化、渲染层自报 hidden、
 * 无修复链在跑且重试预算未耗尽时才允许微抖。
 */
export function shouldNudgeRenderer(input: {
  windowVisible: boolean;
  minimized: boolean;
  rendererHidden: boolean;
  inFlight: boolean;
  attempts: number;
}): boolean {
  return (
    input.windowVisible &&
    !input.minimized &&
    input.rendererHidden &&
    !input.inFlight &&
    input.attempts < VISIBILITY_NUDGE_MAX_ATTEMPTS
  );
}

/**
 * 巡检连击计数（纯函数）：hidden 连击累加、可见即清零——
 * 中间夹一次可见就不算连续。
 */
export function advanceHiddenStrikes(
  prev: number,
  rendererHidden: boolean,
): number {
  return rendererHidden ? prev + 1 : 0;
}

/**
 * 自愈开关（纯函数）：默认开；环境变量为 "0"/"off"/"false"（不区分大小写）
 * 时关。
 */
export function isVisibilityWatchdogEnabled(
  env: Record<string, string | undefined>,
): boolean {
  const value = env[VISIBILITY_WATCHDOG_ENV];
  if (value === undefined) {
    return true;
  }
  return !["0", "off", "false"].includes(value.toLowerCase());
}

/**
 * 自愈接触的窗口面（结构化子集）：BrowserWindow 天然满足，模块不导入
 * electron，便于无运行时单测。
 */
export interface VisibilityWindow {
  isDestroyed(): boolean;
  isVisible(): boolean;
  isMinimized(): boolean;
  hide(): void;
  show(): void;
  webContents: {
    isDestroyed(): boolean;
    executeJavaScript(code: string): Promise<unknown>;
  };
}

export interface VisibilityRecoveryOptions {
  /** 等待注入（单测时钟）；缺省真实 setTimeout */
  delay?: (ms: number) => Promise<void>;
}

/**
 * 可见性失联自愈控制器：持有单一窗口的修复链状态（重试预算/连击计数/
 * inFlight 标记），驱动微抖与巡检。一个窗口一个实例。
 */
export class VisibilityRecovery {
  private readonly win: VisibilityWindow;
  private readonly delay: (ms: number) => Promise<void>;

  /** 微抖修复链执行中（顶点防抖标记） */
  private inFlight = false;
  /** 当前 episode 已执行的微抖次数 */
  private attempts = 0;
  /** 巡检连续 hidden 采样计数 */
  private strikes = 0;
  /** show/restore 延迟校验定时器 */
  private verifyTimer: ReturnType<typeof setTimeout> | null = null;
  /** 巡检定时器 */
  private pollTimer: ReturnType<typeof setInterval> | null = null;

  constructor(win: VisibilityWindow, options?: VisibilityRecoveryOptions) {
    this.win = win;
    this.delay =
      options?.delay ?? ((ms: number) => new Promise((r) => setTimeout(r, ms)));
  }

  /**
   * show/restore 事件入口：重置重试预算并排一次延迟校验。微抖自身的
   * hide→show 会回声出 show 事件，此时 inFlight 为真——那是修复链自己的
   * 动作，不得重置预算也不得重排校验，否则持续 hidden 时预算被反复清零、
   * 修复链无限循环。
   */
  onWindowShown(): void {
    if (this.inFlight) {
      return;
    }
    this.attempts = 0;
    if (this.verifyTimer !== null) {
      clearTimeout(this.verifyTimer);
    }
    this.verifyTimer = setTimeout(() => {
      this.verifyTimer = null;
      void this.sampleAndMaybeRecover(1, "show-hook");
    }, VISIBILITY_VERIFY_DELAY_MS);
  }

  /** 启动兜底巡检（重复调用忽略） */
  startPolling(): void {
    if (this.pollTimer !== null) {
      return;
    }
    this.pollTimer = setInterval(() => {
      void this.sampleAndMaybeRecover(
        VISIBILITY_POLL_STRIKES,
        "poll",
      );
    }, VISIBILITY_POLL_INTERVAL_MS);
  }

  /** 停止巡检与待决校验（退出清理用，幂等） */
  stop(): void {
    if (this.verifyTimer !== null) {
      clearTimeout(this.verifyTimer);
      this.verifyTimer = null;
    }
    if (this.pollTimer !== null) {
      clearInterval(this.pollTimer);
      this.pollTimer = null;
    }
  }

  /**
   * 采样渲染层可见性，达到该触发源要求的连击数即进入修复链。
   * requiredStrikes=1（show 钩子，事件即时性）或 2（巡检，防瞬时误报）。
   */
  private async sampleAndMaybeRecover(
    requiredStrikes: number,
    trigger: string,
  ): Promise<void> {
    if (this.inFlight || this.win.isDestroyed()) {
      return;
    }
    // OS 层真隐藏/最小化时渲染层 hidden 是预期，不空转不修复
    if (!this.win.isVisible() || this.win.isMinimized()) {
      this.strikes = 0;
      return;
    }
    const state = await this.readRendererVisibility();
    if (state === null) {
      return;
    }
    const hidden = state === "hidden";
    this.strikes = advanceHiddenStrikes(this.strikes, hidden);
    if (!hidden || this.strikes < requiredStrikes) {
      return;
    }
    this.strikes = 0;
    await this.runNudgeCycle(trigger);
  }

  /**
   * 微抖修复链：预算内反复 hide→show 并复查，恢复或预算耗尽即收束。
   * 入口经纯闸门复核（防抖/预算），链内每步都复核窗口存活与 OS 层状态。
   */
  private async runNudgeCycle(trigger: string): Promise<void> {
    if (
      !shouldNudgeRenderer({
        windowVisible: this.win.isVisible(),
        minimized: this.win.isMinimized(),
        rendererHidden: true,
        inFlight: this.inFlight,
        attempts: this.attempts,
      })
    ) {
      return;
    }
    this.inFlight = true;
    try {
      while (this.attempts < VISIBILITY_NUDGE_MAX_ATTEMPTS) {
        if (this.win.isDestroyed() || !this.win.isVisible() || this.win.isMinimized()) {
          return;
        }
        // 每次尝试前重读渲染层：排队的这段时间里状态可能已自行恢复
        const before = await this.readRendererVisibility();
        if (before === null) {
          return;
        }
        if (before !== "hidden") {
          this.attempts = 0;
          return;
        }
        this.attempts += 1;
        const attempt = this.attempts;
        console.warn(
          `[Electron] 渲染层可见性失联（${trigger}），微抖重建 ${attempt}/${VISIBILITY_NUDGE_MAX_ATTEMPTS}，before=hidden`,
        );
        this.win.hide();
        await this.delay(VISIBILITY_NUDGE_GAP_MS);
        // 间隔里用户若最小化了窗口，show 会把它顶回前台——让用户的动作生效，
        // 留待 restore 事件走新一轮校验
        if (this.win.isDestroyed() || this.win.isMinimized()) {
          return;
        }
        this.win.show();
        await this.delay(VISIBILITY_NUDGE_RECHECK_MS);
        if (this.win.isDestroyed()) {
          return;
        }
        const after = await this.readRendererVisibility();
        console.warn(
          `[Electron] 微抖 ${attempt}/${VISIBILITY_NUDGE_MAX_ATTEMPTS} 结果：after=${after ?? "unreadable"}`,
        );
        if (after !== "hidden") {
          this.attempts = 0;
          return;
        }
      }
      // 预算耗尽后闸门持续拒绝（attempts 保持 MAX），直到下一个 show/restore
      // episode 重置预算；不再重复刷耗尽日志
      console.error(
        `[Electron] 渲染层可见性微抖 ${VISIBILITY_NUDGE_MAX_ATTEMPTS} 次后仍 hidden，停止重试`,
      );
    } finally {
      this.inFlight = false;
    }
  }

  /** 读渲染层 visibilityState；窗口/渲染器不可用或查询异常返回 null */
  private async readRendererVisibility(): Promise<string | null> {
    try {
      if (this.win.isDestroyed() || this.win.webContents.isDestroyed()) {
        return null;
      }
      const state = await this.win.webContents.executeJavaScript(
        "document.visibilityState",
      );
      return typeof state === "string" ? state : null;
    } catch (err) {
      console.warn("[Electron] visibilityState 读取失败（跳过本轮校验）:", err);
      return null;
    }
  }
}
