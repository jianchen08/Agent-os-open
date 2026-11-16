/**
 * ULID 幂等键生成 —— 同步事件主键（幂等去重），与 plugins/shared/ulid_keys.py 同构。
 *
 * ULID = 48bit UTC 毫秒 + 80bit 随机，Crockford Base32 26 字符，字典序即时间序；
 * 同一页面会话内同毫秒单调递增（随机段 +1），时钟回拨按上一毫秒继续发号，
 * 保证任意两枚 ULID 字典序不回退（at-least-once 上行的去重锚）。
 */

const ENCODING = "0123456789ABCDEFGHJKMNPQRSTVWXYZ";
export const ULID_LENGTH = 26;
const TIMESTAMP_BITS = 48;
const RANDOM_BITS = 80;
const MAX_RANDOM = 2n ** BigInt(RANDOM_BITS) - 1n;

/** Crockford 规范字母集（无 I/L/O/U），sync_event 校验共用。 */
export const ULID_CHARSET = ENCODING;

let lastMs = 0n;
let lastRandom = 0n;

function randomBits80(): bigint {
  const bytes = new Uint8Array(10);
  crypto.getRandomValues(bytes);
  let value = 0n;
  for (const byte of bytes) value = (value << 8n) | BigInt(byte);
  return value;
}

function encodeTime(ms: bigint): string {
  let time = ms;
  let out = "";
  for (let i = 0; i < 10; i++) {
    out = ENCODING[Number(time % 32n)] + out;
    time /= 32n;
  }
  return out;
}

function encodeRandom(rand: bigint): string {
  let value = rand;
  let out = "";
  for (let i = 0; i < 16; i++) {
    out = ENCODING[Number(value % 32n)] + out;
    value /= 32n;
  }
  return out;
}

/** 生成一枚 ULID（同会话同毫秒单调递增；回拨时沿用上一毫秒，字典序不回退）。 */
export function newUlid(nowMs: number = Date.now()): string {
  let ms = BigInt(Math.max(0, Math.trunc(nowMs))) & ((1n << BigInt(TIMESTAMP_BITS)) - 1n);
  if (ms <= lastMs) {
    ms = lastMs;
    if (lastRandom >= MAX_RANDOM) {
      // 随机段耗尽（单会话单毫秒 >2^80 次发号，现实不可达）：进位下一毫秒保持单调。
      ms = (ms + 1n) & ((1n << BigInt(TIMESTAMP_BITS)) - 1n);
      lastRandom = 0n;
    } else {
      lastRandom += 1n;
    }
  } else {
    lastRandom = randomBits80();
  }
  lastMs = ms;
  return encodeTime(ms) + encodeRandom(lastRandom);
}

/** 26 位大写 Crockford Base32 形态校验（sync_event 五必填的 ulid 分支共用）。 */
export function isValidUlid(raw: unknown): raw is string {
  return (
    typeof raw === "string" &&
    raw.length === ULID_LENGTH &&
    [...raw].every((ch) => ULID_CHARSET.includes(ch))
  );
}
