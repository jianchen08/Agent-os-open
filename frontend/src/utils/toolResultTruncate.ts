/**
 * 工具结果内存截断（数据面瘦身批次 1.2，方案 docs/working/前端内存优化方案_20261009.md）
 *
 * 工具输出全文常驻消息桶是长会话内存大头：写入面统一把超阈值（8KB，低于内核
 * spill_guard 的 10KB，保证被截的都有可拉回的全量）的 result/resultData 替换为
 * 「预览(头 2KB) + 截断标记」；展示面遇标记显示预览，用户展开时经
 * expandTruncatedToolResult（toolResultBackfill.ts）按 sequence 回取全量回填。
 *
 * 本模块是纯函数：无 store/网络依赖，写入面（session.ts 映射 / toolHandler 流式
 * 写入）与展示面（useMessageRender / MessageItem）共用，不各写一套。
 */
import type { ActivityDetailBlock } from '@/types/activity'

/** 截断标记对象（替换超限原值写入 store） */
export interface TruncatedToolResult {
  __truncated: true
  /** 序列化后的前 previewBytes 字节预览 */
  preview: string
  /** 原值序列化后的总字节长度 */
  totalBytes: number
  /** 原消息 sequence（展开回填按 after_sequence=sequence-1 & limit=1 精确取单条） */
  sequence?: number
}

/** 截断阈值（字节）：低于内核 spill_guard 10KB，保证 8-10KB 区间回取可得全量 */
export const TOOL_RESULT_TRUNCATE_THRESHOLD = 8 * 1024
/** 预览保留字节数 */
export const TOOL_RESULT_PREVIEW_BYTES = 2 * 1024

/** 判定值是否为截断标记对象 */
export function isTruncatedToolResult(value: unknown): value is TruncatedToolResult {
  return (
    typeof value === 'object' &&
    value !== null &&
    (value as { __truncated?: unknown }).__truncated === true &&
    typeof (value as { preview?: unknown }).preview === 'string'
  )
}

/** 序列化尺寸：字符串取 length，其余 JSON 序列化长度（序列化失败按 0 处理不截断） */
function serializedSize(value: unknown): number | null {
  if (typeof value === 'string') return value.length
  try {
    return JSON.stringify(value)?.length ?? null
  } catch {
    return null
  }
}

/**
 * 超限工具结果截断（写入面唯一入口）。
 *
 * - 体积（字符串 length / 对象 JSON.stringify 长）≤ threshold → 原样返回（同一引用）
 * - 超限 → 返回标记对象 { __truncated, preview: 前 previewBytes, totalBytes, sequence }
 * - undefined/null 或序列化失败 → 原样返回
 */
export function truncateToolResultData(
  resultData: unknown,
  sequence: number | undefined,
  threshold: number = TOOL_RESULT_TRUNCATE_THRESHOLD,
  previewBytes: number = TOOL_RESULT_PREVIEW_BYTES,
): unknown {
  if (resultData === undefined || resultData === null) return resultData
  const size = serializedSize(resultData)
  if (size === null || size <= threshold) return resultData
  const serialized = typeof resultData === 'string' ? resultData : JSON.stringify(resultData) as string
  return {
    __truncated: true,
    preview: serialized.slice(0, previewBytes),
    totalBytes: size,
    sequence,
  } satisfies TruncatedToolResult
}

/**
 * 展示面标记解析：把（可能为标记的）result/resultData 换成可安全进渲染分支的
 * 值——标记字段剔除（渲染分支按数据形状路由，标记对象进去会产出垃圾视图），
 * 截断信息收敛到 mark，由调用方拼「已截断」详情块（buildTruncatedResultDetailBlock）。
 *
 * 双字段独立判定：仅一侧截断时保留另一侧真实值（渲染分支优先 resultData，
 * 真实 resultData 在场时预览无信息量）。
 */
export function resolveTruncatedToolResultForDisplay(
  result: unknown,
  resultData: unknown,
): { result: unknown; resultData: unknown; mark?: TruncatedToolResult } {
  let mark: TruncatedToolResult | undefined
  let r = result
  let rd = resultData
  if (isTruncatedToolResult(rd)) {
    mark = rd
    rd = undefined
  }
  if (isTruncatedToolResult(r)) {
    mark = mark ?? r
    r = undefined
  }
  return { result: r, resultData: rd, mark }
}

/**
 * 「已截断结果」详情块：预览收在可折叠块内，label 如实标注总量；
 * onExpand 由展开回填接线（首次展开触发单条回取，见 toolResultBackfill.ts）。
 */
export function buildTruncatedResultDetailBlock(
  mark: TruncatedToolResult,
  onExpand: () => Promise<boolean>,
): ActivityDetailBlock {
  const kb = mark.totalBytes / 1024
  const sizeLabel = kb >= 1024 ? `${(kb / 1024).toFixed(1)} MB` : `${Math.max(kb, 0.1).toFixed(1)} KB`
  return {
    id: 'result-truncated',
    label: `结果已截断（共 ${sizeLabel}），展开加载全文`,
    content: mark.preview,
    contentType: 'text',
    collapsible: true,
    defaultExpanded: false,
    onExpand,
  }
}
