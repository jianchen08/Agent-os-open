/**
 * StreamLiveLine 取文口径（从组件文件抽出的纯函数，react-refresh 单组件文件
 * 契约：组件文件只出组件——棘轮清偿 2026-11-19，原导出随抽随迁）。
 *
 * - 取文（运行中）= 最后一行非空文本：按 `\r\n?/` 归一后 split('\n')，
 *   从末尾回溯首个 `trim()` 非空行（ZCode cTn 口径）。
 * - 取文（终态）= 首行非空文本（DSH firstLine 口径）。
 */

/** 取最后一行非空文本（ZCode cTn / DSH latestLine 同口径） */
export function latestLine(text: string): string {
  const lines = text.replace(/\r\n?/gu, '\n').split('\n')
  for (let i = lines.length - 1; i >= 0; i--) {
    const line = lines[i]?.trim() ?? ''
    if (line.length > 0) return line
  }
  return ''
}

/** 取首行非空文本（DSH firstLine 同口径） */
export function firstLine(text: string): string {
  const lines = text.replace(/\r\n?/gu, '\n').split('\n')
  for (const line of lines) {
    const trimmed = line.trim()
    if (trimmed.length > 0) return trimmed
  }
  return ''
}
