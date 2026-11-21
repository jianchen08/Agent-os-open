/**
 * 渲染器 JS 堆周期采样（观测埋点，2026-11-12 装机渲染进程 1.6GB 排查缺口）。
 *
 * 现有内存面全是**进程级**：mem_watch 采进程 RSS、app:metrics 采
 * workingSet、内核采 mimalloc 统计——渲染器内部 JS 堆此前零采样，长跑增长
 * 只能开 DevTools 手工看，事后无时间序列可回溯。
 *
 * 本模块按固定周期在渲染进程里读 `performance.memory`（Chromium 专有 API），
 * 一行落 diag-log（`userData/logs/electron-main.log`）。采样失败不虚构数值：
 * 连续失败只在首次留一条降级说明，恢复后重新武装——不在日志里刷屏。
 *
 * 处理函数不 import electron、日志与窗口均注入——可在 node 环境直接单测；
 * 接线（主窗口创建时启动）在 main.ts。
 */

import type { DiagLineSink } from "./renderer-health";

/** 采样周期：60s 与内核 mimalloc 快照同节奏（分钟级足够看出小时级增长） */
export const RENDERER_HEAP_SAMPLE_INTERVAL_MS = 60_000;

/** 渲染器堆采样结果（字节） */
export interface RendererHeapSample {
  /** 已用堆字节（V8 used heap） */
  usedBytes: number;
  /** 已分配堆总量（含未使用的已分配页） */
  totalBytes: number;
  /** 堆上限（V8 old space 上限，Electron 默认约 4GB 量级） */
  limitBytes: number;
}

/** 渲染进程内执行的采样脚本：API 缺失返回 null（不猜 0） */
export const RENDERER_HEAP_PROBE_SOURCE = `(() => {
  const m = typeof performance !== "undefined" ? performance.memory : undefined;
  if (!m) return null;
  return { used: m.usedJSHeapSize, total: m.totalJSHeapSize, limit: m.jsHeapSizeLimit };
})()`;

/** 采样目标（生产 = BrowserWindow.webContents；测试注入桩） */
export interface HeapProbeTarget {
  executeJavaScript(code: string): Promise<unknown>;
}

/** 采样窗口（生产 = BrowserWindow；测试注入桩） */
export interface HeapProbeWindow {
  isDestroyed(): boolean;
  webContents: HeapProbeTarget;
}

/** 解析采样脚本回包：结构不符/非有限非负数一律 null（不虚构） */
export function parseRendererHeapProbe(raw: unknown): RendererHeapSample | null {
  if (raw === null || typeof raw !== "object") return null;
  const record = raw as Record<string, unknown>;
  const used = record.used;
  const total = record.total;
  const limit = record.limit;
  if (typeof used !== "number" || typeof total !== "number" || typeof limit !== "number") {
    return null;
  }
  if (
    !Number.isFinite(used) ||
    !Number.isFinite(total) ||
    !Number.isFinite(limit) ||
    used < 0 ||
    total < 0 ||
    limit < 0
  ) {
    return null;
  }
  return { usedBytes: used, totalBytes: total, limitBytes: limit };
}

/** 采样一次（脚本执行异常向上抛，由调用方决定降级留痕） */
export async function sampleRendererHeap(
  target: HeapProbeTarget,
): Promise<RendererHeapSample | null> {
  const raw = await target.executeJavaScript(RENDERER_HEAP_PROBE_SOURCE);
  return parseRendererHeapProbe(raw);
}

/** 字节 → MB（四舍五入取整；日志只做人读量级，不做精确统计） */
function toMb(bytes: number): number {
  return Math.round(bytes / (1024 * 1024));
}

/** 日志行（字段名即查询键：used/total/limit/使用率） */
export function formatRendererHeapLine(sample: RendererHeapSample): string {
  const usagePercent =
    sample.limitBytes > 0
      ? ((sample.usedBytes / sample.limitBytes) * 100).toFixed(1)
      : "?";
  return (
    `[renderer-heap] used=${toMb(sample.usedBytes)}MB ` +
    `total=${toMb(sample.totalBytes)}MB limit=${toMb(sample.limitBytes)}MB ` +
    `used_limit=${usagePercent}%`
  );
}

/**
 * 启动周期采样，返回停止函数。
 *
 * 首拍延后到第一个周期（页面未就绪时 executeJavaScript 会失败，立即首采
 * 只会制造一条假降级）；窗口已销毁则静默跳过该拍。
 */
export function startRendererHeapSampling(
  win: HeapProbeWindow,
  logDiag: DiagLineSink,
  intervalMs: number = RENDERER_HEAP_SAMPLE_INTERVAL_MS,
): () => void {
  let stopped = false;
  let degradedLogged = false;

  const tick = async (): Promise<void> => {
    if (stopped || win.isDestroyed()) return;
    try {
      const sample = await sampleRendererHeap(win.webContents);
      if (sample === null) {
        if (!degradedLogged) {
          degradedLogged = true;
          logDiag("[renderer-heap] 采样不可用：performance.memory 缺失");
        }
        return;
      }
      degradedLogged = false;
      logDiag(formatRendererHeapLine(sample));
    } catch (err) {
      if (!degradedLogged) {
        degradedLogged = true;
        const message = err instanceof Error ? err.message : String(err);
        logDiag(`[renderer-heap] 采样失败: ${message}`);
      }
    }
  };

  const timer = setInterval(() => {
    void tick();
  }, intervalMs);
  // 采样不阻止应用退出（窗口全关后主进程仍可能因托盘存活，定时器不该是
  // 退出与否的决策因素）
  (timer as { unref?: () => void }).unref?.();

  return () => {
    stopped = true;
    clearInterval(timer);
  };
}
