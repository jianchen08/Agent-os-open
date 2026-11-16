/**
 * HLC 混合逻辑时钟 —— 多端同步全序可比时间戳，与 plugins/shared/hlc_clock.py 同构移植。
 *
 * 语义（ADR docs/decisions/2026-10-15-multi-device-sync-hlc-lww.md D2/D3）：
 * - 全序 = (physical_ms, logical, device_id) 字典序，任意两枚恒可比，LWW 恒有唯一胜者；
 * - 单调：同一实例发出的时间戳严格递增（wall 钟回拨由逻辑计数器兜底）；
 * - 保因果：observe 过远端戳后发出的事件严格大于该远端值。
 * 算法 = Kulkarni et al. Hybrid Logical Clocks（issue=now / observe 两式）；
 * 状态 (physical, logical) 持久化后经 initial 恢复，跨页面生命周期单调续接。
 */

/** 一枚 HLC 时间戳（发出去的戳不可变）。 */
export interface HlcTimestamp {
  readonly physicalMs: number
  readonly logical: number
  readonly deviceId: string
}

/** 全序比较：负=a<b，0=相等，正=a>b。device_id 为同物理同逻辑毫秒的决胜位。 */
export function compareHlc(a: HlcTimestamp, b: HlcTimestamp): number {
  if (a.physicalMs !== b.physicalMs) return a.physicalMs - b.physicalMs;
  if (a.logical !== b.logical) return a.logical - b.logical;
  if (a.deviceId < b.deviceId) return -1;
  if (a.deviceId > b.deviceId) return 1;
  return 0;
}

/** 线格式（sync_event.hlc 字段载荷）。 */
export function hlcToDict(stamp: HlcTimestamp): { physical_ms: number; logical: number; device_id: string } {
  return { physical_ms: stamp.physicalMs, logical: stamp.logical, device_id: stamp.deviceId };
}

function assertNonNegativeInt(value: unknown, name: string): number {
  if (typeof value === "boolean" || typeof value !== "number" || !Number.isSafeInteger(value) || value < 0) {
    throw new RangeError(`${name} 必须是非负安全整数`);
  }
  return value;
}

/** 线格式解析；形态违约抛 RangeError（fail-closed，与 syncEvent 校验同口径）。 */
export function hlcFromDict(raw: unknown): HlcTimestamp {
  if (raw === null || typeof raw !== "object" || Array.isArray(raw)) {
    throw new RangeError("hlc 必须是对象");
  }
  const record = raw as Record<string, unknown>;
  const physical = assertNonNegativeInt(record.physical_ms, "hlc.physical_ms");
  const logical = assertNonNegativeInt(record.logical, "hlc.logical");
  const device = record.device_id;
  if (typeof device !== "string" || !device) {
    throw new RangeError("hlc.device_id 必须是非空字符串");
  }
  return { physicalMs: physical, logical, deviceId: device };
}

export interface HybridLogicalClockOptions {
  /** 物理毫秒钟（测试注入 fake clock 断言时序不变量；运行态默认 wall clock）。 */
  clockMs?: () => number
  /** 持久化恢复的 (physical_ms, logical)（跨重启单调续接）；缺省从零起步。 */
  initial?: readonly [number, number]
}

/** 每端一个的 HLC 状态对象（引擎自持）。 */
export class HybridLogicalClock {
  private physical: number;
  private logical: number;
  private readonly clockMs: () => number;
  readonly deviceId: string;

  constructor(deviceId: string, options: HybridLogicalClockOptions = {}) {
    if (!deviceId) throw new RangeError("HLC 需要 device_id（全序决胜位，不允许空）");
    this.deviceId = deviceId;
    this.clockMs = options.clockMs ?? (() => Date.now());
    this.physical = options.initial ? options.initial[0] : 0;
    this.logical = options.initial ? options.initial[1] : 0;
  }

  /** 当前 (physical_ms, logical)（持久化恢复用）。 */
  get state(): readonly [number, number] {
    return [this.physical, this.logical];
  }

  /** 本地事件打戳（严格大于本实例此前发出的任何一枚）。 */
  now(): HlcTimestamp {
    const pt = this.clockMs();
    if (pt > this.physical) {
      this.physical = pt;
      this.logical = 0;
    } else {
      this.logical += 1;
    }
    return { physicalMs: this.physical, logical: this.logical, deviceId: this.deviceId };
  }

  /** 观察远端时间戳后推进本地状态（保因果）：返回值 > remote 且 > 本实例此前任何状态。 */
  observe(remote: HlcTimestamp): HlcTimestamp {
    const pt = this.clockMs();
    const physical = Math.max(this.physical, remote.physicalMs, pt);
    if (physical === this.physical && physical === remote.physicalMs) {
      this.logical = Math.max(this.logical, remote.logical) + 1;
    } else if (physical === this.physical) {
      this.logical = this.logical + 1;
    } else if (physical === remote.physicalMs) {
      this.logical = remote.logical + 1;
    } else {
      this.logical = 0;
    }
    this.physical = physical;
    return { physicalMs: this.physical, logical: this.logical, deviceId: this.deviceId };
  }
}
