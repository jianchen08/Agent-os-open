// @feature: FP-0.2.三 宿主接入 | @ci: frontend-test
/**
 * 渲染器 JS 堆周期采样（electron/renderer-heap.ts）单测。
 *
 * 行为契约：
 *  - 回包解析只认 {used,total,limit} 三个有限非负数，其余一律 null（不虚构）；
 *  - 日志行字段名稳定（used/total/limit/used_limit），MB 取整、使用率一位小数；
 *  - 每个周期落一行；窗口已销毁跳过该拍；
 *  - 采样失败/API 缺失只留一条降级说明，持续失败不刷屏，恢复后重新武装；
 *  - stop() 之后不再产生日志。
 *
 * 定时器用虚拟时钟；executeJavaScript 是唯一外部边界（注入桩）。
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  RENDERER_HEAP_SAMPLE_INTERVAL_MS,
  formatRendererHeapLine,
  parseRendererHeapProbe,
  sampleRendererHeap,
  startRendererHeapSampling,
  type HeapProbeTarget,
  type HeapProbeWindow,
} from "../renderer-heap";

const MB = 1024 * 1024;

describe("parseRendererHeapProbe", () => {
  it("extracts finite non-negative bytes", () => {
    expect(parseRendererHeapProbe({ used: 1, total: 2, limit: 3 })).toEqual({
      usedBytes: 1,
      totalBytes: 2,
      limitBytes: 3,
    });
  });

  it("rejects missing/null/non-numeric/negative/NaN payloads", () => {
    expect(parseRendererHeapProbe(null)).toBeNull();
    expect(parseRendererHeapProbe(undefined)).toBeNull();
    expect(parseRendererHeapProbe("x")).toBeNull();
    expect(parseRendererHeapProbe({ used: 1, total: 2 })).toBeNull();
    expect(parseRendererHeapProbe({ used: "1", total: 2, limit: 3 })).toBeNull();
    expect(parseRendererHeapProbe({ used: -1, total: 2, limit: 3 })).toBeNull();
    expect(
      parseRendererHeapProbe({ used: Number.NaN, total: 2, limit: 3 }),
    ).toBeNull();
  });
});

describe("formatRendererHeapLine", () => {
  it("renders integer MB and one-decimal usage", () => {
    const line = formatRendererHeapLine({
      usedBytes: 512 * MB,
      totalBytes: 1024 * MB,
      limitBytes: 2048 * MB,
    });
    expect(line).toBe(
      "[renderer-heap] used=512MB total=1024MB limit=2048MB used_limit=25.0%",
    );
  });

  it("degrades usage to ? when limit is zero (no division by zero)", () => {
    const line = formatRendererHeapLine({
      usedBytes: 0,
      totalBytes: 0,
      limitBytes: 0,
    });
    expect(line).toContain("used_limit=?%");
  });
});

describe("sampleRendererHeap", () => {
  it("passes the probe source to the renderer and parses the reply", async () => {
    const target: HeapProbeTarget = {
      executeJavaScript: vi.fn(async () => ({ used: 3, total: 4, limit: 5 })),
    };
    await expect(sampleRendererHeap(target)).resolves.toEqual({
      usedBytes: 3,
      totalBytes: 4,
      limitBytes: 5,
    });
    expect(target.executeJavaScript).toHaveBeenCalledTimes(1);
  });
});

describe("startRendererHeapSampling", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  function makeWindow(
    execute: () => Promise<unknown>,
    destroyed = false,
  ): HeapProbeWindow & { executeJavaScript: ReturnType<typeof vi.fn> } {
    const executeJavaScript = vi.fn(execute);
    return {
      isDestroyed: () => destroyed,
      webContents: { executeJavaScript },
      executeJavaScript,
    };
  }

  it("logs one line per interval and stops after stop()", async () => {
    const win = makeWindow(async () => ({
      used: 100 * MB,
      total: 200 * MB,
      limit: 400 * MB,
    }));
    const log = vi.fn();
    const stop = startRendererHeapSampling(win, log);

    await vi.advanceTimersByTimeAsync(RENDERER_HEAP_SAMPLE_INTERVAL_MS);
    expect(log).toHaveBeenCalledTimes(1);
    expect(log).toHaveBeenCalledWith(
      "[renderer-heap] used=100MB total=200MB limit=400MB used_limit=25.0%",
    );

    await vi.advanceTimersByTimeAsync(RENDERER_HEAP_SAMPLE_INTERVAL_MS);
    expect(log).toHaveBeenCalledTimes(2);

    stop();
    await vi.advanceTimersByTimeAsync(RENDERER_HEAP_SAMPLE_INTERVAL_MS * 3);
    expect(log).toHaveBeenCalledTimes(2);
  });

  it("skips the tick when the window is destroyed", async () => {
    const win = makeWindow(async () => ({ used: 1, total: 2, limit: 3 }), true);
    const log = vi.fn();
    startRendererHeapSampling(win, log);

    await vi.advanceTimersByTimeAsync(RENDERER_HEAP_SAMPLE_INTERVAL_MS * 2);
    expect(log).not.toHaveBeenCalled();
    expect(win.executeJavaScript).not.toHaveBeenCalled();
  });

  it("logs a single degraded line while the probe keeps failing, then re-arms on success", async () => {
    let shouldFail = true;
    const win = makeWindow(async () => {
      if (shouldFail) throw new Error("execution context destroyed");
      return { used: 1, total: 2, limit: 3 };
    });
    const log = vi.fn();
    startRendererHeapSampling(win, log);

    await vi.advanceTimersByTimeAsync(RENDERER_HEAP_SAMPLE_INTERVAL_MS);
    expect(log).toHaveBeenCalledTimes(1);
    expect(log.mock.calls[0][0]).toContain("[renderer-heap] 采样失败");

    // 持续失败不再刷屏
    await vi.advanceTimersByTimeAsync(RENDERER_HEAP_SAMPLE_INTERVAL_MS * 2);
    expect(log).toHaveBeenCalledTimes(1);

    // 恢复后正常落行，且后续再次失败会重新留一条降级说明
    shouldFail = false;
    await vi.advanceTimersByTimeAsync(RENDERER_HEAP_SAMPLE_INTERVAL_MS);
    expect(log).toHaveBeenCalledTimes(2);
    expect(log.mock.calls[1][0]).toContain("used=0MB");

    shouldFail = true;
    await vi.advanceTimersByTimeAsync(RENDERER_HEAP_SAMPLE_INTERVAL_MS);
    expect(log).toHaveBeenCalledTimes(3);
    expect(log.mock.calls[2][0]).toContain("[renderer-heap] 采样失败");
  });

  it("logs a single degraded line when performance.memory is unavailable", async () => {
    const win = makeWindow(async () => null);
    const log = vi.fn();
    startRendererHeapSampling(win, log);

    await vi.advanceTimersByTimeAsync(RENDERER_HEAP_SAMPLE_INTERVAL_MS * 2);
    expect(log).toHaveBeenCalledTimes(1);
    expect(log.mock.calls[0][0]).toContain("performance.memory 缺失");
  });
});
