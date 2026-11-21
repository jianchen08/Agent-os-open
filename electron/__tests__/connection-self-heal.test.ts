// @feature: FP-0.2.可观测性 连接池毒化自愈（装机事故批次 2） | @ci: frontend-test
/**
 * 连接池毒化自愈测试（connection-self-heal.ts，2026-11-18 装机事故批次 2）。
 *
 * 行为契约：
 *  - 决策三分支：重连次数不足 → skip；内核不健康 → skip（红线：内核真挂
 *    不重建）；冷却窗口内 → skip（限频红线，带 retryInMs）；三者皆过 →
 *    rebuild；
 *  - 请求处理：载荷校验 fail-closed（非法 reconnectAttempts 不探活不重建）；
 *    探活健康 → closeAllConnections + notifyRebuilt({rebuiltAt})；探活失败
 *    → 不重建（kernel-unhealthy）；首次重建后冷却内再请求 → 不重建，
 *    冷却过期 → 可再重建（限频窗口滑动正确）；
 *  - IPC 两端字段对齐：请求/广播通道常量单源（main.ts 与 preload.ts 同
 *    import 本模块），载荷字段 reconnectAttempts / rebuiltAt 与类型契约一致。
 *
 * 副作用全打桩（probe/closeAll/notify/时钟注入），不 import electron。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  createConnectionSelfHeal,
  decideSelfHeal,
  SELF_HEAL_COOLDOWN_MS,
  SELF_HEAL_PROBE_TIMEOUT_MS,
  SELF_HEAL_REBUILT_CHANNEL,
  SELF_HEAL_REQUEST_CHANNEL,
  SELF_HEAL_TRIGGER_ATTEMPTS,
} from "../connection-self-heal";

const NOW_MS = 1_800_000_000_000;

describe("decideSelfHeal 决策三分支", () => {
  it("重连次数不足阈值 → skip（正常退避期偶发掉线不重建）", () => {
    const d = decideSelfHeal({
      reconnectAttempts: SELF_HEAL_TRIGGER_ATTEMPTS - 1,
      kernelHealthy: true,
      lastHealAtMs: null,
      nowMs: NOW_MS,
    });
    expect(d).toEqual({ action: "skip", reason: "attempts-below-threshold" });
  });

  it("内核不健康 → skip（红线：内核真挂时重建无意义）", () => {
    const d = decideSelfHeal({
      reconnectAttempts: SELF_HEAL_TRIGGER_ATTEMPTS,
      kernelHealthy: false,
      lastHealAtMs: null,
      nowMs: NOW_MS,
    });
    expect(d).toEqual({ action: "skip", reason: "kernel-unhealthy" });
  });

  it("冷却窗口内 → skip 且带 retryInMs（限频红线：5 分钟内至多一次）", () => {
    const lastHealAtMs = NOW_MS - (SELF_HEAL_COOLDOWN_MS - 60_000);
    const d = decideSelfHeal({
      reconnectAttempts: SELF_HEAL_TRIGGER_ATTEMPTS,
      kernelHealthy: true,
      lastHealAtMs,
      nowMs: NOW_MS,
    });
    expect(d).toEqual({
      action: "skip",
      reason: "cooldown-active",
      retryInMs: 60_000,
    });
  });

  it("次数达标 + 内核健康 + 从未重建 → rebuild", () => {
    const d = decideSelfHeal({
      reconnectAttempts: SELF_HEAL_TRIGGER_ATTEMPTS,
      kernelHealthy: true,
      lastHealAtMs: null,
      nowMs: NOW_MS,
    });
    expect(d).toEqual({ action: "rebuild" });
  });

  it("冷却窗口已过（≥5 分钟）→ rebuild（窗口滑动正确）", () => {
    const d = decideSelfHeal({
      reconnectAttempts: SELF_HEAL_TRIGGER_ATTEMPTS * 3,
      kernelHealthy: true,
      lastHealAtMs: NOW_MS - SELF_HEAL_COOLDOWN_MS,
      nowMs: NOW_MS,
    });
    expect(d).toEqual({ action: "rebuild" });
  });
});

describe("handleSelfHealRequest 请求处理", () => {
  function build(healthOk: boolean) {
    const probeHealth = vi.fn(async () => healthOk);
    const closeAllConnections = vi.fn();
    const notifyRebuilt = vi.fn();
    let nowMs = NOW_MS;
    const heal = createConnectionSelfHeal({
      healthUrl: "http://127.0.0.1:9101/health",
      probeHealth,
      closeAllConnections,
      notifyRebuilt,
      now: () => nowMs,
      logInfo: () => {},
    });
    return {
      heal,
      probeHealth,
      closeAllConnections,
      notifyRebuilt,
      advance: (ms: number) => {
        nowMs += ms;
      },
    };
  }

  it("载荷非法（attempts 非数/缺失/负数）→ invalid-payload，不探活不重建", async () => {
    for (const payload of [null, {}, { reconnectAttempts: "9" }, { reconnectAttempts: -1 }]) {
      const t = build(true);
      const result = await t.heal.handleSelfHealRequest(payload);
      expect(result).toEqual({ rebuilt: false, reason: "invalid-payload" });
      expect(t.probeHealth).not.toHaveBeenCalled();
      expect(t.closeAllConnections).not.toHaveBeenCalled();
    }
  });

  it("探活健康 → closeAllConnections + notifyRebuilt（rebuiltAt ISO 载荷）", async () => {
    const t = build(true);
    const result = await t.heal.handleSelfHealRequest({
      reconnectAttempts: SELF_HEAL_TRIGGER_ATTEMPTS,
    });
    expect(result).toEqual({ rebuilt: true });
    expect(t.probeHealth).toHaveBeenCalledWith(
      "http://127.0.0.1:9101/health",
      SELF_HEAL_PROBE_TIMEOUT_MS,
    );
    expect(t.closeAllConnections).toHaveBeenCalledTimes(1);
    expect(t.notifyRebuilt).toHaveBeenCalledTimes(1);
    const payload = t.notifyRebuilt.mock.calls[0][0] as { rebuiltAt: string };
    expect(typeof payload.rebuiltAt).toBe("string");
    expect(Number.isNaN(Date.parse(payload.rebuiltAt))).toBe(false);
  });

  it("探活失败 → 不重建（kernel-unhealthy，红线：内核真挂不 closeAll）", async () => {
    const t = build(false);
    const result = await t.heal.handleSelfHealRequest({
      reconnectAttempts: SELF_HEAL_TRIGGER_ATTEMPTS,
    });
    expect(result).toEqual({ rebuilt: false, reason: "kernel-unhealthy" });
    expect(t.closeAllConnections).not.toHaveBeenCalled();
    expect(t.notifyRebuilt).not.toHaveBeenCalled();
  });

  it("冷却限频：重建后 5 分钟内再请求不重建，窗口过期可再重建", async () => {
    const t = build(true);
    const payload = { reconnectAttempts: SELF_HEAL_TRIGGER_ATTEMPTS };
    expect(await t.heal.handleSelfHealRequest(payload)).toEqual({ rebuilt: true });

    t.advance(SELF_HEAL_COOLDOWN_MS - 60_000);
    const second = await t.heal.handleSelfHealRequest({
      reconnectAttempts: SELF_HEAL_TRIGGER_ATTEMPTS * 2,
    });
    expect(second).toEqual({ rebuilt: false, reason: "cooldown-active" });
    expect(t.closeAllConnections).toHaveBeenCalledTimes(1);

    t.advance(60_000);
    const third = await t.heal.handleSelfHealRequest({
      reconnectAttempts: SELF_HEAL_TRIGGER_ATTEMPTS * 3,
    });
    expect(third).toEqual({ rebuilt: true });
    expect(t.closeAllConnections).toHaveBeenCalledTimes(2);
  });

  it("lastHealAtMs 诊断面：重建后记录时刻，初始为 null", async () => {
    const t = build(true);
    expect(t.heal.lastHealAtMs()).toBeNull();
    await t.heal.handleSelfHealRequest({ reconnectAttempts: SELF_HEAL_TRIGGER_ATTEMPTS });
    expect(t.heal.lastHealAtMs()).toBe(NOW_MS);
  });
});

describe("IPC 通道常量（两端字段对齐单源）", () => {
  beforeEach(() => {
    // 通道名即 IPC 契约：main.ts 与 preload.ts 同 import 本模块常量，改名即测试红
  });

  it("请求/广播通道名稳定且成对", () => {
    expect(SELF_HEAL_REQUEST_CHANNEL).toBe("net:self-heal:request");
    expect(SELF_HEAL_REBUILT_CHANNEL).toBe("net:self-heal:rebuilt");
  });
});
