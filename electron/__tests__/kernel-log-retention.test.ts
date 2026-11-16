// @ci: frontend-test
/**
 * 内核日志保留策略测试（P-29c：pruneExpiredKernelLogs 纯函数）。
 *
 * 覆盖口径：文件名日期解析（合法/非法/进位假日期/非日志文件）+ 过期过滤
 * （过期删/未过期留/保留边界当天留/retentionDays 参数化）。
 */

import { describe, expect, it } from "vitest";

import {
  KERNEL_LOG_RETENTION_DAYS,
  kernelLogFileDate,
  pruneExpiredKernelLogs,
} from "../diag-log";

/** 固定"现在"（2026-10-29 UTC 午夜），轮转口径纯 UTC 同源 */
const NOW = new Date("2026-10-29T00:00:00.000Z");

describe("kernelLogFileDate 文件名日期解析", () => {
  it("kernel.log.YYYY-MM-DD → 纯 UTC 零点", () => {
    expect(kernelLogFileDate("kernel.log.2026-10-29")?.toISOString()).toBe(
      "2026-10-29T00:00:00.000Z",
    );
  });

  it.each([
    ["kernel-stderr.log", "stderr 落盘文件（自有轮转面，不归保留策略）"],
    ["kernel-stderr.log.old", "stderr 轮转代"],
    ["kernel.liveness", "判活标记"],
    ["electron-main.log", "壳侧诊断日志"],
    ["kernel.log", "缺日期后缀"],
    ["kernel.log.20261029", "日期形态不符（无连字符）"],
    ["kernel.log.2026-13-40", "假日期（月份/日越界）"],
    ["kernel.log.2026-02-30", "进位假日期（2 月 30 日不存在）"],
  ])("非日志日期形态/非法日期 → null（%s：%s）", (name) => {
    expect(kernelLogFileDate(name)).toBeNull();
  });
});

describe("pruneExpiredKernelLogs 过期过滤", () => {
  const dir = [
    "kernel.log.2026-10-14", // 15 天前 → 过期
    "kernel.log.2026-10-15", // 恰好 14 天 → 保留边界，留
    "kernel.log.2026-10-28", // 昨天 → 留
    "kernel.log.2026-09-01", // 远超保留期 → 过期
    "kernel-stderr.log", // 非日期形态 → 留
    "kernel.liveness", // 判活标记 → 留
  ];

  it("默认保留期 14 天：严格过期删除，边界当天保留，非日志文件不碰", () => {
    expect(pruneExpiredKernelLogs(dir, NOW)).toEqual([
      "kernel.log.2026-10-14",
      "kernel.log.2026-09-01",
    ]);
  });

  it("保留边界：now - retentionDays 当天整日保留（只删严格更早的）", () => {
    expect(pruneExpiredKernelLogs(["kernel.log.2026-10-15"], NOW)).toEqual([]);
    expect(pruneExpiredKernelLogs(["kernel.log.2026-10-14"], NOW)).toEqual([
      "kernel.log.2026-10-14",
    ]);
  });

  it("retentionDays 参数化：7 天期下昨天的文件仍在场", () => {
    expect(pruneExpiredKernelLogs(["kernel.log.2026-10-28"], NOW, 7)).toEqual([]);
    expect(pruneExpiredKernelLogs(["kernel.log.2026-10-21"], NOW, 7)).toEqual([
      "kernel.log.2026-10-21",
    ]);
  });

  it("空目录/无过期 → 空名单（不删任何文件）", () => {
    expect(pruneExpiredKernelLogs([], NOW)).toEqual([]);
    expect(pruneExpiredKernelLogs(["kernel.log.2026-10-29"], NOW)).toEqual([]);
  });

  it("保留期常量为 14（与提案 P-29c 建议值对账）", () => {
    expect(KERNEL_LOG_RETENTION_DAYS).toBe(14);
  });
});
