/**
 * sync_event 事件契约校验 —— 与 plugins/shared/sync_event.py::validate_event 同构移植（fail-closed）。
 *
 * 五必填（缺一即拒）：ulid / device_id / asset_type / version / updated_at；
 * 字段类型违约即拒。hlc 可选（协议宽容），但缺 HLC 的事件不可全序，merge 层
 * 恒拒收（裸墙钟 LWW 已被 ADR 否，见 syncEngine）。入网码端侧只整串透传不解释
 * （relay 签发/核销两端共同消费），故本模块不含编解码。
 */

import { hlcFromDict, type HlcTimestamp } from "./hlc";
import { ULID_LENGTH, ULID_CHARSET } from "./ulid";

export const REQUIRED_FIELDS = ["ulid", "device_id", "asset_type", "version", "updated_at"] as const;

/** 资产身份键（裁决与属主登记的粒度）。 */
export function assetKeyOf(assetType: string, assetId: string): string {
  return assetId ? `${assetType}/${assetId}` : assetType;
}

/** 校验通过后的不可变事件视图（cursor 由存储层发号，端侧只透传消费）。 */
export interface SyncEvent {
  readonly ulid: string
  readonly deviceId: string
  readonly assetType: string
  readonly assetId: string
  readonly version: number
  readonly updatedAt: string
  readonly payload: Record<string, unknown>
  readonly hlc: HlcTimestamp | null
  readonly cursor: number
}

/** 资产键便捷访问。 */
export function eventAssetKey(event: SyncEvent): string {
  return assetKeyOf(event.assetType, event.assetId);
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

/**
 * schema 校验：五必填缺一即拒 + 字段类型违约即拒（抛 RangeError，消息与
 * python 侧 validate_event 同口径）。缺失 = 键不存在或值为 null/undefined。
 */
export function validateEvent(raw: unknown): SyncEvent {
  if (!isPlainObject(raw)) throw new RangeError("sync_event 必须是对象");

  const missing = REQUIRED_FIELDS.filter((name) => raw[name] === null || raw[name] === undefined);
  if (missing.length > 0) {
    throw new RangeError(`sync_event 缺少必填字段: ${missing.join(", ")}`);
  }

  const ulidValue = raw.ulid;
  if (
    typeof ulidValue !== "string" ||
    ulidValue.length !== ULID_LENGTH ||
    [...ulidValue].some((ch) => !ULID_CHARSET.includes(ch))
  ) {
    throw new RangeError("ulid 必须是 26 位大写 Crockford Base32");
  }

  const deviceId = raw.device_id;
  if (typeof deviceId !== "string" || !deviceId) {
    throw new RangeError("device_id 必须是非空字符串");
  }
  const assetType = raw.asset_type;
  if (typeof assetType !== "string" || !assetType) {
    throw new RangeError("asset_type 必须是非空字符串");
  }
  const version = raw.version;
  if (typeof version === "boolean" || typeof version !== "number" || !Number.isSafeInteger(version) || version < 0) {
    throw new RangeError("version 必须是非负整数");
  }
  const updatedAt = raw.updated_at;
  if (typeof updatedAt !== "string" || !updatedAt) {
    throw new RangeError("updated_at 必须是非空字符串");
  }

  const assetId = raw.asset_id === undefined ? "" : raw.asset_id;
  if (typeof assetId !== "string") throw new RangeError("asset_id 必须是字符串");

  const payload = raw.payload === undefined ? {} : raw.payload;
  if (!isPlainObject(payload)) throw new RangeError("payload 必须是对象");

  const rawHlc = raw.hlc;
  if (rawHlc !== null && rawHlc !== undefined && !isPlainObject(rawHlc)) {
    // hlcFromDict 自带对象形态校验；数组等形态在此统一拒绝。
    throw new RangeError("hlc 必须是对象");
  }
  const hlc = rawHlc === null || rawHlc === undefined ? null : hlcFromDict(rawHlc);

  const rawCursor = raw.cursor === undefined ? 0 : raw.cursor;
  if (typeof rawCursor === "boolean" || typeof rawCursor !== "number" || !Number.isSafeInteger(rawCursor) || rawCursor < 0) {
    throw new RangeError("cursor 必须是非负整数");
  }

  return {
    ulid: ulidValue,
    deviceId,
    assetType,
    assetId,
    version,
    updatedAt,
    payload,
    hlc,
    cursor: rawCursor,
  };
}
