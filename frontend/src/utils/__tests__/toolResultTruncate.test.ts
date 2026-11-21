/** @feature FP-T12 前端适配（工具结果瘦身纯函数） @vision V6 可即用 @ci frontend-test */
/**
 * 工具结果截断纯函数测试（数据面瘦身批次 1.2）
 *
 * 断行为：阈值两侧（小原样/大截断标记）、字符串与对象两形态、恰在边界、
 * 展示面解析的字段取舍。不 mock 内部依赖（纯函数无外部依赖）。
 */
import { describe, it, expect } from 'vitest'
import {
  TOOL_RESULT_PREVIEW_BYTES,
  TOOL_RESULT_TRUNCATE_THRESHOLD,
  buildTruncatedResultDetailBlock,
  isTruncatedToolResult,
  resolveTruncatedToolResultForDisplay,
  truncateToolResultData,
} from '../toolResultTruncate'

const SEQ = 42

describe('truncateToolResultData：阈值两侧', () => {
  it('小对象（远低于阈值）→ 原样返回（同一引用）', () => {
    const small = { status: 'ok', rows: [1, 2, 3] }
    expect(truncateToolResultData(small, SEQ)).toBe(small)
  })

  it('大对象（超阈值）→ 截断标记：preview 为序列化前 2KB、totalBytes 为全量序列化长、sequence 透传', () => {
    const big = { blob: 'x'.repeat(TOOL_RESULT_TRUNCATE_THRESHOLD * 2) }
    const out = truncateToolResultData(big, SEQ)
    expect(isTruncatedToolResult(out)).toBe(true)
    const mark = out as { __truncated: true; preview: string; totalBytes: number; sequence?: number }
    expect(mark.__truncated).toBe(true)
    expect(mark.sequence).toBe(SEQ)
    expect(mark.preview.length).toBe(TOOL_RESULT_PREVIEW_BYTES)
    expect(mark.preview).toBe(JSON.stringify(big).slice(0, TOOL_RESULT_PREVIEW_BYTES))
    expect(mark.totalBytes).toBe(JSON.stringify(big).length)
    expect(mark.totalBytes).toBeGreaterThan(TOOL_RESULT_TRUNCATE_THRESHOLD)
  })

  it('短字符串 → 原样；长字符串 → 标记（preview 取字符串前 2KB）', () => {
    const short = '短结果'
    expect(truncateToolResultData(short, SEQ)).toBe(short)

    const long = 'y'.repeat(TOOL_RESULT_TRUNCATE_THRESHOLD + 100)
    const mark = truncateToolResultData(long, SEQ) as { preview: string; totalBytes: number }
    expect(mark.preview).toBe(long.slice(0, TOOL_RESULT_PREVIEW_BYTES))
    expect(mark.totalBytes).toBe(long.length)
  })

  it('恰在阈值边界：= threshold 原样，threshold+1 截断', () => {
    const at = 'z'.repeat(TOOL_RESULT_TRUNCATE_THRESHOLD)
    expect(truncateToolResultData(at, SEQ)).toBe(at)

    const over = 'z'.repeat(TOOL_RESULT_TRUNCATE_THRESHOLD + 1)
    expect(isTruncatedToolResult(truncateToolResultData(over, SEQ))).toBe(true)
  })

  it('undefined/null → 原样返回；自定义阈值/预览参数生效', () => {
    expect(truncateToolResultData(undefined, SEQ)).toBeUndefined()
    expect(truncateToolResultData(null, SEQ)).toBeNull()

    const v = 'a'.repeat(50)
    expect(truncateToolResultData(v, SEQ, 40, 10)).toEqual({
      __truncated: true,
      preview: 'a'.repeat(10),
      totalBytes: 50,
      sequence: SEQ,
    })
  })

  it('标记对象本身再次进入截断（重复写入面）→ 幂等不二次包裹预览语义', () => {
    const mark = truncateToolResultData('b'.repeat(9000), SEQ)
    // 写入面幂等：已截断的标记不会被二次序列化放大（标记体积小于阈值原样透传）
    expect(truncateToolResultData(mark, SEQ)).toBe(mark)
  })
})

describe('isTruncatedToolResult', () => {
  it('仅「__truncated === true 且 preview 为字符串」的命中；普通对象/字符串/null 不命中', () => {
    expect(isTruncatedToolResult({ __truncated: true, preview: 'p', totalBytes: 1 })).toBe(true)
    expect(isTruncatedToolResult({ __truncated: false, preview: 'p' })).toBe(false)
    expect(isTruncatedToolResult({ __truncated: true })).toBe(false)
    expect(isTruncatedToolResult({ status: 'ok' })).toBe(false)
    expect(isTruncatedToolResult('plain')).toBe(false)
    expect(isTruncatedToolResult(null)).toBe(false)
  })
})

describe('resolveTruncatedToolResultForDisplay：展示面字段取舍', () => {
  const mark = (seq: number | undefined = SEQ) => ({
    __truncated: true as const,
    preview: '预览'.repeat(100),
    totalBytes: 99999,
    sequence: seq,
  })

  it('双侧截断 → 双侧剔除，mark 收敛（渲染分支不再见标记对象）', () => {
    const r = resolveTruncatedToolResultForDisplay(mark(), mark())
    expect(r.mark).toEqual(mark())
    expect(r.result).toBeUndefined()
    expect(r.resultData).toBeUndefined()
  })

  it('仅 resultData 截断、result 真实 → 保留真实 result，剔除标记 resultData', () => {
    const real = '真实短结果'
    const r = resolveTruncatedToolResultForDisplay(real, mark())
    expect(r.mark).toEqual(mark())
    expect(r.result).toBe(real)
    expect(r.resultData).toBeUndefined()
  })

  it('仅 result 截断、resultData 真实 → 保留真实 resultData（渲染分支优先结构化）', () => {
    const realData = { diff: { added: 3, removed: 1 } }
    const r = resolveTruncatedToolResultForDisplay(mark(), realData)
    expect(r.mark).toEqual(mark())
    expect(r.result).toBeUndefined()
    expect(r.resultData).toBe(realData)
  })

  it('无标记 → 原样透传（同一引用）且无 mark', () => {
    const r1 = { a: 1 }
    const d1 = { b: 2 }
    const out = resolveTruncatedToolResultForDisplay(r1, d1)
    expect(out.result).toBe(r1)
    expect(out.resultData).toBe(d1)
    expect(out.mark).toBeUndefined()
  })
})

describe('buildTruncatedResultDetailBlock：截断详情块', () => {
  it('缺省静态：label 标注「与 LLM 所见一致」、content 为预览、无 onExpand（不回取全文）', () => {
    const block = buildTruncatedResultDetailBlock(
      { __truncated: true, preview: 'PREVIEW', totalBytes: 20 * 1024, sequence: 7 },
    )
    expect(block.id).toBe('result-truncated')
    expect(block.label).toContain('20.0 KB')
    expect(block.label).toContain('与 LLM 所见一致')
    expect(block.label).not.toContain('展开加载全文')
    expect(block.content).toBe('PREVIEW')
    expect(block.collapsible).toBe(true)
    expect(block.defaultExpanded).toBe(false)
    expect(block.onExpand).toBeUndefined()
  })

  it('接线 onExpand 时原样保留（卡型自管语义，如 file_card 自带 label）', async () => {
    const onExpand = async () => true
    const block = buildTruncatedResultDetailBlock(
      { __truncated: true, preview: 'PREVIEW', totalBytes: 20 * 1024, sequence: 7 },
      onExpand,
      '差异对比（共 20.0 KB），展开加载全文',
    )
    expect(block.label).toContain('展开加载全文')
    expect(block.onExpand).toBe(onExpand)
    await expect(block.onExpand!()).resolves.toBe(true)
  })

  it('超大结果 label 以 MB 计', () => {
    const block = buildTruncatedResultDetailBlock(
      { __truncated: true, preview: 'p', totalBytes: 3 * 1024 * 1024, sequence: undefined },
      async () => true,
    )
    expect(block.label).toContain('3.0 MB')
  })
})
