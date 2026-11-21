/**
 * Excel 工作簿解析（纯函数）
 *
 * 选型真值 = `docs/working/971600b7ff1e_查看器选型与双链方案.md` §1.2 Excel 节：
 * 解析用 `@e965/xlsx`（SheetJS 社区镜像，Apache-2.0；官方 npm `xlsx` 停更 4 年
 * 不采用），表格渲染由 FilePreview excel 分支自绘（报告口径：只读预览 + 前 N 行，
 * 自绘成本可控）。
 *
 * 职责边界：只做「字节 → AOA 二维数组（显示值形态）+ 截断元数据」；不感知
 * React、不取流（取流归 FilePreview loadPreviewBytes）。
 */

import * as XLSX from '@e965/xlsx'

/** 单 sheet 渲染行数上限（超限截断 + truncated 标记，防大表卡死渲染面） */
export const EXCEL_PREVIEW_MAX_ROWS = 200

/** 单 sheet 解析结果（单元格恒为字符串显示值；锯齿行已 pad 对齐） */
export interface ExcelSheetData {
  name: string
  rows: string[][]
  /** 工作簿内真实总行数（截断时 > rows.length，如实上报不掩盖） */
  totalRows: number
  truncated: boolean
}

export interface ExcelWorkbook {
  sheets: ExcelSheetData[]
}

export interface ParseExcelOptions {
  /** 行数上限（缺省 EXCEL_PREVIEW_MAX_ROWS） */
  maxRows?: number
}

/** codepage 表装载标记：OLE2 解析前惰性装载一次（进程内单次），zip 路径零装载 */
let cptableLoaded = false

/**
 * OLE2（.xls BIFF）解析前装载 codepage 表（审查 MF-1 修复）。
 *
 * 为什么必须装：xlsx.mjs 的 ESM 入口不带 codepage 表，BIFF 的 8-bit ANSI
 * 字符串（cp1252/936 等单字节编码）会走 UTF-8 回退解码——非 ASCII 文本
 * 静默错值（实测 '中文' → '__' 占位符，无任何告警）。装载 cpexcel 后
 * decode 走真实码表。
 *
 * 为什么 zip 不装：.xlsx/.xlsm 共享字符串是 UTF-8 XML，不依赖码表——
 * 不装载则主 bundle 与本 chunk 均零增量。
 *
 * 传参形态：set_cptable 收完整模块命名空间（cpexcel.full.mjs 同名导出
 * `.cptable` 与 `.utils`，xlsx.mjs 内部 `$cptable.utils.decode` 消费的
 * 是命名空间级 utils；传内层 `.cptable` 会因缺 utils 崩溃）。
 */
async function ensureCodepageTable(): Promise<void> {
  if (cptableLoaded) return
  const cpt = await import('@e965/xlsx/dist/cpexcel.full.mjs')
  XLSX.set_cptable(cpt)
  cptableLoaded = true
}

/**
 * Excel 二进制魔数校验 + 容器判别：xlsx/xlsm = zip 容器（PK 头），xls =
 * OLE2 复合文档（D0 CF 11 E0 A1 B1 1A E1）。
 *
 * 为什么不能省：SheetJS 解析宽容——非 zip 的纯文本字节不抛错，而是按 CSV/文本
 * 表格解析「成功」，损坏内容会被渲染成假表格（静默错误行为）。查看器语义
 * 是「看起来像原文件」，损坏输入必须显式报错，魔数校验把它转成显式错误。
 */
function detectExcelContainer(bytes: Uint8Array): 'zip' | 'ole2' {
  const isZip =
    bytes.length >= 4 &&
    bytes[0] === 0x50 &&
    bytes[1] === 0x4b &&
    (bytes[2] === 0x03 || bytes[2] === 0x05 || bytes[2] === 0x07)
  const isOle2 =
    bytes.length >= 8 &&
    bytes[0] === 0xd0 &&
    bytes[1] === 0xcf &&
    bytes[2] === 0x11 &&
    bytes[3] === 0xe0 &&
    bytes[4] === 0xa1 &&
    bytes[5] === 0xb1 &&
    bytes[6] === 0x1a &&
    bytes[7] === 0xe1
  if (!isZip && !isOle2) {
    throw new Error('不是有效的 Excel 文件（缺少 zip/OLE2 文件头，可能已损坏）')
  }
  return isZip ? 'zip' : 'ole2'
}

/**
 * 解析 xlsx/xlsm/xls 字节为各 sheet 二维数组（async：OLE2 需先装载 codepage）。
 *
 * - 容器判别先行：非 Excel 二进制（纯文本/损坏内容）显式抛错，绝不降级成
 *   假表格（SheetJS 的文本宽容解析在此场景是错误行为）；
 * - OLE2（.xls）解析前惰性装载 codepage 表（审查 MF-1：无表时 8-bit ANSI
 *   字符串静默错值）；zip 路径零装载零增量；
 * - `raw: false`：单元格取显示值（数字/日期格式化、公式取缓存值），不输出
 *   公式文本——查看器语义是「看起来像打开的表」；
 * - 其余解析错误（zip 结构损坏、BIFF 记录流损坏等）XLSX.read 抛错原样传播
 *   （错误是值，不吞）。
 */
export async function parseExcelWorkbook(
  bytes: Uint8Array,
  options?: ParseExcelOptions,
): Promise<ExcelWorkbook> {
  const container = detectExcelContainer(bytes)
  if (container === 'ole2') {
    await ensureCodepageTable()
  }
  const maxRows = options?.maxRows ?? EXCEL_PREVIEW_MAX_ROWS
  const wb = XLSX.read(bytes, { type: 'array' })
  const sheets: ExcelSheetData[] = wb.SheetNames.map((name) => {
    const ws = wb.Sheets[name]
    const aoa = XLSX.utils.sheet_to_json<unknown[]>(ws, {
      header: 1,
      raw: false,
      defval: '',
    })
    const totalRows = aoa.length
    const capped = aoa.slice(0, maxRows)
    let width = 0
    for (const row of capped) {
      if (Array.isArray(row) && row.length > width) width = row.length
    }
    const rows = capped.map((row) => {
      const out: string[] = []
      for (let c = 0; c < width; c += 1) {
        const cell = Array.isArray(row) ? row[c] : undefined
        out.push(cell === undefined || cell === null ? '' : String(cell))
      }
      return out
    })
    return { name, rows, totalRows, truncated: totalRows > rows.length }
  })
  return { sheets }
}
