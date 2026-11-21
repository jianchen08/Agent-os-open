/**
 * 工具结果展开回填（数据面瘦身批次 1.2 的读取半边）。
 *
 * 写入面（session.ts 映射 / toolHandler）把超阈值结果截断为「预览+标记」；
 * 用户展开截断块时按标记携带的 sequence 单条回取全量消息并回填 store——
 * 内核列表端点无单条详情端点，但 after_sequence=seq-1 & limit=1 可精确取
 * 单条（游标语义见 kernel store.rs）。回取经 fetchMessages 的
 * includeFullToolResults 旁路截断映射；取回的全量 tool 消息按 id 去重替换
 * 桶内截断版（appendMessages 增量语义），parts 路径再经 updatePart 把全量
 * 结果写回 assistant 的 tool_call part。
 *
 * 防重契约：每次展开至多一次网络请求——在途请求共享同一 Promise，已成功
 * 回填的键不再发请求；失败不落已回填标记（下次展开可重试），由调用方
 * 如实显示失败态（保留预览，禁止假装已全文）。
 */
import { usePipelineMessageStore } from '@/stores/pipelineMessageStore'
import { loggers } from '@/utils/logger'
import type { ToolCallPart } from '@/types/messageParts'

const logger = loggers.sessionStore

export interface ExpandToolResultParams {
  /** 承载 tool_call part 的 assistant 消息 id（独立 tool 消息卡场景与 messageId 无关） */
  messageId?: string
  /** tool_call part 下标（parts 路径回填目标；独立 tool 消息卡缺省） */
  partIndex?: number
  /** 工具调用 id（参与防重键） */
  callId?: string
  /** 截断标记携带的原消息 sequence（缺省=无法定位，如实失败） */
  sequence?: number
}

/** 在途回取（同键共享同一 Promise，保证单飞） */
const _inflight = new Map<string, Promise<boolean>>()
/** 已成功回填的键（不再发请求） */
const _backfilled = new Set<string>()

function expandKey(p: ExpandToolResultParams): string {
  return `${p.messageId ?? ''}:${p.callId ?? ''}:${p.partIndex ?? ''}`
}

/**
 * 展开截断的工具结果：单条回取全量并回填。返回 true=全量已进 store；
 * false=失败（sequence 缺失/请求失败/取回消息对不上），调用方保留预览并
 * 显示错误态。
 */
export async function expandTruncatedToolResult(p: ExpandToolResultParams): Promise<boolean> {
  const key = expandKey(p)
  if (_backfilled.has(key)) return true
  const existing = _inflight.get(key)
  if (existing) return existing
  const task = doExpand(key, p).finally(() => _inflight.delete(key))
  _inflight.set(key, task)
  return task
}

async function doExpand(key: string, p: ExpandToolResultParams): Promise<boolean> {
  const store = usePipelineMessageStore.getState()
  const pipelineId = store.activePipelineId
  if (!pipelineId || p.sequence === undefined) return false
  const threadId = store.pipelineSessionMap[pipelineId]
  if (!threadId) return false
  try {
    await store.fetchMessages(pipelineId, {
      threadId,
      after_sequence: p.sequence - 1,
      limit: 1,
      includeFullToolResults: true,
    })
  } catch (err) {
    logger.warn('[toolResultBackfill] 单条回取失败: seq=%s err=%s', p.sequence, String(err))
    return false
  }
  // 取回的全量 tool 消息（fetch 已按 id 去重替换桶内截断版；对不上号=游标
  // 漂移等协议异常，如实失败不硬凑）
  const msgs = usePipelineMessageStore.getState().getMessages(pipelineId)
  const full = msgs.find((m) => m.role === 'tool' && m.sequence === p.sequence)
  if (!full) return false
  // 取回内容为空（服务端 spill/映射缺口等导致 toolResult 缺失，含空串兜底）→
  // 回填失败：绝不用空值顶掉已有预览——顶掉后 hasResult 变 false，卡头会从
  // completed 翻成「已中断（无结果）」（2026-11-18 装机用户实测展开即中断）。
  // 失败不落已回填标记，保留预览下次展开可重试（与失败态同一口径）。
  if (!full.toolResult && !full.toolResultData) {
    logger.warn(
      '[toolResultBackfill] 全量消息无工具结果（seq=%s msg=%s）——保留预览不回填',
      p.sequence,
      String(full.id ?? ''),
    )
    return false
  }
  if (p.partIndex !== undefined && p.messageId && p.partIndex >= 0) {
    usePipelineMessageStore.getState().updatePart(pipelineId, p.messageId, p.partIndex, {
      result: full.toolResult,
      resultData: full.toolResultData,
    } satisfies Partial<ToolCallPart>)
  }
  _backfilled.add(key)
  return true
}

/** 测试专用：清空防重状态（模块级集合跨用例残留会伪绿/伪红） */
export function _resetToolResultBackfillForTest(): void {
  _inflight.clear()
  _backfilled.clear()
}
