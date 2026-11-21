/**
 * 连接池毒化自愈（Electron 壳侧，2026-11-18 装机事故批次 2）。
 *
 * 事故形态：内核全程健康（HTTP 1.8ms、WS 握手端点裸探秒回），但 Electron
 * NetworkService 对 127.0.0.1:9101 的死 ESTABLISHED socket 持续堆积（4→8 条），
 * 渲染进程 WS 两步连接（POST /ws-ticket 取票 + 握手）在重连循环里全部卡死
 * ——升级请求根本到不了内核，重连永不自愈。
 *
 * 自愈链路：渲染进程 GlobalWebSocket 重连计数达阈值（与前端
 * RECONNECT_DEGRADE_ATTEMPTS=10 对齐）经 preload IPC 请求
 * （net:self-heal:request，载荷 { reconnectAttempts }）；主进程先探
 * KERNEL_HEALTH_URL——健康 = 内核正常、毒化在前端网络池，才调
 * session.closeAllConnections() 重建连接池并广播渲染进程
 * （net:self-heal:rebuilt，载荷 { rebuiltAt }）供常驻通知承载；
 * 不健康 = 内核真挂，重建无意义（红线：此时不得 closeAllConnections）。
 *
 * 红线（任务书 2026-11-18）：
 * - 限频：5 分钟冷却窗口内至多一次重建，防重连风暴期反复重建；
 * - 主进程侧零渲染进程反向依赖：决策纯函数 + 副作用全注入，本模块不
 *   import electron（探活/closeAll/广播/时钟由 main.ts 组装注入）。
 */

/** 渲染进程 → 主进程：自愈请求通道（载荷 { reconnectAttempts: number }） */
export const SELF_HEAL_REQUEST_CHANNEL = "net:self-heal:request";

/** 主进程 → 渲染进程：连接池已重建广播（载荷 { rebuiltAt: ISO 字符串 }） */
export const SELF_HEAL_REBUILT_CHANNEL = "net:self-heal:rebuilt";

/** 触发阈值：重连失败次数达此值才受理自愈请求（与前端 RECONNECT_DEGRADE_ATTEMPTS 对齐） */
export const SELF_HEAL_TRIGGER_ATTEMPTS = 10;

/** 冷却窗口：两次重建的最小间隔（防重连风暴期反复重建） */
export const SELF_HEAL_COOLDOWN_MS = 5 * 60_000;

/** 健康探测超时：内核健康端点裸探秒回，5s 足够判死 */
export const SELF_HEAL_PROBE_TIMEOUT_MS = 5_000;

/** 自愈请求载荷（渲染 → 主；preload 接口与渲染服务同形状） */
export interface SelfHealRequestPayload {
  reconnectAttempts: number;
}

/** 自愈决策（纯函数输出；执行层据此动作） */
export type SelfHealDecision =
  | { action: "rebuild" }
  | {
      action: "skip";
      reason:
        | "attempts-below-threshold"
        | "kernel-unhealthy"
        | "cooldown-active";
      /** cooldown-active 时距可重建的剩余毫秒数 */
      retryInMs?: number;
    };

/** 自愈决策输入（探测结果由调用方先行取得——探活失败即 kernel-unhealthy） */
export interface SelfHealDecisionInput {
  /** 渲染进程上报的累计重连失败次数 */
  reconnectAttempts: number;
  /** 内核健康探测结果（true = 内核正常，毒化在前端网络池） */
  kernelHealthy: boolean;
  /** 上次重建时刻（epoch ms）；从未重建为 null */
  lastHealAtMs: number | null;
  /** 当前时刻（epoch ms，注入便于测试） */
  nowMs: number;
}

/**
 * 自愈决策（纯函数，三分支判定顺序 = 便宜在前）：
 * ① 次数不足 → skip（正常退避期的偶发掉线不重建）；
 * ② 内核不健康 → skip（内核真挂时重建无意义）；
 * ③ 冷却窗口内 → skip（限频）；④ 其余 → rebuild。
 */
export function decideSelfHeal(input: SelfHealDecisionInput): SelfHealDecision {
  if (!Number.isFinite(input.reconnectAttempts) || input.reconnectAttempts < SELF_HEAL_TRIGGER_ATTEMPTS) {
    return { action: "skip", reason: "attempts-below-threshold" };
  }
  if (!input.kernelHealthy) {
    return { action: "skip", reason: "kernel-unhealthy" };
  }
  if (input.lastHealAtMs !== null) {
    const elapsed = input.nowMs - input.lastHealAtMs;
    if (elapsed < SELF_HEAL_COOLDOWN_MS) {
      return {
        action: "skip",
        reason: "cooldown-active",
        retryInMs: SELF_HEAL_COOLDOWN_MS - elapsed,
      };
    }
  }
  return { action: "rebuild" };
}

/** 自愈请求处理结果（preload invoke 的返回形状） */
export interface SelfHealResult {
  rebuilt: boolean;
  /** 未重建原因（决策 skip 原因 / invalid-payload） */
  reason?: string;
}

/** 自愈执行器依赖（全部注入；main.ts 组装，测试打桩） */
export interface ConnectionSelfHealOptions {
  /** 内核健康端点 URL（main.ts 传 KERNEL_HEALTH_URL） */
  healthUrl: string;
  /** 健康探测（缺省 fetch + AbortController 超时；resp.ok = 健康） */
  probeHealth?: (url: string, timeoutMs: number) => Promise<boolean>;
  /** 连接池重建（生产 = session.defaultSession.closeAllConnections()） */
  closeAllConnections: () => void | Promise<void>;
  /** 重建成功后的用户通知与渲染侧广播（生产 = webContents.send + 系统通知） */
  notifyRebuilt: (payload: { rebuiltAt: string }) => void;
  /** 时钟（测试注入） */
  now?: () => number;
  /** 日志（测试注入；缺省 console.info） */
  logInfo?: (msg: string, ...args: unknown[]) => void;
}

/** 缺省健康探测：fetch + AbortController 超时，resp.ok 即健康（观测面不抛） */
async function defaultProbeHealth(url: string, timeoutMs: number): Promise<boolean> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const resp = await fetch(url, { signal: controller.signal });
    return resp.ok;
  } catch {
    return false;
  } finally {
    clearTimeout(timer);
  }
}

export interface ConnectionSelfHeal {
  /** IPC 请求入口（main.ts ipcMain.handle 直挂；参数校验 + 探活 + 决策 + 执行） */
  handleSelfHealRequest(payload: unknown): Promise<SelfHealResult>;
  /** 距上次重建的 epoch ms（诊断面；从未重建为 null） */
  lastHealAtMs(): number | null;
}

/**
 * 组装自愈执行器。载荷校验 fail-closed：非法载荷按 invalid-payload 跳过，
 * 不探活不重建（错误是值：原因随结果返回，不抛不吞）。
 */
export function createConnectionSelfHeal(
  opts: ConnectionSelfHealOptions,
): ConnectionSelfHeal {
  const probeHealth = opts.probeHealth ?? defaultProbeHealth;
  const now = opts.now ?? Date.now;
  const logInfo = opts.logInfo ?? ((msg, ...args) => console.info(msg, ...args));
  let lastHeal: number | null = null;

  return {
    lastHealAtMs: () => lastHeal,
    async handleSelfHealRequest(payload: unknown): Promise<SelfHealResult> {
      const attempts = (payload as SelfHealRequestPayload | null)?.reconnectAttempts;
      if (typeof attempts !== "number" || !Number.isFinite(attempts) || attempts < 0) {
        return { rebuilt: false, reason: "invalid-payload" };
      }
      const kernelHealthy = await probeHealth(opts.healthUrl, SELF_HEAL_PROBE_TIMEOUT_MS);
      const decision = decideSelfHeal({
        reconnectAttempts: attempts,
        kernelHealthy,
        lastHealAtMs: lastHeal,
        nowMs: now(),
      });
      if (decision.action === "skip") {
        logInfo(
          "[ConnectionSelfHeal] 自愈跳过 reason=%s attempts=%d kernelHealthy=%s",
          decision.reason, attempts, kernelHealthy,
        );
        return { rebuilt: false, reason: decision.reason };
      }
      await opts.closeAllConnections();
      lastHeal = now();
      const rebuiltAt = new Date(lastHeal).toISOString();
      opts.notifyRebuilt({ rebuiltAt });
      logInfo(
        "[ConnectionSelfHeal] 连接池已重建（内核健康、毒化在前端网络池），等待渲染侧重连",
      );
      return { rebuilt: true };
    },
  };
}
