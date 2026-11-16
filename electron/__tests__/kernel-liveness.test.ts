// @feature: FP-0.2.可观测性 内核日志面判活消费 | @ci: frontend-test
/**
 * 内核日志判活消费测试（kernel-liveness.ts，修 G4 壳侧判活缺口）。
 *
 * 行为契约：
 *  - 解析：`<pid> <epoch_ms>` 两字段 → epoch ms；空/单字段/三字段/非有限数
 *    /负数 → null；
 *  - 评估三分支（fs 打桩）：标记存在且新鲜 → healthy；存在但过期 → 停摆
 *    （markerFound true + healthy false + 年龄实值）；缺失/不可解析 →
 *    markerFound false / lastOkWriteAgeMs null / healthy false（不误报）；
 *  - 轮询器：构造即先测一轮；停摆态跃迁才告警（同一停摆态不逐周期刷屏）；
 *    恢复后重新武装（再停摆可再告警）；stop() 后不再轮询；markerPath null
 *    = 路径不可知，不触 fs 恒报 unknown。
 *
 * fs 打桩注入；时钟用 fake Date（vi.advanceTimersByTime 同步推进定时器与
 * Date.now，轮询器默认消费 Date.now，无真 sleep）。
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  evaluateKernelLiveness,
  KERNEL_LIVENESS_STALE_THRESHOLD_MS,
  parseLivenessMarkerEpochMs,
  startKernelLivenessPoller,
} from "../kernel-liveness";

/** 钉死的「现在」（fake clock 起点） */
const NOW_MS = 1_800_000_000_000;

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date", "setInterval", "clearInterval"] });
  vi.setSystemTime(NOW_MS);
});

afterEach(() => {
  vi.useRealTimers();
});

describe("parseLivenessMarkerEpochMs", () => {
  it("两字段 <pid> <epoch_ms>（含换行尾）→ 解析出 epoch ms", () => {
    expect(parseLivenessMarkerEpochMs("12345 1800000000000\n")).toBe(1_800_000_000_000);
    expect(parseLivenessMarkerEpochMs("  7  42  ")).toBe(42);
  });

  it("空串/单字段/三字段/非数/负数 → null（不虚构时间戳）", () => {
    expect(parseLivenessMarkerEpochMs("")).toBeNull();
    expect(parseLivenessMarkerEpochMs("12345")).toBeNull();
    expect(parseLivenessMarkerEpochMs("1 2 3")).toBeNull();
    expect(parseLivenessMarkerEpochMs("1 not-a-number")).toBeNull();
    expect(parseLivenessMarkerEpochMs("1 -5")).toBeNull();
  });
});

describe("evaluateKernelLiveness（三分支，阈值注入）", () => {
  const thresholdMs = 600_000;

  it("标记存在且新鲜（年龄 < 阈值）→ healthy", () => {
    expect(
      evaluateKernelLiveness({
        markerContent: "1 1799999900000\n", // 距 now 100s
        nowMs: NOW_MS,
        staleThresholdMs: thresholdMs,
      }),
    ).toEqual({ markerFound: true, lastOkWriteAgeMs: 100_000, healthy: true });
  });

  it("标记存在但过期（年龄 > 阈值）→ 停摆且带实值年龄", () => {
    expect(
      evaluateKernelLiveness({
        markerContent: "1 1799999364000\n", // 距 now 636s > 600s
        nowMs: NOW_MS,
        staleThresholdMs: thresholdMs,
      }),
    ).toEqual({ markerFound: true, lastOkWriteAgeMs: 636_000, healthy: false });
  });

  it("阈值边界：年龄恰等于阈值 → 健康（≤ 判据，与内核 ms_since_ok_write 同口径）", () => {
    expect(
      evaluateKernelLiveness({
        markerContent: "1 1799999400000\n", // 距 now 恰 600s
        nowMs: NOW_MS,
        staleThresholdMs: thresholdMs,
      }),
    ).toEqual({ markerFound: true, lastOkWriteAgeMs: 600_000, healthy: true });
  });

  it("标记缺失（null）/内容不可解析 → markerFound false / age null / healthy false", () => {
    expect(
      evaluateKernelLiveness({ markerContent: null, nowMs: NOW_MS, staleThresholdMs: thresholdMs }),
    ).toEqual({ markerFound: false, lastOkWriteAgeMs: null, healthy: false });
    expect(
      evaluateKernelLiveness({
        markerContent: "corrupt",
        nowMs: NOW_MS,
        staleThresholdMs: thresholdMs,
      }),
    ).toEqual({ markerFound: false, lastOkWriteAgeMs: null, healthy: false });
  });
});

describe("startKernelLivenessPoller", () => {
  /** fs 打桩：路径 → 内容（缺失路径给 null） */
  function fakeFs(files: Record<string, string | null>) {
    return (path: string): string | null => files[path] ?? null;
  }

  const MARKER = "C:\\k\\logs\\kernel.liveness";

  it("构造即先测一轮：新鲜标记 → 首查快照即健康，无跃迁回调", () => {
    const transitions: unknown[] = [];
    const poller = startKernelLivenessPoller({
      markerPath: MARKER,
      readFile: fakeFs({ [MARKER]: `9 ${NOW_MS - 5_000}\n` }),
      onTransition: (snap) => transitions.push(snap),
    });
    expect(poller.snapshot()).toEqual({
      markerFound: true,
      lastOkWriteAgeMs: 5_000,
      healthy: true,
    });
    expect(transitions).toEqual([]);
    poller.stop();
  });

  it("新鲜 → 过期跃迁告警一次；持续停摆不逐周期刷屏（fake clock 推进多周期）", () => {
    const transitions: unknown[] = [];
    const files: Record<string, string | null> = { [MARKER]: `9 ${NOW_MS - 5_000}\n` };
    const poller = startKernelLivenessPoller({
      markerPath: MARKER,
      readFile: fakeFs(files),
      onTransition: (snap) => transitions.push(snap),
    });

    // 标记冻结（内核停写）：推 3 个周期（年龄 5s+180s=185s）仍新鲜
    vi.advanceTimersByTime(3 * 60_000);
    expect(poller.snapshot().healthy).toBe(true);
    // 再推 10 分钟（年龄 785s > 600s）→ 停摆，只在跃迁瞬间回调一次
    vi.advanceTimersByTime(10 * 60_000);
    expect(poller.snapshot()).toEqual({
      markerFound: true,
      lastOkWriteAgeMs: 785_000,
      healthy: false,
    });
    expect(transitions).toHaveLength(1);
    expect(transitions[0]).toMatchObject({ markerFound: true, healthy: false });

    // 继续停摆再推 5 个周期：无新回调（同一停摆态只告警一次）
    vi.advanceTimersByTime(5 * 60_000);
    expect(transitions).toHaveLength(1);
    poller.stop();
  });

  it("停摆 → 恢复 → 再停摆：恢复留痕且重新武装（第二次停摆可再告警）", () => {
    const transitions: unknown[] = [];
    // 初值即过期（距 now 700s）
    const files: Record<string, string | null> = { [MARKER]: `9 ${NOW_MS - 700_000}\n` };
    const poller = startKernelLivenessPoller({
      markerPath: MARKER,
      readFile: fakeFs(files),
      onTransition: (snap) => transitions.push(snap),
    });
    expect(poller.snapshot().healthy).toBe(false);
    expect(transitions).toHaveLength(1); // 起测一轮即停摆告警

    // 内核恢复写标记（新鲜）
    files[MARKER] = `9 ${Date.now()}\n`;
    vi.advanceTimersByTime(60_000);
    expect(transitions).toHaveLength(2);
    expect(transitions[1]).toMatchObject({ markerFound: true, healthy: true });

    // 再次停摆：重新武装后可再告警
    files[MARKER] = `9 ${Date.now() - 700_000}\n`;
    vi.advanceTimersByTime(60_000);
    expect(transitions).toHaveLength(3);
    expect(transitions[2]).toMatchObject({ markerFound: true, healthy: false });
    poller.stop();
  });

  it("标记缺失 → unknown 快照不告警；stop() 后不再轮询", () => {
    const transitions: unknown[] = [];
    const readSpy = vi.fn((_path: string) => null as string | null);
    const poller = startKernelLivenessPoller({
      markerPath: MARKER,
      readFile: readSpy,
      onTransition: (snap) => transitions.push(snap),
    });
    expect(poller.snapshot()).toEqual({
      markerFound: false,
      lastOkWriteAgeMs: null,
      healthy: false,
    });
    vi.advanceTimersByTime(3 * 60_000);
    expect(transitions).toEqual([]);
    expect(readSpy).toHaveBeenCalledTimes(4); // 起测 1 + 3 周期

    poller.stop();
    const callsAtStop = readSpy.mock.calls.length;
    vi.advanceTimersByTime(5 * 60_000);
    expect(readSpy).toHaveBeenCalledTimes(callsAtStop); // stop 后零调用
  });

  it("markerPath null（路径不可知）→ 恒 unknown 且不触 fs", () => {
    const readSpy = vi.fn((_path: string) => null as string | null);
    const poller = startKernelLivenessPoller({
      markerPath: null,
      readFile: readSpy,
    });
    vi.advanceTimersByTime(2 * 60_000);
    expect(poller.snapshot()).toEqual({
      markerFound: false,
      lastOkWriteAgeMs: null,
      healthy: false,
    });
    expect(readSpy).not.toHaveBeenCalled();
    poller.stop();
  });

  it("阈值常量与内核 log_liveness 生产阈值同值（600s 对齐契约）", () => {
    expect(KERNEL_LIVENESS_STALE_THRESHOLD_MS).toBe(600_000);
  });
});
