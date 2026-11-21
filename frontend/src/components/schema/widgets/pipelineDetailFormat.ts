/**
 * 任务详情面板省略格式化（P1-4 移动端视觉优化：UUID/ISO 时间戳满宽硬换行，
 * MuMu 实测 shots/02）。纯函数，渲染面与单测共用。
 *
 * - shortId：长 ID 显示前 8 位 + 省略号（完整值由点击复制承载，不进显示面）
 * - shortUuidsInText：复合文本里的 uuid / 长 hex 段收短（归属行 thread id）
 * - formatLocalMinute：ISO 时间戳 → 本地「YYYY-MM-DD HH:mm」；非法值原样透传
 */

/** ID 显示上限：前 8 位（审计口径：UUID 显示前 8 位） */
const SHORT_ID_LEN = 8

/** 长 ID（uuid 或 ≥12 位连续 hex）显示前 8 位 + 省略号；短值原样 */
export function shortId(value: string): string {
  if (!value || value.length <= SHORT_ID_LEN) return value
  return `${value.slice(0, SHORT_ID_LEN)}…`
}

const UUID_RE = /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/gi
/** 独立长 hex（前后不再接 hex 字符，防把已收短的尾部再次命中）：管道 12 位短 id */
const LONG_HEX_RE = /(?<![0-9a-f])[0-9a-f]{12,}(?![0-9a-f])/gi

/** 复合文本（如「会话名（thread-uuid）」）中的 uuid / 长 hex 段统一收短 */
export function shortUuidsInText(text: string): string {
  return text.replace(UUID_RE, (m) => shortId(m)).replace(LONG_HEX_RE, (m) => shortId(m))
}

/** 两位补零 */
function pad2(n: number): string {
  return n < 10 ? `0${n}` : String(n)
}

/**
 * ISO 时间戳 → 本地「YYYY-MM-DD HH:mm」。
 * 解析失败（非 ISO/空串）原样返回——详情面板不因脏数据丢行。
 */
export function formatLocalMinute(iso: string): string {
  if (!iso) return iso
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  return (
    `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())} ` +
    `${pad2(d.getHours())}:${pad2(d.getMinutes())}`
  )
}
