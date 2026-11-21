/**
 * 工具结果的「可读文本」抽取（现场展开区用，纯函数）
 *
 * 设备类工具（Windows-MCP 的 Snapshot/Screenshot、phone_control 的 tree）返回的是
 * 内嵌结构：内核 MCP 结果统一归一为 `{success, output:{text?, images?}}`
 * （`kernel/crates/invoker/src/invoker/mcp_result.rs`），插件工具则可能把文本放在
 * `result.text` / `result.data` / 裸字符串里。
 *
 * 通用 JSON 折叠块只列字段名，看不到元素树正文——而现场页要的正是正文（
 * 方案 §3.3.3 桌面 Snapshot / 手机 tree 的可内嵌展示项）。故本模块按已知形状
 * 取正文，取不到返回 undefined（调用方不加文本块，不造假内容）。
 */

/**
 * 从工具结果里抽正文文本。
 *
 * 判定顺序（命中即返回）：
 * 1. 裸字符串结果（部分插件直接回文本）
 * 2. `text` 字段（trim 后非空）
 * 3. `output.text`（内核 MCP 归一形态）
 * 4. `data` 字符串（phone_control 的 tree 结果）
 * 5. `snapshot_text`（浏览器快照字段）
 * 6. `content` 字符串
 */
export function extractResultText(result: unknown): string | undefined {
  if (typeof result === 'string') {
    const trimmed = result.trim()
    return trimmed === '' ? undefined : result
  }
  if (typeof result !== 'object' || result === null) return undefined
  const record = result as Record<string, unknown>
  const direct = nonEmptyString(record.text)
  if (direct) return direct
  const output = record.output
  if (typeof output === 'object' && output !== null) {
    const outputText = nonEmptyString((output as Record<string, unknown>).text)
    if (outputText) return outputText
  }
  const data = nonEmptyString(record.data)
  if (data) return data
  const snapshot = nonEmptyString(record.snapshot_text)
  if (snapshot) return snapshot
  const content = nonEmptyString(record.content)
  if (content) return content
  // 桌面/手机元素树的直挂字段（部分实现把树放 tree/snapshot 而非 text）
  const tree = nonEmptyString(record.tree) ?? nonEmptyString(record.snapshot)
  if (tree) return tree
  return undefined
}

function nonEmptyString(value: unknown): string | undefined {
  if (typeof value !== 'string') return undefined
  return value.trim() === '' ? undefined : value
}
