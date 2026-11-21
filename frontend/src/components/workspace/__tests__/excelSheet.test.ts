/** @feature FP-0.2.四 声明驱动查看器路由 | @ci: frontend-test */
/**
 * excelSheet（Excel 工作簿解析，纯函数）单元测试
 *
 * 解析链路走真实依赖：用 @e965/xlsx 真包构造 xlsx/biff 字节 → parseExcelWorkbook
 * 真包读回，零 mock（防拟合——实现无法靠硬编码期望值刷绿，换输入仍正确）。
 *
 * 覆盖：
 * - zip 容器（.xlsx）：多 sheet / 截断 / 锯齿行 / 空 sheet / 公式显示值
 * - OLE2 容器（.xls BIFF）：biff8 中文往返（UTF-16LE 字符串，.xls 主流形态）+
 *   biff5 ASCII 往返（Excel 5.0/95 老格式）
 * - 坏字节（非 Excel 魔数）显式抛错（parseExcelWorkbook 为 async，错误走
 *   rejected promise——消费方 await 语义统一）
 *
 * codepage 装载断言（OLE2 触发 set_cptable / zip 不触发）独立在
 * excelSheet.cptable.test.ts（该文件需 mock 包入口，与本真包文件隔离）。
 */
import * as XLSX from '@e965/xlsx'
import { describe, expect, it } from 'vitest'
import {
  EXCEL_PREVIEW_MAX_ROWS,
  parseExcelWorkbook,
} from '@/components/workspace/excelSheet'

/** 用真包构造工作簿字节（写入 → 读回全真链路） */
function buildBytes(
  sheets: Array<{ name: string; aoa: unknown[][] }>,
  bookType: 'xlsx' | 'biff8' | 'biff5' = 'xlsx',
): Uint8Array {
  const wb = XLSX.utils.book_new()
  for (const { name, aoa } of sheets) {
    XLSX.utils.book_append_sheet(wb, XLSX.utils.aoa_to_sheet(aoa), name)
  }
  const out = XLSX.write(wb, { bookType, type: 'array' }) as ArrayBuffer
  return new Uint8Array(out)
}

describe('parseExcelWorkbook — zip 容器（.xlsx）解析与截断', () => {
  it('多 sheet 工作簿：sheet 名单保序，各 sheet 产出字符串二维数组', async () => {
    const bytes = buildBytes([
      { name: '成绩', aoa: [['姓名', '分数'], ['小明', '95'], ['小红', '88']] },
      { name: '说明', aoa: [['备注'], ['样例数据']] },
    ])
    const wb = await parseExcelWorkbook(bytes)
    expect(wb.sheets.map((s) => s.name)).toEqual(['成绩', '说明'])
    expect(wb.sheets[0]!.rows).toEqual([
      ['姓名', '分数'],
      ['小明', '95'],
      ['小红', '88'],
    ])
    expect(wb.sheets[0]!.truncated).toBe(false)
    expect(wb.sheets[0]!.totalRows).toBe(3)
    // 数字单元格以显示值形态输出（字符串）
    expect(typeof wb.sheets[0]!.rows[1]![1]).toBe('string')
  })

  it('行数截断：超过 maxRows 只保留前 maxRows 行，totalRows 如实、truncated=true', async () => {
    const bigAoa: unknown[][] = [['列A', '列B']]
    for (let i = 1; i <= EXCEL_PREVIEW_MAX_ROWS + 30; i += 1) {
      bigAoa.push([`r${i}`, i])
    }
    const bytes = buildBytes([{ name: '大表', aoa: bigAoa }])
    const wb = await parseExcelWorkbook(bytes)
    const sheet = wb.sheets[0]!
    expect(sheet.rows).toHaveLength(EXCEL_PREVIEW_MAX_ROWS)
    expect(sheet.truncated).toBe(true)
    expect(sheet.totalRows).toBe(EXCEL_PREVIEW_MAX_ROWS + 31)
    // 截断保留的是前缀（第一行表头仍在）
    expect(sheet.rows[0]).toEqual(['列A', '列B'])
  })

  it('maxRows 可覆盖：自定义阈值生效（第二组有区分度输入）', async () => {
    const bytes = buildBytes([{ name: 's', aoa: [['a'], ['b'], ['c'], ['d']] }])
    const wb = await parseExcelWorkbook(bytes, { maxRows: 2 })
    expect(wb.sheets[0]!.rows).toEqual([['a'], ['b']])
    expect(wb.sheets[0]!.truncated).toBe(true)
    expect(wb.sheets[0]!.totalRows).toBe(4)
  })

  it('锯齿行：短行 pad 空串到统一列数（表格渲染不塌列）', async () => {
    const bytes = buildBytes([{ name: 's', aoa: [['h1', 'h2', 'h3'], ['only', 'two']] }])
    const wb = await parseExcelWorkbook(bytes)
    expect(wb.sheets[0]!.rows[1]).toEqual(['only', 'two', ''])
  })

  it('空 sheet：rows 为空数组，不抛错', async () => {
    const bytes = buildBytes([{ name: '空白', aoa: [] }])
    const wb = await parseExcelWorkbook(bytes)
    expect(wb.sheets[0]!.name).toBe('空白')
    expect(wb.sheets[0]!.rows).toEqual([])
    expect(wb.sheets[0]!.truncated).toBe(false)
  })
})

describe('parseExcelWorkbook — OLE2 容器（.xls BIFF）', () => {
  it('biff8（Excel 97-2003 主流形态）中文往返：UTF-16LE 字符串正确读回', async () => {
    // 性质断言：非 ASCII 内容经 OLE2 解析路径不静默错值
    // （审查 MF-1 场景：无表时 8-bit ANSI 路径曾产出 "__" 占位）
    const bytes = buildBytes([{ name: '数据', aoa: [['名称', '数值'], ['中文内容', '42']] }], 'biff8')
    const wb = await parseExcelWorkbook(bytes)
    expect(wb.sheets[0]!.rows).toEqual([
      ['名称', '数值'],
      ['中文内容', '42'],
    ])
  })

  it('biff5（Excel 5.0/95）ASCII 往返：OLE2 解析路径可用', async () => {
    // ASCII 是 ANSI 编码子集——验证老格式解析链路本身工作；
    // 非 ASCII biff5 的写入侧缺陷属上游问题（见实施报告遗留节），读取侧
    // codepage 保障由 OLE2 装载路径承担（excelSheet.cptable.test.ts 断言装载行为）
    const bytes = buildBytes([{ name: 'sheet1', aoa: [['h1', 'h2'], ['v1', '42']] }], 'biff5')
    const wb = await parseExcelWorkbook(bytes)
    expect(wb.sheets[0]!.rows).toEqual([
      ['h1', 'h2'],
      ['v1', '42'],
    ])
  })
})

describe('parseExcelWorkbook — 异常与显示值', () => {
  it('坏字节（非 Excel 魔数）显式抛错，不吞异常', async () => {
    const garbage = new TextEncoder().encode('这不是 xlsx，只是普通文本\uFFFD\uFFFD')
    await expect(parseExcelWorkbook(garbage)).rejects.toThrow()
  })

  it('空字节输入同样抛错（错误是值，不静默返回空结果）', async () => {
    await expect(parseExcelWorkbook(new Uint8Array(0))).rejects.toThrow()
  })

  it('公式单元格取显示值（raw:false），不输出公式文本', async () => {
    const wbSrc = XLSX.utils.book_new()
    const ws = XLSX.utils.aoa_to_sheet([[1, 2]])
    ws['C1'] = { t: 'n', f: 'A1+B1', v: 3 }
    // 手工注入的公式单元格在 aoa_to_sheet 的默认 !ref（A1:B1）之外，扩到 C 列
    // 才会被 sheet_to_json 读到
    ws['!ref'] = 'A1:C1'
    XLSX.utils.book_append_sheet(wbSrc, ws, 'calc')
    const out = XLSX.write(wbSrc, { bookType: 'xlsx', type: 'array' }) as ArrayBuffer
    const wb = await parseExcelWorkbook(new Uint8Array(out))
    expect(wb.sheets[0]!.rows[0]).toEqual(['1', '2', '3'])
  })
})
