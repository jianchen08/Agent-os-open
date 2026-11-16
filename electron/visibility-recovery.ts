/**
 * 渲染层可见性失联自愈（ghost window）。
 *
 * 契约：OS 层窗口可见（IsWindowVisible=true、未最小化）**且窗口持有 OS
 * 焦点**时，渲染层 document.visibilityState 必须为 "visible"。失联即两者
 * 脱钩——Chromium 按自身可见性管线（遮挡跟踪/合成器状态）判定 hidden，与
 * OS 层窗口状态之间没有自动同步回路，常规 show/restore/reload 都不重建该
 * 管线；唯一可靠复位是让窗口走一次完整的隐藏→显示通知序列（hide→show
 * 微抖），遮挡跟踪重新评估后 visibilityState 翻回 visible。
 *
 * **焦点门控（遮挡误判拦截）**：Windows 上 Chromium 的遮挡跟踪在窗口被
 * 其他窗口完全盖住时会正常地报 hidden——那是遮挡，不是渲染层失联。微抖
 * 对遮挡无效（show 之后窗口仍被盖住，仍报 hidden），却会在任务栏上制造
 * 按钮消失/重现的跳动并抢前台。Electron 不暴露 Chromium 的遮挡状态
 * （无 isOccluded），唯一可用的判据是窗口是否持有 OS 焦点：不在前台 =
 * 用户在看别的窗口 = 遮挡。故失联判定与微抖闸门一律以「持有焦点」为前提。
 *
 * **焦点是硬前提，微抖只在窗口持有焦点时发生**：隐藏前再复核一次焦点，
 * 并区分两种「不持焦」——「用户切走」（上次恢复后仍持焦、此刻不持焦）
 * 则中止本轮，不隐藏用户没在看的窗口；「我们自己的 show() 没归还焦点」
 * （Windows 前台锁可能拒绝 show() 的激活）则继续本轮，且恢复改用
 * showInactive()（恢复显示但不抢前台）。恢复方式按隐藏前的焦点归属取：
 * 隐藏前持焦 → show()（焦点还回原处，用户本来就在这个窗口里），隐藏前
 * 已失焦 → showInactive()。
 *
 * 三个触发源共用同一修复链：
 *  - show/restore 事件钩子：显示/还原后延迟校验渲染层自报状态（启动冷启
 *    即 hidden 的场景经 ready-to-show → show 事件同样进入）——**重置重试
 *    预算**（新 episode）；
 *  - focus 事件钩子：用户切回本应用时的复查时机——**不重置预算**（否则
 *    用户每点一次就再武装一轮 3 次 hide→show，「预算耗尽即停」变无上限）；
 *  - 兜底巡检：窗口 OS 层可见且在前台期间周期采样，连续多次 hidden 才
 *    触发修复（单次采样可能是瞬时未同步）。
 *
 * 微抖以重试预算为界（单 episode 最多 MAX 次），show/restore/focus 钩子
 * 与巡检经 inFlight 顶点防抖互斥——同一时刻只允许一条修复链在跑。
 */

/** show/restore/focus 后等 Chromium 可见性状态传播完成再查渲染层的延迟（毫秒） */
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
 * 渲染层 visibilityState 读取超时（毫秒）。executeJavaScript 无超时语义：
 * 渲染器停摆时该 Promise 永挂，轮询连击永不累计、修复链永不进入——R371
 * 现场失效（17 分钟未自愈）的头号 wedge 候选，超时即放弃本轮采样并留痕。
 */
export const VISIBILITY_READ_TIMEOUT_MS = 5_000;

/**
 * 微抖闸门（纯函数）：仅当 OS 层可见、未最小化、**窗口持有 OS 焦点**、
 * 渲染层自报 hidden、无修复链在跑且重试预算未耗尽时才允许微抖。
 *
 * 焦点条件是遮挡误判闸：窗口不在前台 = 被别的窗口盖住 = Chromium 报
 * hidden 属正常遮挡，微抖既修不好又会在任务栏上制造跳动。
 */
export function shouldNudgeRenderer(input: {
  windowVisible: boolean;
  minimized: boolean;
  windowFocused: boolean;
  rendererHidden: boolean;
  inFlight: boolean;
  attempts: number;
}): boolean {
  return (
    input.windowVisible &&
    !input.minimized &&
    input.windowFocused &&
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
  /** 窗口是否持有 OS 焦点（遮挡误判门控的判据） */
  isFocused(): boolean;
  hide(): void;
  /** 恢复显示并激活（Windows 上自带激活语义，可能被前台锁拒绝） */
  show(): void;
  /** 恢复显示但不激活（不抢用户当前焦点） */
  showInactive(): void;
  webContents: {
    isDestroyed(): boolean;
    executeJavaScript(code: string): Promise<unknown>;
  };
}

export interface VisibilityRecoveryOptions {
  /** 等待注入（单测时钟）；缺省真实 setTimeout */
  delay?: (ms: number) => Promise<void>;
  /** 诊断落盘 sink（主进程注入文件写入；缺省不落盘）。决策点事件逐行
   * 上报——装机版主进程 stdout 脱管即丢，R371 修复A现场失效（17 分钟未
   * 自愈）无据可查的教训。 */
  sink?: (line: string) => void;
  /** 渲染层读取超时注入（单测）；缺省 VISIBILITY_READ_TIMEOUT_MS */
  readTimeoutMs?: number;
  /** 渲染层读取注入（单测）；缺省 win.webContents.executeJavaScript */
  readRendererState?: () => Promise<string | null>;
}

/**
 * 可见性失联自愈控制器：持有单一窗口的修复链状态（重试预算/连击计数/
 * inFlight 标记），驱动微抖与巡检。一个窗口一个实例。
 */
export class VisibilityRecovery {
  private readonly win: VisibilityWindow;
  private readonly delay: (ms: number) => Promise<void>;
  private readonly sink: (line: string) => void;
  private readonly readTimeoutMs: number;
  private readRendererState?: () => Promise<string | null>;

  /** 微抖修复链执行中（顶点防抖标记） */
  private inFlight = false;
  /** 当前 episode 已执行的微抖次数 */
  private attempts = 0;
  /** 巡检连续 hidden 采样计数 */
  private strikes = 0;
  /**
   * 上一次恢复后窗口是否重新持有焦点。初值 true（尚无恢复动作，按「用户仍
   * 在前台」处理）。下一轮判别「用户切走」与「我们自己的 show() 没归还焦点」
   * 靠它——两者在主进程视角下都表现为 isFocused()=false，混为一谈会让微抖
   * 链在第 1 次后自止（白扔 2/3 预算）。
   */
  private focusHeldAfterShow = true;
  /** show/restore 延迟校验定时器 */
  private verifyTimer: ReturnType<typeof setTimeout> | null = null;
  /** 待决校验的触发源（show/restore 优先于 focus，用于 episode 归因） */
  private pendingTrigger: string | null = null;
  /** 巡检定时器 */
  private pollTimer: ReturnType<typeof setInterval> | null = null;

  constructor(win: VisibilityWindow, options?: VisibilityRecoveryOptions) {
    this.win = win;
    this.delay =
      options?.delay ?? ((ms: number) => new Promise((r) => setTimeout(r, ms)));
    this.sink = options?.sink ?? (() => {});
    this.readTimeoutMs = options?.readTimeoutMs ?? VISIBILITY_READ_TIMEOUT_MS;
    this.readRendererState = options?.readRendererState;
  }

  /** 诊断事件落盘（时间戳 + 事件名 + 键值对） */
  private diag(event: string, fields: Record<string, unknown> = {}): void {
    try {
      const kv = Object.entries(fields)
        .map(([k, v]) => `${k}=${String(v)}`)
        .join(" ");
      this.sink(`${new Date().toISOString()} ${event}${kv ? " " + kv : ""}`);
    } catch {
      // 落盘失败不参与修复链（诊断面零影响）
    }
  }

  /**
   * show/restore 事件入口：重置重试预算并排一次延迟校验。微抖自身的
   * hide→show 会回声出 show 事件，此时 inFlight 为真——那是修复链自己的
   * 动作，不得重置预算也不得重排校验，否则持续 hidden 时预算被反复清零、
   * 修复链无限循环。
   */
  onWindowShown(): void {
    this.scheduleVerify("show-hook", true);
  }

  /**
   * focus 事件入口（用户切回本应用）：只排校验，**不重置重试预算**——
   * 预算耗尽后若每次切回都重置，用户每点一次应用就再武装一轮 3 次
   * hide→show（任务栏按钮消失/重现 = 用户主诉面），「预算耗尽即停」的
   * 契约会变成无上限。失联判定以窗口持有焦点为前提（见头注释），用户
   * 切回即需复查一次——但复查不等于重新武装。
   */
  onWindowFocused(): void {
    this.scheduleVerify("focus-hook", false);
  }

  /** 排一次延迟校验；resetBudget=true 才重置本 episode 的重试预算。 */
  private scheduleVerify(trigger: string, resetBudget: boolean): void {
    if (this.inFlight) {
      return;
    }
    // 待决校验已由更高优先的触发源排定时，不被 focus 降级覆盖——show/restore
    // 与 focus 常同拍连发（托盘/通知/双开都是 show()+focus()），降级会让
    // episode 的归因随事件先后摇摆，装机按 trigger 分流时不准。
    if (
      this.verifyTimer !== null &&
      this.pendingTrigger !== null &&
      this.pendingTrigger !== "focus-hook" &&
      trigger === "focus-hook"
    ) {
      return;
    }
    if (resetBudget) {
      this.attempts = 0;
    }
    this.pendingTrigger = trigger;
    if (this.verifyTimer !== null) {
      clearTimeout(this.verifyTimer);
    }
    this.verifyTimer = setTimeout(() => {
      this.verifyTimer = null;
      const pending = this.pendingTrigger ?? trigger;
      this.pendingTrigger = null;
      void this.sampleAndMaybeRecover(1, pending);
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
    this.pendingTrigger = null;
    if (this.pollTimer !== null) {
      clearInterval(this.pollTimer);
      this.pollTimer = null;
    }
  }

  /**
   * 采样渲染层可见性，达到该触发源要求的连击数即进入修复链。
   * requiredStrikes=1（show/focus 钩子，事件即时性）或 2（巡检，防瞬时误报）。
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
    // 焦点门控：窗口不在前台 = 用户在看别的窗口。此时 Chromium 报 hidden
    // 属正常遮挡（Windows 遮挡跟踪），不是渲染层失联——微抖对遮挡无效，
    // 却会在任务栏上制造按钮消失/重现的跳动并抢前台。不采样、不连击。
    if (!this.win.isFocused()) {
      this.strikes = 0;
      this.diag("skip-not-foreground", { trigger });
      return;
    }
    const state = await this.readRendererVisibility();
    if (state === null) {
      return;
    }
    const hidden = state === "hidden";
    this.strikes = advanceHiddenStrikes(this.strikes, hidden);
    if (hidden) {
      this.diag("poll-hidden-strike", { strikes: this.strikes });
    }
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
    // 本 episode 的起点：闸门要求窗口持有焦点，故「上一次恢复的焦点归属」
    // 按 true 起算。逐 episode 重置——否则上一 episode 留下的 false 会让本
    // episode 首轮的「用户切走 vs 我们没还焦」判别失准（把用户切走误判成
    // 我们没还焦 → 继续微抖 → 藏起用户没在看的窗口）。
    this.focusHeldAfterShow = true;
    const windowFocused = this.win.isFocused();
    if (
      !shouldNudgeRenderer({
        windowVisible: this.win.isVisible(),
        minimized: this.win.isMinimized(),
        windowFocused,
        rendererHidden: true,
        inFlight: this.inFlight,
        attempts: this.attempts,
      })
    ) {
      this.diag("nudge-gate-reject", {
        trigger,
        attempts: this.attempts,
        focused: windowFocused,
      });
      return;
    }
    this.inFlight = true;
    this.diag("nudge-start", { trigger });
    try {
      while (this.attempts < VISIBILITY_NUDGE_MAX_ATTEMPTS) {
        if (this.win.isDestroyed() || !this.win.isVisible() || this.win.isMinimized()) {
          this.diag("nudge-abort-os", { attempts: this.attempts });
          return;
        }
        // 每次尝试前重读渲染层：排队的这段时间里状态可能已自行恢复
        const before = await this.readRendererVisibility();
        if (before === null) {
          this.diag("nudge-abort-unreadable", { attempts: this.attempts });
          return;
        }
        if (before !== "hidden") {
          this.diag("nudge-self-healed", { attempts: this.attempts });
          this.attempts = 0;
          return;
        }
        // 隐藏前复核焦点，并区分两种「不持焦」：
        //  - 用户切走了：上一次恢复后窗口仍持焦、此刻不持焦 = 用户去了别的
        //    窗口。此时按焦点门控契约，渲染层 hidden 属正常遮挡而非失联，
        //    且微抖会把用户没在看的窗口藏起来再顶回前台（任务栏按钮消失/
        //    重现 = 用户主诉面）→ 中止，不隐藏。
        //  - 我们自己的 show() 没归还焦点：Windows 前台锁可能拒绝 show() 的
        //    激活（show 自带激活语义）。此时窗口虽不在前台，但用户从未离开，
        //    复位机制与焦点无关 → 继续本轮，且恢复改用 showInactive()（恢复
        //    显示但不抢前台）。把两者混为一谈会让微抖链在第 1 次后自止，
        //    白扔 2/3 预算（复核阶段探针实证）。
        const nowFocused = this.win.isFocused();
        if (!nowFocused) {
          if (this.focusHeldAfterShow) {
            this.diag("nudge-abort-not-foreground", {
              attempts: this.attempts,
              trigger,
            });
            return;
          }
          this.diag("nudge-focus-not-restored", {
            attempts: this.attempts,
            trigger,
          });
        }
        this.attempts += 1;
        const attempt = this.attempts;
        console.warn(
          `[Electron] 渲染层可见性失联（${trigger}），微抖重建 ${attempt}/${VISIBILITY_NUDGE_MAX_ATTEMPTS}，before=hidden`,
        );
        this.diag("nudge-attempt", { attempt, trigger });
        // 恢复方式按隐藏前的焦点归属取：隐藏前持焦 → show()（用户本来就在
        // 这个窗口里，把焦点还回原处）；隐藏前已失焦 → showInactive()（恢复
        // 显示但不激活，不抢用户当前焦点）。
        const restoreWithActivate = nowFocused;
        this.win.hide();
        await this.delay(VISIBILITY_NUDGE_GAP_MS);
        // 间隔里用户若最小化了窗口，show 会把它顶回前台——让用户的动作生效，
        // 留待 restore 事件走新一轮校验
        if (this.win.isDestroyed() || this.win.isMinimized()) {
          this.diag("nudge-abort-minimized", { attempt });
          return;
        }
        if (restoreWithActivate) {
          this.win.show();
        } else {
          this.win.showInactive();
        }
        // 记录本次恢复后窗口是否重新持焦：下一轮的「用户切走 vs 我们没还焦」
        // 判别靠它（show() 可能被 Windows 前台锁拒绝激活）。
        this.focusHeldAfterShow = this.win.isFocused();
        await this.delay(VISIBILITY_NUDGE_RECHECK_MS);
        if (this.win.isDestroyed()) {
          return;
        }
        const after = await this.readRendererVisibility();
        console.warn(
          `[Electron] 微抖 ${attempt}/${VISIBILITY_NUDGE_MAX_ATTEMPTS} 结果：after=${after ?? "unreadable"}`,
        );
        this.diag("nudge-result", { attempt, after: after ?? "unreadable" });
        if (after !== "hidden") {
          this.attempts = 0;
          return;
        }
      }
      // 预算耗尽后闸门持续拒绝（attempts 保持 MAX），直到下一个 show/restore
      // episode 重置预算（focus 钩子只复查不重置——否则用户每点一次应用就
      // 再武装一轮 3 次 hide→show）；不再重复刷耗尽日志
      console.error(
        `[Electron] 渲染层可见性微抖 ${VISIBILITY_NUDGE_MAX_ATTEMPTS} 次后仍 hidden，停止重试`,
      );
      this.diag("nudge-budget-exhausted", { attempts: this.attempts });
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
      // 读取统一过超时闸：webContents 与注入读取都无内建超时语义——渲染器
      // 停摆时永挂会让巡检连击永不累计、修复链永不进入（R371 现场失效的
      // 头号 wedge 候选）。
      const exec = this.readRendererState
        ? this.readRendererState()
        : this.win.webContents.executeJavaScript("document.visibilityState");
      const state = await (this.readTimeoutMs > 0
        ? Promise.race([
            exec,
            new Promise<null>((resolve) =>
              setTimeout(() => resolve(null), this.readTimeoutMs),
            ),
          ])
        : exec);
      if (state === null) {
        // 超时（或渲染器返回非字符串）：本轮放弃，留痕。
        this.diag("read-timeout", { timeoutMs: this.readTimeoutMs });
      }
      return typeof state === "string" ? state : null;
    } catch (err) {
      this.diag("read-error", { err: String(err).slice(0, 120) });
      console.warn("[Electron] visibilityState 读取失败（跳过本轮校验）:", err);
      return null;
    }
  }
}
