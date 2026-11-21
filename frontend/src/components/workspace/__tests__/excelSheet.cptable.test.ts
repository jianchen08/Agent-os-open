/** @feature FP-0.2.四 声明驱动查看器路由 | @ci: frontend-test */
/**
 * excelSheet — codepage 装载行为断言（审查 MF-1 修复的 Red 锚）
 *
 * MF-1：registry 声明 .xls 命中 excel_preview，但浏览器解析入口从不装载
 * codepage 表——旧式 8-bit ANSI 编码 .xls 的非 ASCII 文本静默错值。
 * 修复契约：OLE2（.xls BIFF）解析前必须装载 codepage 表（cpexcel）；
 * zip（.xlsx/.xlsm）路径零增量、不触发装载。
 *
 * 本文件 mock 两个包入口（外部依赖边界，testing_rules §6.3 允许）：
 * - cpexcel.full.mjs → 哨兵命名空间（断言 set_cptable 收到的对象）
 * - @e965/xlsx → 真身 spread + set_cptable 换 spy（utils 真身保留，
 *   zip 用例仍用真包构造/解析）
 *
 * 真包行为用例（OLE2 往返正确性）在 excelSheet.test.ts，两文件互不污染。
 */
import * as XLSX from '@e965/xlsx'
import { describe, expect, it, vi } from 'vitest'
import { parseExcelWorkbook } from '@/components/workspace/excelSheet'

const setCptableSpy = vi.hoisted(() => vi.fn())

vi.mock('@e965/xlsx/dist/cpexcel.full.mjs', () => ({
  cptable: { __cpexcelSentinel: true },
}))

vi.mock('@e965/xlsx', async (importOriginal) => {
  const actual = await importOriginal()
  return { ...actual, set_cptable: setCptableSpy }
})

describe('parseExcelWorkbook — codepage 装载行为（MF-1 修复契约）', () => {
  it('OLE2（.xls）输入触发 codepage 表装载；zip（.xlsx）输入零装载', async () => {
    // zip 路径先行断言：真包构造 xlsx，解析成功且不触发装载
    const wbSrc = XLSX.utils.book_new()
    XLSX.utils.book_append_sheet(wbSrc, XLSX.utils.aoa_to_sheet([['h'], ['v']]), 's')
    const zipBytes = new Uint8Array(XLSX.write(wbSrc, { bookType: 'xlsx', type: 'array' }) as ArrayBuffer)
    const wbZip = await parseExcelWorkbook(zipBytes)
    expect(wbZip.sheets[0]!.rows).toEqual([['h'], ['v']])
    expect(setCptableSpy).not.toHaveBeenCalled()

    // OLE2 路径：魔数（D0 CF 11 E0 A1 B1 1A E1）+ 非法 BIFF 内容——
    // 装载必须发生在解析之前（spy 收到 cpexcel 哨兵命名空间）；
    // 解析本体对垃圾内容显式抛错（错误传播语义不变）
    const ole2Garbage = new Uint8Array(64)
    ole2Garbage.set([0xd0, 0xcf, 0x11, 0xe0, 0xa1, 0xb1, 0x1a, 0xe1], 0)
    ole2Garbage.fill(0x42, 8)
    await expect(parseExcelWorkbook(ole2Garbage)).rejects.toThrow()
    expect(setCptableSpy).toHaveBeenCalledTimes(1)
    expect(setCptableSpy).toHaveBeenCalledWith(
      expect.objectContaining({ cptable: expect.objectContaining({ __cpexcelSentinel: true }) }),
    )
  })
})
