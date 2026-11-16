/**
 * 内核日志面判活消费（Electron 壳侧）。
 *
 * 内核（kernel/crates/api/src/log_liveness.rs）的静默看门狗每 60s 周期把
 * 「最后成功写日志的 epoch ms」写进日志目录下的标记文件 `kernel.liveness`
 * （内容两字段：`<pid> <epoch_ms>`），内核侧静默阈值为 600s。本模块周期
 * 读取该标记文件计算年龄，判活结果并入 app:metrics（监控页「内核日志活
 * 性」）；停摆态经回调告警——同一停摆态只告警一次（跃迁检测），恢复后重
 * 新武装可再告警，不逐周期刷屏。
 *
 * 观测面不反噬：文件缺失/路径不可知/内容不可解析 = markerFound:false +
 * lastOkWriteAgeMs:null + healthy:false（展示端显 —，不误报）；读失败只降
 * 级不抛出。壳只读标记文件，不替代内核自身的静默看门狗。
 *
 * 不 import electron：纯函数与轮询器经 fs/时钟注入可在 node 环境直接单测。
 */

import * as fs from "fs";

/** 活性标记文件名（相对内核日志目录；与内核 log_liveness.rs LIVENESS_MARKER_FILENAME 同名） */
export const LIVENESS_MARKER_FILENAME = "kernel.liveness";

/** 轮询周期（对齐内核 PRODUCTION_CHECK_INTERVAL 的 60s 心跳同量级） */
export const KERNEL_LIVENESS_POLL_INTERVAL_MS = 60_000;

/** 停摆阈值（对齐内核 PRODUCTION_SILENCE_THRESHOLD：无成功写超 600s 判停摆） */
export const KERNEL_LIVENESS_STALE_THRESHOLD_MS = 600_000;

/** 判活快照（app:metrics payload 的 kernelLiveness 字段契约） */
export interface KernelLivenessSnapshot {
  /** 标记文件是否存在且内容可解析 */
  markerFound: boolean;
  /** 距内核最后成功写日志的毫秒数（标记不可用时为 null） */
  lastOkWriteAgeMs: number | null;
  /** 年龄未超停摆阈值 */
  healthy: boolean;
}

/** 无可用标记时的快照（文件缺失/路径不可知/内容不可解析共用） */
export const KERNEL_LIVENESS_UNKNOWN: KernelLivenessSnapshot = {
  markerFound: false,
  lastOkWriteAgeMs: null,
  healthy: false,
};

/** 解析标记文件内容（`<pid> <epoch_ms>`）→ epoch ms；形态不符/非有限数 → null */
export function parseLivenessMarkerEpochMs(content: string): number | null {
  const fields = content.trim().split(/\s+/);
  if (fields.length !== 2) {
    return null;
  }
  const epochMs = Number(fields[1]);
  return Number.isFinite(epochMs) && epochMs >= 0 ? epochMs : null;
}

/** 判活评估（纯函数）：标记内容 null = 不可用；年龄 ≤ 阈值判健康 */
export function evaluateKernelLiveness(input: {
  markerContent: string | null;
  nowMs: number;
  staleThresholdMs: number;
}): KernelLivenessSnapshot {
  if (input.markerContent === null) {
    return { ...KERNEL_LIVENESS_UNKNOWN };
  }
  const epochMs = parseLivenessMarkerEpochMs(input.markerContent);
  if (epochMs === null) {
    return { ...KERNEL_LIVENESS_UNKNOWN };
  }
  const ageMs = input.nowMs - epochMs;
  return {
    markerFound: true,
    lastOkWriteAgeMs: ageMs,
    healthy: ageMs <= input.staleThresholdMs,
  };
}

/** 生产读面：标记文件读失败（缺失/权限/占用等）一律 null，观测面不反噬 */
function readMarkerFile(path: string): string | null {
  try {
    return fs.readFileSync(path, "utf-8");
  } catch {
    return null;
  }
}

/** 停摆/恢复跃迁回调（生产 = diag-log 告警与恢复留痕） */
export type LivenessTransitionListener = (snapshot: KernelLivenessSnapshot) => void;

export interface KernelLivenessPoller {
  /** 当前判活快照（app:metrics 消费；返回副本防外部篡改） */
  snapshot(): KernelLivenessSnapshot;
  /** 停止轮询（壳退出清理） */
  stop(): void;
}

export interface KernelLivenessPollerOptions {
  /** 标记文件绝对路径；null = 路径不可知（恒报 unknown，不触 fs） */
  markerPath: string | null;
  intervalMs?: number;
  staleThresholdMs?: number;
  now?: () => number;
  readFile?: (path: string) => string | null;
  onTransition?: LivenessTransitionListener;
}

/**
 * 启动判活轮询（构造即先测一轮，metrics 首查即有数据，不等首个周期）。
 *
 * 停摆判定 = 标记在且年龄超阈值；标记不可用（内核未由壳拉起/dev 形态/
 * 内核首周期未落标记）不算停摆、不告警。跃迁（停摆开始/恢复）才回调，
 * 同一停摆态静默不刷屏。
 */
export function startKernelLivenessPoller(
  opts: KernelLivenessPollerOptions,
): KernelLivenessPoller {
  const intervalMs = opts.intervalMs ?? KERNEL_LIVENESS_POLL_INTERVAL_MS;
  const staleThresholdMs =
    opts.staleThresholdMs ?? KERNEL_LIVENESS_STALE_THRESHOLD_MS;
  const now = opts.now ?? Date.now;
  const readFile = opts.readFile ?? readMarkerFile;
  const onTransition = opts.onTransition ?? (() => {});

  let snapshot: KernelLivenessSnapshot = { ...KERNEL_LIVENESS_UNKNOWN };
  let stalled = false;

  const check = (): void => {
    const content =
      opts.markerPath === null ? null : readFile(opts.markerPath);
    const next = evaluateKernelLiveness({
      markerContent: content,
      nowMs: now(),
      staleThresholdMs,
    });
    snapshot = next;
    const nextStalled = next.markerFound && !next.healthy;
    if (nextStalled !== stalled) {
      stalled = nextStalled;
      onTransition(next);
    }
  };

  check();
  const timer = setInterval(check, intervalMs);
  return {
    snapshot: () => ({ ...snapshot }),
    stop: () => clearInterval(timer),
  };
}
