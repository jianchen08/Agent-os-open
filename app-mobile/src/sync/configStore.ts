/**
 * 配置应用面 —— 下行 config 资产的 LWW 账本 + 游标水位 + HLC 状态持久化。
 *
 * 与 sync_agent AgentStateStore 同构（ADR 2026-10-15-multi-device-sync-hlc-lww
 * D3/D4）：asset 账本记每资产现值裁决依据（hlc/version/ulid/hash），applied
 * 账本按 ULID 幂等（断网重拉零重复应用），cursor 为拉取断点（null=未自举）。
 * 应用内容落 lingxi_sync.config.<assetId>（app 侧配置镜像面；消费接线=下一刀）。
 */

import { compareHlc, hlcFromDict, type HlcTimestamp } from "./hlc";
import { KEYS, type KvStore } from "./storage";

export interface AssetRecord {
  readonly hlc: HlcTimestamp
  readonly version: number
  readonly ulid: string
  readonly contentHash: string
}

/** applied 账本容量上限（ULID 字典序=时间序，超限删最旧一批；量级=config 事件频度，低频）。 */
export const APPLIED_LEDGER_CAP = 2000;

export class ConfigStore {
  constructor(private readonly kv: KvStore) {}

  // ── 设备标识（跨配对保留：同一台设备）──────────────────────

  loadDeviceId(): string | null {
    return this.kv.get(KEYS.deviceId);
  }

  saveDeviceId(deviceId: string): void {
    this.kv.set(KEYS.deviceId, deviceId);
  }

  // ── HLC 时钟状态（跨页面生命周期单调续接）──────────────────

  loadHlcState(): readonly [number, number] | null {
    const raw = this.kv.get(KEYS.hlcState);
    if (!raw) return null;
    try {
      const value = JSON.parse(raw) as { physical_ms?: unknown; logical?: unknown };
      const physical = value.physical_ms;
      const logical = value.logical;
      if (
        typeof physical === "number" &&
        Number.isSafeInteger(physical) &&
        physical >= 0 &&
        typeof logical === "number" &&
        Number.isSafeInteger(logical) &&
        logical >= 0
      ) {
        return [physical, logical];
      }
    } catch {
      // 损坏状态按全新时钟起步（wall 钟接管，逻辑计数从零；持久化立即覆盖旧值）。
    }
    return null;
  }

  saveHlcState(physical: number, logical: number): void {
    this.kv.set(KEYS.hlcState, JSON.stringify({ physical_ms: physical, logical }));
  }

  // ── 游标水位（null = 未自举）───────────────────────────────

  loadCursor(): number | null {
    const raw = this.kv.get(KEYS.cursor);
    if (raw === null) return null;
    const value = Number(raw);
    return Number.isSafeInteger(value) && value >= 0 ? value : null;
  }

  saveCursor(cursor: number): void {
    this.kv.set(KEYS.cursor, String(cursor));
  }

  // ── 资产裁决账本（HLC-LWW 依据）────────────────────────────

  loadAsset(assetKey: string): AssetRecord | null {
    const raw = this.kv.get(KEYS.assetPrefix + assetKey);
    if (!raw) return null;
    try {
      const value = JSON.parse(raw) as Record<string, unknown>;
      const hlc = hlcFromDict(value.hlc);
      const version = value.version;
      const ulid = value.ulid;
      const contentHash = value.content_hash;
      if (
        typeof version === "number" &&
        typeof ulid === "string" &&
        typeof contentHash === "string"
      ) {
        return { hlc, version, ulid, contentHash };
      }
    } catch {
      // 账本记录损坏按无本地值处理：下一事件直接应用（fail-open 到 LWW 重放安全面）。
    }
    return null;
  }

  saveAsset(assetKey: string, record: AssetRecord): void {
    this.kv.set(
      KEYS.assetPrefix + assetKey,
      JSON.stringify({
        hlc: {
          physical_ms: record.hlc.physicalMs,
          logical: record.hlc.logical,
          device_id: record.hlc.deviceId,
        },
        version: record.version,
        ulid: record.ulid,
        content_hash: record.contentHash,
      }),
    );
  }

  assetCount(): number {
    return this.kv.keysWithPrefix(KEYS.assetPrefix).length;
  }

  // ── 已应用账本（ULID 幂等）─────────────────────────────────

  appliedOutcome(ulid: string): string | null {
    return this.kv.get(KEYS.appliedPrefix + ulid);
  }

  markApplied(ulid: string, outcome: string, appliedAtIso: string): void {
    this.kv.set(KEYS.appliedPrefix + ulid, JSON.stringify({ outcome, applied_at: appliedAtIso }));
    this.trimAppliedLedger();
  }

  appliedCount(): number {
    return this.kv.keysWithPrefix(KEYS.appliedPrefix).length;
  }

  private trimAppliedLedger(): void {
    const keys = this.kv.keysWithPrefix(KEYS.appliedPrefix).sort();
    const excess = keys.length - APPLIED_LEDGER_CAP;
    for (let i = 0; i < excess; i++) this.kv.remove(keys[i]!);
  }

  // ── 应用内容（app 侧配置镜像面）────────────────────────────

  loadConfigContent(assetId: string): string | null {
    return this.kv.get(KEYS.configPrefix + assetId);
  }

  saveConfigContent(assetId: string, content: string): void {
    this.kv.set(KEYS.configPrefix + assetId, content);
  }

  appliedConfigIds(): string[] {
    return this.kv
      .keysWithPrefix(KEYS.configPrefix)
      .map((key) => key.slice(KEYS.configPrefix.length))
      .sort();
  }

  // ── 配对状态作废（join 成功/unpair 同款；HLC 与 device_id 保留）──

  resetPairingState(): void {
    this.kv.remove(KEYS.cursor);
    this.kv.remove(KEYS.lastHeartbeat);
    for (const key of this.kv.keysWithPrefix(KEYS.assetPrefix)) this.kv.remove(key);
    for (const key of this.kv.keysWithPrefix(KEYS.appliedPrefix)) this.kv.remove(key);
    for (const key of this.kv.keysWithPrefix(KEYS.configPrefix)) this.kv.remove(key);
  }

  // ── 心跳痕迹（面板展示）────────────────────────────────────

  loadLastHeartbeat(): string | null {
    return this.kv.get(KEYS.lastHeartbeat);
  }

  saveLastHeartbeat(atIso: string): void {
    this.kv.set(KEYS.lastHeartbeat, atIso);
  }

  /** 单资产现值 HLC（面板/诊断便捷面；无记录 = null）。 */
  assetHlc(assetKey: string): HlcTimestamp | null {
    return this.loadAsset(assetKey)?.hlc ?? null;
  }

  /** LWW 单步裁决可见面（测试与诊断）：event 是否胜过本地现值。 */
  isEventStale(assetKey: string, eventHlc: HlcTimestamp): boolean {
    const local = this.loadAsset(assetKey);
    return local !== null && compareHlc(eventHlc, local.hlc) <= 0;
  }
}
