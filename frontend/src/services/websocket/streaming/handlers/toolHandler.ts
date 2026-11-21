/** 工具调用事件处理器（start / result） 仅使用 parts[] 统一路径，已移除旧 toolCalls / contentBlocks 兼容代码。 */
import { usePipelineMessageStore as pipelineStore } from '@/stores/pipelineMessageStore'
import { loggers } from '@/utils/logger'
import { truncateToolResultData } from '@/utils/toolResultTruncate'
import { resolvePipelineId } from '../router'
import { flushStreamChunkBuffer } from './streamHandler'
import {
  backfillAuthoritativeSequence,
  ensureMessageSlot,
  ensureStreamingPlaceholder,
  extractMessageId,
  extractThreadId,
} from './utils'
import type { ToolCallPart } from '@/types/messageParts'

const _debugLogger = loggers.websocket

/** 宿主消息权威槽位：只读 payload 内层 data.sequence（入口占位行 / 轮次槽位，
 *  与 stream_start.sequence 同源）。事件信封顶层 sequence 是事件总线计数器，
 *  严禁当消息序消费（ADR 2026-11-06-round-entry-slot-placeholder）。 */
function extractEventSequence(eventData: any): number | undefined {
  const seq = eventData?.data?.sequence
  return typeof seq === 'number' && Number.isFinite(seq) ? seq : undefined
}

/** tool_result 缺 success 字段的告警节流（避免流式高频刷屏；30s 至多一条） */
let _lastMissingSuccessWarnAt = 0

/**
 * 解析 tool_result 的终态：缺 success 且 error 非空 → error（fail-closed）；
 * success 缺失时不再默认成功（后端契约漂移会让失败工具在 UI 呈现为成功），
 * 两者皆缺按 done 渲染但节流 warn 一条，契约漂移可观测。
 */
function resolveToolResultState(
  success: unknown,
  error: unknown,
  toolName: string,
): 'done' | 'error' {
  if (success !== undefined) {
    return success === false ? 'error' : 'done'
  }
  if (error !== null && error !== undefined && error !== '') {
    return 'error'
  }
  const now = Date.now()
  if (now - _lastMissingSuccessWarnAt > 30_000) {
    _lastMissingSuccessWarnAt = now
    _debugLogger.warn(
      '[TOOL_RESULT] success/error 字段均缺失，按 done 渲染（后端契约漂移？）: tool=%s',
      toolName,
    )
  }
  return 'done'
}

/**
 * 同管道内跨消息按 callId 搜既有声明卡，返回其宿主消息 id；找不到返回 null。
 *
 * 融合迭代管道（tool_exec 步先于 llm 步、post 路由不写 core_type）下，第 N 轮
 * LLM 声明的工具调用在 N+1 轮迭代开头执行，契约事件 message_id 全部 = a_{N+1}
 * （晚一轮）——按事件 message_id 直接寻址必然 miss。callId 是唯一稳定身份
 * （provider id 或 call_<hex24>，跨消息唯一），消息只是容器：直接寻址 miss 时
 * 以 callId 跨消息归并到声明卡（贯彻 ADR 2026-09-28-tool-call-live-card 单键归并）。
 * 数据面单管道持久化上限 250 条，全量遍历成本可忽略，且只走直接 miss 的罕见路径。
 */
function findToolCallMessageId(pipelineId: string, callId: string): string | null {
  const msgs = pipelineStore.getState().getMessages(pipelineId)
  for (const msg of msgs) {
    if (msg.parts?.some((p) => p.type === 'tool_call' && p.callId === callId)) {
      return msg.id
    }
  }
  return null
}

/** 处理工具调用开始事件 向 parts[] 追加一个 tool_call part；若 call_id 缺失则跳过（等数据完整再渲染）。 */
export function handleToolStart(eventData: any) {
  const pipelineId = resolvePipelineId(eventData)
  if (!pipelineId) return
  const messageId = extractMessageId(eventData)
  if (!messageId) return

  // 先冲刷缓冲的增量：tool_start 是 part 结构边界变更点（与 block_end / stream_end 同类），
  // 必须在变更前把 RAF 缓冲里的正文 delta 落到当前 text part。否则本函数会把当前
  // streaming text part 置为 done，随后 RAF flush 时 findStreamingPartIndex 找不到
  // streaming text part，会新建 text part 追加到 tool_call 之后 → 文本被劈到工具后面，
  // 多轮交错时表现为「思考-工具-文本-文本-思考」错乱。
  flushStreamChunkBuffer()

  const callId = eventData.call_id || eventData.data?.call_id
  // 没有 call_id 无法唯一定位和去重，跳过等数据完整
  if (!callId) {
    _debugLogger.debug(
      `[TOOL_START] skipped (no call_id): msgId=%s pipelineId=%s`,
      messageId?.slice(0, 12), pipelineId?.slice(0, 8),
    )
    return
  }

  const toolName = eventData.tool_name || eventData.data?.tool_name || ''
  if (!toolName) {
    _debugLogger.warn(
      '[TOOL_START] tool_name 缺失，跳过该工具调用: msgId=%s pipelineId=%s',
      messageId?.slice(0, 12), pipelineId?.slice(0, 8),
    )
    return
  }
  _debugLogger.debug(
    `[TOOL_START] tool=%s callId=%s pipelineId=%s msgId=%s`,
    toolName, callId, pipelineId?.slice(0, 8), messageId?.slice(0, 12),
  )

  const msgs = pipelineStore.getState().getMessages(pipelineId)
  const msg = msgs.find((m: any) => m.id === messageId)
  // 宿主消息权威槽位（工具卡容器从第一刻起有排序坐标）：事件携带 seq 时
  // 落位/补落——工具卡不得住在无 seq 的消息里（中断后恒垫底的根因）。
  const eventSequence = extractEventSequence(eventData)
  if (msg) backfillAuthoritativeSequence(pipelineId, messageId, eventSequence)
  if (!msg) {
    // tool_start 到达时消息占位可能不存在（stream_start 断线丢失/乱序/合并改名未命中），
    // 自动创建占位符——对齐 blockHandler 各 delta handler 的
    // "有消息就有占位符" 语义：占位缺失时自动补建，任何 tool 事件自愈。
    _debugLogger.warn(
      `[TOOL_START] msg not found, auto-creating placeholder: pipeline=%s msgId=%s tool=%s`,
      pipelineId?.slice(0, 12), messageId?.slice(0, 12), toolName,
    )
    ensureStreamingPlaceholder(pipelineId, messageId, extractThreadId(eventData), eventSequence)
  }

  // 同 call_id 的卡已存在（block 协议输出阶段建卡 / new_message 映射 / FIXUP）：
  // upsert 归并进这张卡，不建第二张。执行开始置 'calling'（state='streaming'
  // 的映射卡也收拢进 calling）；已有终态证据（result/error）的迟到事件不回退。
  // 直接寻址（事件 message_id 钉死消息）miss 时先跨消息按 callId 搜声明卡——
  // 融合迭代管道下契约事件 message_id 晚一轮，归并目标在上一轮消息里；
  // 跨消息也未命中才走占位+建卡路径。
  let targetMsgId = messageId
  let existingIdx = pipelineStore.getState().findToolCallPartIndex(pipelineId, messageId, callId)
  if (existingIdx < 0) {
    const foundMsgId = findToolCallMessageId(pipelineId, callId)
    if (foundMsgId) {
      targetMsgId = foundMsgId
      existingIdx = pipelineStore.getState().findToolCallPartIndex(pipelineId, targetMsgId, callId)
    }
  }
  if (existingIdx >= 0) {
    const cur = pipelineStore.getState().getMessages(pipelineId)
      .find((m) => m.id === targetMsgId)?.parts?.[existingIdx] as ToolCallPart | undefined
    if (cur) {
      const hasTerminal = cur.result !== undefined || cur.resultData !== undefined || cur.error !== undefined
      const eventArgs = eventData.args || eventData.data?.args || eventData.data?.tool_args || {}
      pipelineStore.getState().updatePart(pipelineId, targetMsgId, existingIdx, {
        state: hasTerminal ? cur.state : 'calling',
        name: !cur.name || cur.name === 'unknown' ? toolName : cur.name,
        args: cur.args && Object.keys(cur.args).length > 0 ? cur.args : eventArgs,
        containerTaskId: cur.containerTaskId
          ?? (eventData.container_task_id || eventData.data?.container_task_id || undefined),
      })
      _debugLogger.debug(
        '[TOOL_START] upsert 既有卡: tool=%s callId=%s state=%s msgId=%s',
        toolName, callId?.slice(0, 12), hasTerminal ? cur.state : 'calling', targetMsgId?.slice(0, 12),
      )
    }
    return
  }

  // 关闭当前 streaming text part，确保后续文本创建新的 text part
  // 流式阶段文本和工具卡片按 sequence 交错渲染。
  // 如果不关闭当前 text part，后续 stream_chunk 仍追加到 tool 卡片前面的 text part，
  // 导致工具调用后的文本拼到工具卡片前面的文本里，渲染顺序错误。
  const streamingIdx = pipelineStore.getState().findStreamingPartIndex(pipelineId, messageId)
  if (streamingIdx >= 0) {
    const streamingPart = msg?.parts?.[streamingIdx]
    if (streamingPart && streamingPart.type === 'text') {
      pipelineStore.getState().updatePart(pipelineId, messageId, streamingIdx, { state: 'done' })
    }
  }

  // 追加 tool_call part
  const existingToolParts = (msg?.parts || []).filter((p) => p.type === 'tool_call')
  _debugLogger.debug(
    '[TOOL_CREATE] tool=%s callId=%s msgId=%s totalToolParts=%d',
    toolName, callId?.slice(0, 12), messageId?.slice(0, 12), existingToolParts.length + 1,
  )
  pipelineStore.getState().appendPart(pipelineId, messageId, {
    type: 'tool_call',
    callId,
    name: toolName,
    args: eventData.args || eventData.data?.args || eventData.data?.tool_args || {},
    state: 'calling',
    // part 渲染按数组顺序（= 追加顺序 = 接收顺序），不分配 sequence。
    containerTaskId: eventData.container_task_id || eventData.data?.container_task_id || undefined,
  })
}

/** 处理工具执行中进度事件（task_observability 任务 2）。

 * bash 等长任务执行中经 frontend.emit 推 tool_progress（stdout 增量，
 * 源头已按 1KB/2s 节流）：按 call_id 定位 tool_call part，追加 partialOutput
 * 供 ActivityCard「执行输出」实时渲染，并更新 currentStep 运行时摘要。
 */
/** partialOutput 累计字符上限（保留尾部，防超大输出把 store 撑爆） */
const PARTIAL_OUTPUT_CAP = 64 * 1024

export function handleToolProgress(eventData: any) {
  const pipelineId = resolvePipelineId(eventData)
  if (!pipelineId) return
  const messageId = extractMessageId(eventData)
  if (!messageId) return
  const callId = eventData.call_id || eventData.data?.call_id
  if (!callId) return

  const data = eventData.data || eventData
  const delta: string = typeof data?.delta === 'string' ? data.delta : ''
  if (!delta) return

  // 直接寻址 miss 时跨消息按 callId 归并（契约事件 message_id 晚一轮，见
  // findToolCallMessageId 注释），定位到声明卡所在消息再追加。
  let targetMsgId = messageId
  let partIndex = pipelineStore.getState().findToolCallPartIndex(pipelineId, messageId, callId)
  if (partIndex < 0) {
    const foundMsgId = findToolCallMessageId(pipelineId, callId)
    if (foundMsgId) {
      targetMsgId = foundMsgId
      partIndex = pipelineStore.getState().findToolCallPartIndex(pipelineId, targetMsgId, callId)
    }
  }
  if (partIndex < 0) {
    // tool_start 未达/丢失：进度无法定位，静默丢弃（结果仍会由 tool_result 补齐）
    _debugLogger.debug(
      '[TOOL_PROGRESS] tool_call part not found, skip: tool=%s callId=%s',
      data?.tool_name, callId?.slice(0, 12),
    )
    return
  }

  const msgs = pipelineStore.getState().getMessages(pipelineId)
  const msg = msgs.find((m: any) => m.id === targetMsgId)
  const part = msg?.parts?.[partIndex] as any
  if (!part) return
  // 迟到进度（part 已 done/error）：结果已定，忽略避免覆盖
  if (part.state === 'done' || part.state === 'error' || part.state === 'cancelled') return

  // 追加 delta（尾部截断：最新输出最有信息量）
  const chunks: string[] = [...(part.partialOutput ?? []), delta]
  const merged = chunks.join('')
  const nextOutput: string[] =
    merged.length > PARTIAL_OUTPUT_CAP ? [merged.slice(merged.length - PARTIAL_OUTPUT_CAP)] : chunks

  // 运行时摘要：已输出 X KB / Ys（「工具跑到哪了」一句话可见）
  const kb = (Number(data?.bytes_read) || 0) / 1024
  const secs = (Number(data?.elapsed_ms) || 0) / 1000
  const currentStep = `已输出 ${kb >= 1024 ? `${(kb / 1024).toFixed(1)} MB` : `${kb.toFixed(1)} KB`} / ${secs.toFixed(1)}s`

  pipelineStore.getState().updatePart(pipelineId, targetMsgId, partIndex, {
    partialOutput: nextOutput,
    currentStep,
  } as any)
}

/** 处理工具调用结果事件 在 parts[] 中定位对应的 tool_call part 并更新其状态。 */
export function handleToolResult(eventData: any) {
  const pipelineId = resolvePipelineId(eventData)
  if (!pipelineId) {
    _debugLogger.warn(
      `[TOOL_RESULT] pipeline_id missing, _threadId=%s msgId=%s tool=%s`,
      eventData.data?._threadId?.slice(0, 12),
      extractMessageId(eventData)?.slice(0, 12),
      eventData.tool_name || eventData.data?.tool_name,
    )
    return
  }
  const messageId = extractMessageId(eventData)
  if (!messageId) return

  const callId = eventData.call_id || eventData.data?.call_id
  if (!callId) return

  // 结果事件到达即补落宿主权威槽位（工具卡容器不得缺 seq）。
  const eventSequence = extractEventSequence(eventData)
  backfillAuthoritativeSequence(pipelineId, messageId, eventSequence)

  // 通过 call_id 精确匹配 parts[] 中的 tool_call part 并更新。
  // 直接寻址 miss 时先跨消息按 callId 搜声明卡（契约事件 message_id 晚一轮，
  // 见 findToolCallMessageId 注释）；跨消息也未命中才走 FIXUP 建卡兜底。
  let targetMsgId = messageId
  let partIndex = pipelineStore.getState().findToolCallPartIndex(pipelineId, messageId, callId)
  if (partIndex < 0) {
    const foundMsgId = findToolCallMessageId(pipelineId, callId)
    if (foundMsgId) {
      targetMsgId = foundMsgId
      partIndex = pipelineStore.getState().findToolCallPartIndex(pipelineId, targetMsgId, callId)
    }
  }
  if (partIndex < 0) {
    // tool_result 到达时对应的 tool_call part 可能不存在（tool_start 事件丢失/乱序），
    // 补建 tool_call part 再写入结果，对齐 Python bridge_events.py 的 FIXUP 自动补发
    // tool_start 逻辑（Rust 内核 tool_core 无 FIXUP，前端兜底）——占位缺失时自动补建，
    // 任何 tool 事件自愈。
    const toolName = eventData.tool_name || eventData.data?.tool_name || 'unknown'
    _debugLogger.warn(
      '[TOOL_RESULT] tool_call part not found, FIXUP creating: tool=%s callId=%s msgId=%s',
      toolName, callId?.slice(0, 12), messageId?.slice(0, 12),
    )
    // 宿主容器也缺失（stream_start/tool_start 双丢）→ 先按权威槽位补建，
    // 否则 appendPart 无处落笔、工具结果静默蒸发。
    ensureMessageSlot(pipelineId, messageId, extractThreadId(eventData), eventSequence)
    pipelineStore.getState().appendPart(pipelineId, messageId, {
      type: 'tool_call',
      callId,
      name: toolName,
      args: {},
      state: 'calling',
    })
    partIndex = pipelineStore.getState().findToolCallPartIndex(pipelineId, messageId, callId)
  }

  if (partIndex >= 0) {
    const resultToolName = eventData.tool_name || eventData.data?.tool_name
    // 结果体超阈值统一截断（数据面瘦身批次 1.2，与 session.ts 映射收敛到同一
    // 纯函数）：part 只存「预览+标记」，展开时经 toolResultBackfill 按 sequence
    // 回取全量回填。sequence 取结果卡宿主消息当前权威值（流式占位未挂 seq 时
    // 标记不带 sequence，展开如实报不可回取，落库对账后新标记会带上）。
    const msgSequence = pipelineStore
      .getState()
      .getMessages(pipelineId)
      .find((m) => m.id === targetMsgId)?.sequence
    const updates: Record<string, unknown> = {
      state: resolveToolResultState(
        eventData.success ?? eventData.data?.success,
        eventData.error ?? eventData.data?.error,
        resultToolName ?? 'unknown',
      ),
      result: truncateToolResultData(eventData.result ?? eventData.data?.result, msgSequence),
      // 后端在 tool_result 事件携带的结构化完整数据（含 diff 的 added/removed/old_content/new_content），
      // 流式 result 字段为截断字符串仅供预览；result_data 供工具卡片渲染 +/- 徽标与展开 diff。
      resultData: truncateToolResultData(
        eventData.result_data ?? eventData.data?.result_data,
        msgSequence,
      ),
      error: eventData.error ?? eventData.data?.error,
      durationMs: eventData.duration_ms ?? eventData.data?.duration_ms,
      // 执行中 stdout 缓冲随结果落地释放（单调用上限 64KB 的线性滞留清零，
      // 2026-11-12 内存修复；结果后的权威视图是 result/resultData）
      partialOutput: undefined,
    }
    // 当 part 的 name 仍为 fallback "unknown" 且 result 事件携带有效 tool_name 时，回填更新
    if (resultToolName && resultToolName !== 'unknown') {
      const msgs = pipelineStore.getState().getMessages(pipelineId)
      const msg = msgs.find((m: any) => m.id === targetMsgId)
      if (msg?.parts?.[partIndex]) {
        const currentPart = msg.parts[partIndex] as any
        if (currentPart.name === 'unknown' || !currentPart.name) {
          updates.name = resultToolName
        }
      }
    }
    pipelineStore.getState().updatePart(pipelineId, targetMsgId, partIndex, updates)
  }
}
