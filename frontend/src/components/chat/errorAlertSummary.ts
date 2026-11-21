/**
 * 错误提示摘要短语（P1-6 移动端视觉优化：整屏红字错误折叠为一行摘要）。
 *
 * 摘要规则（能省略的一律省略，优化原则 3）：取首个分隔符前的人类可读短语——
 * 首个中文冒号「：」或「": "」（ASCII 冒号后跟空白；不带空白跟随的冒号
 * （如 URL 的 https://）不是分隔符，防把网址劈成「https」）之前的片段；
 * 前片段过短（<2 字符，说明首段不是短语）或无分隔符时回退整条消息截断。
 * 截断上限 40 字符（超长加省略号）。全文仍可经「详情」展开查看，摘要只
 * 承担一行可扫读的错误类型短语。
 */

/** 摘要最大字符数（超长截断加省略号） */
const SUMMARY_MAX_CHARS = 40

/** 在首个分隔符（中文冒号 / ": "）处切成短语候选；无分隔符返回 null */
function firstSegmentBeforeSeparator(message: string): string | null {
  const cjkIndex = message.indexOf('：')
  const asciiIndex = message.indexOf(': ')
  const index =
    cjkIndex >= 0 && (asciiIndex < 0 || cjkIndex < asciiIndex) ? cjkIndex : asciiIndex
  if (index < 0) return null
  return message.slice(0, index)
}

/** 截断到上限并加省略号（已短于上限原样返回） */
function truncate(text: string): string {
  if (text.length <= SUMMARY_MAX_CHARS) return text
  return `${text.slice(0, SUMMARY_MAX_CHARS)}…`
}

/**
 * 从完整错误文案提炼一行摘要短语。
 * - 「语音转文字失败：ASR 转写失败（…）:local: …」→「语音转文字失败」
 * - 「x: 本地模型下载失败 (https://…)」→「x」过短（<2 字符）回退截断整条
 *   （≥2 的「local: …」直接取「local」）
 * - 无分隔符（「文件上传失败」）→ 原样（超长才截断）
 */
export function summarizeErrorMessage(message: string): string {
  const firstLine = message.split('\n')[0]?.trim() ?? ''
  if (!firstLine) return ''
  const segment = firstSegmentBeforeSeparator(firstLine)
  if (segment && segment.trim().length >= 2) return truncate(segment.trim())
  return truncate(firstLine)
}
