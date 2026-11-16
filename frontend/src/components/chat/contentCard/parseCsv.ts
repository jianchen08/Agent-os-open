/**
 * CSV 解析（content_cards csv_table 内置指令的数据面，ADR 2026-10-14-content-card-registry §4）。
 *
 * RFC4180 简化：双引号包裹字段、字段内 `""` 转义引号、CRLF/CR/LF 行界。
 * 单元格全部按文本产出（消费侧 textContent 注入 DOM，天然转义，零注入面）。
 */

/** 解析 CSV 文本为行数组（空输入 → 空数组；末行无换行符照常成行） */
export function parseCsv(text: string): string[][] {
  const src = String(text ?? '').replace(/\r\n/g, '\n').replace(/\r/g, '\n')
  if (src === '') return []
  const rows: string[][] = []
  let row: string[] = []
  let field = ''
  let inQuotes = false
  let i = 0
  while (i < src.length) {
    const ch = src.charAt(i)
    if (inQuotes) {
      if (ch === '"') {
        if (src.charAt(i + 1) === '"') {
          field += '"'
          i += 2
          continue
        }
        inQuotes = false
        i += 1
        continue
      }
      field += ch
      i += 1
      continue
    }
    if (ch === '"') {
      inQuotes = true
      i += 1
      continue
    }
    if (ch === ',') {
      row.push(field)
      field = ''
      i += 1
      continue
    }
    if (ch === '\n') {
      row.push(field)
      rows.push(row)
      row = []
      field = ''
      i += 1
      continue
    }
    field += ch
    i += 1
  }
  if (field !== '' || row.length > 0) {
    row.push(field)
    rows.push(row)
  }
  return rows
}
