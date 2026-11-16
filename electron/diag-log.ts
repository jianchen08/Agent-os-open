/**
 * 应用壳诊断日志（electron main 侧文件落地）。
 *
 * 背景（装机 2026-10-10 整树退出三连）：electron main 的 console.* 在打包件
 * 无 stdout 消费者，窗口/退出/内核子进程的决策链不留任何痕迹——整树退出后
 * 无法区分「用户托盘退出」「window-all-closed」「外部树杀」。本模块给退出
 * 决策点与内核 exit/stderr 一个最小文件事实源（userData/logs/ 下），观测
 * 埋点不做任何行为改变。
 *
 * 纯函数层（路径/轮转决策）可在 node 环境直接单测；副作用层只有 fs。
 */

import * as fs from "fs";
import * as path from "path";

/** 单文件上限（超过轮转为 .old，单代覆盖）；低频事件线，5MB 足够追溯 */
export const DIAG_LOG_MAX_BYTES = 5 * 1024 * 1024;

/** 已初始化的日志目录（initDiagLog 设置；未初始化时 logDiag 静默跳过） */
let diagDir: string | null = null;

/** 初始化日志目录（main.ts 在 userData 定稿后调用一次；失败降级为无日志） */
export function initDiagLog(dir: string): void {
  try {
    fs.mkdirSync(dir, { recursive: true });
    diagDir = dir;
  } catch {
    diagDir = null;
  }
}

/** 轮转决策（纯函数）：当前尺寸超上限即轮转 */
export function shouldRotateDiagLog(sizeBytes: number, capBytes = DIAG_LOG_MAX_BYTES): boolean {
  return sizeBytes >= capBytes;
}

/** 日志文件名（纯函数）：主文件与轮转代各一份 */
export function diagLogPaths(dir: string): { current: string; old: string } {
  return {
    current: path.join(dir, "electron-main.log"),
    old: path.join(dir, "electron-main.log.old"),
  };
}

/** 内核日志保留期（天，P-29c）：按日轮转无保留上限 = 无界累积，默认 14 天 */
export const KERNEL_LOG_RETENTION_DAYS = 14;

/**
 * kernel.log.<YYYY-MM-DD> 文件名的日期解析（纯函数）：非该形态/日期非法
 * → null（保留策略不删不认识的文件）。日期按纯 UTC 解析——与内核 daily
 * 轮转口径一致（BUG-71：轮转边界 = UTC 午夜）。
 */
export function kernelLogFileDate(fileName: string): Date | null {
  const m = /^kernel\.log\.(\d{4}-\d{2}-\d{2})$/.exec(fileName);
  if (!m) {
    return null;
  }
  const date = new Date(`${m[1]}T00:00:00.000Z`);
  // 形如 2026-13-40 的假日期 Date 解析得 NaN；形如 2026-02-30 被进位到 3 月
  // ——回读比对钉死（进位 = 非法，不删无法定位日期的文件）。
  if (Number.isNaN(date.getTime()) || date.toISOString().slice(0, 10) !== m[1]) {
    return null;
  }
  return date;
}

/**
 * 过期内核日志名单（纯函数，P-29c 保留策略）：文件名日期严格早于
 * now - retentionDays 天即过期；边界当天保留。非 kernel.log.<日期> 形态
 * （kernel-stderr.log、kernel.liveness 等）与非法日期 → 不在名单。
 * 返回被删名单（调用方执行删除，形态可直接单测）。
 */
export function pruneExpiredKernelLogs(
  fileNames: string[],
  now: Date,
  retentionDays: number = KERNEL_LOG_RETENTION_DAYS,
): string[] {
  const cutoffMs = now.getTime() - retentionDays * 24 * 60 * 60 * 1000;
  return fileNames.filter((name) => {
    const date = kernelLogFileDate(name);
    return date !== null && date.getTime() < cutoffMs;
  });
}

/**
 * 追加一行诊断日志（带本地时间戳；未初始化/写失败静默跳过——观测面绝不
 * 反噬主流程）。
 */
export function logDiag(line: string): void {
  if (!diagDir) {
    return;
  }
  const { current, old } = diagLogPaths(diagDir);
  try {
    try {
      const size = fs.statSync(current).size;
      if (shouldRotateDiagLog(size)) {
        fs.rmSync(old, { force: true });
        fs.renameSync(current, old);
      }
    } catch {
      // 首次写入前文件不存在 = 正常路径
    }
    const stamp = new Date().toISOString();
    fs.appendFileSync(current, `${stamp} ${line}\n`, "utf-8");
  } catch {
    // 磁盘故障时观测面退场，不阻断应用
  }
}
