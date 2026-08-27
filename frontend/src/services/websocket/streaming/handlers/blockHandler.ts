/**
 * LLM 流式 8 事件协议处理器（DSH 形态块索引化，方案 2026-08-26 定稿）。
 *
 * 事件词汇（后端 llm_service 经 event-bus 推送，内核透传补路由键）：
 *   block_start{index, block_type} / text_delta{index, text} /
 *   reasoning_delta{index, text} / tool_call_delta{index, id?, name?,
 *   arguments_delta} / block_end{index, block} / usage{input_tokens,
 *   output_tokens, ...} / finish{reason} / keepalive
 *
 * 组装规则：
 * - 块索引 (index) 是同一消息内块的全局递增序号（text/reasoning/tool-call 共享）。
 * - text/reasoning 块：delta 按块索引缓冲，RAF 批处理追加到对应 part（part 按
 *   块索引精确路由；渲染顺序 = part 追加顺序 = 块打开顺序）。
 * - tool-call 块：arguments_delta 为原始 JSON 字符串增量按块索引累积，id/name
 *   在 delta 首次携带时落 part；block_end 时解析完整 JSON 为 args（渲染卡片
 *   参数区）。不渲染原始 JSON 增量。
 * - block_end 闭合块：text/reasoning → state=done；tool-call → state=done（带
 *   解析后的 args）。闭合前先 flush 该消息缓冲，保证末尾 delta 不丢。
 * - finish 结束流：flush 残留缓冲 + 清理块状态（stream_end 仍由内核收尾裁决
 *   补发，权威合并逻辑不变）。
 * - usage 落入 usage store（与 stream_end 携带 usage 的既有路径同构）。
 * - keepalive 无业务载荷：心跳语义，消费端仅用于区分"上游活着但慢"与"连接死"，
 *   前端无需业务处理。
 *
 * 旧事件名（thinking_start/thinking_chunk/thinking_end/stream_chunk）已退役，
 * 本模块不消费、不留兼容分支。
 */
import { useContextUsageStore } from '@/stores/contextUsageStore'
import { usePipelineMessageStore as pipelineStore } from '@/stores/pipelineMessageStore'
import { loggers } from '@/utils/logger'

import { isPipelineRelevant, resolvePipelineId } from '../router'

import { ensureStreamingPlaceholder, extractMessageId, extractThreadId } from './utils'

const _debugLogger = loggers.websocket

/** 块类型：text / reasoning / tool_call（协议词汇） */
// reasoning 块在 store 的 part 类型词汇中映射为 thinking（渲染思考区）

/** 块类型 → store part type 映射 */
function partTypeOf(blockType: 'text' | 'reasoning'): 'text' | 'thinking' {
  return blockType === 'text' ? 'text' : 'thinking'
}

/** 单个 tool-call 块的累积状态（按块索引独立，并行工具互不污染） */
interface ToolBlockAccum {
  id?: string
  name?: string
  argumentsChunks: string[]
  /** 已创建的 tool_call part 下标（-1 = 尚未创建） */
  partIndex: number
}

/** 单条消息的块累积状态 */
interface MessageBlockState {
  /** 块索引 → 缓冲中的 delta 片段（text/reasoning 增量） */
  buffers: Map<number, string[]>
  /** 块索引 → 该块创建的 part index（text/reasoning） */
  partIndexByBlock: Map<number, number>
  /** 块索引 → tool-call 累积（仅 tool_call 块） */
  toolBlocks: Map<number, ToolBlockAccum>
  /** 已 block_end 闭合的块索引集合（防重复闭合/重复建 part） */
  closedBlocks: Set<number>
}

/** (pipelineId, messageId) → 块累积状态。块索引只在本消息内有效。 */
const _blockStates = new Map<string, MessageBlockState>()

function stateKey(pipelineId: string, messageId: string): string {
  return `${pipelineId}::${messageId}`
}

function getBlockState(pipelineId: string, messageId: string): MessageBlockState {
  const key = stateKey(pipelineId, messageId)
  let st = _blockStates.get(key)
  if (!st) {
    st = { buffers: new Map(), partIndexByBlock: new Map(), toolBlocks: new Map(), closedBlocks: new Set() }
    _blockStates.set(key, st)
  }
  return st
}

/** 事件载荷双取（顶层 / data 子对象），与既有 handler 一致 */
function eventPayload(eventData: any): any {
  return eventData?.data && typeof eventData.data === 'object' ? eventData.data : eventData || {}
}

/** 解析块索引（index 或 block_index 别名） */
function extractBlockIndex(p: any): number | null {
  const raw = p?.index ?? p?.block_index
  const n = Number(raw)
  return Number.isInteger(n) && n >= 0 ? n : null
}

/** 确保消息占位存在（与既有 handler 的"有消息就有占位符"语义一致） */
function ensurePlaceholder(eventData: any, pipelineId: string, messageId: string, eventName: string): void {
  const msgs = pipelineStore.getState().getMessages(pipelineId)
  if (!msgs.find((m: any) => m.id === messageId)) {
    _debugLogger.warn(
      `[${eventName}] msg not found, auto-creating placeholder: pipeline=%s msgId=%s`,
      pipelineId?.slice(0, 12), messageId?.slice(0, 12),
    )
    ensureStreamingPlaceholder(pipelineId, messageId, extractThreadId(eventData))
  }
}

/** 相关性门控：非关注 pipeline 的增量直接丢弃，不创建占位、不写 store。
 * 与旧流式 handler 的 isPipelineRelevant 防御同语义（index.ts 的 _logWrap
 * 已做中央门控，此处为 handler 级兜底，防绕过订阅面直接调用）。 */
function isRelevantOrDrop(pipelineId: string): boolean {
  if (!isPipelineRelevant(pipelineId)) {
    return false
  }
  return true
}

/** 定位指定类型块的 part：text/reasoning 找最后一个同类型 part；
 * tool-call 按 callId（或块索引兜底 tool-<index>）精确匹配。 */
function findBlockPartIndex(
  pipelineId: string,
  messageId: string,
  blockIndex: number,
  blockType: 'text' | 'reasoning' | 'tool_call',
  callId?: string,
): number {
  const msgs = pipelineStore.getState().getMessages(pipelineId)
  const msg = msgs.find((m: any) => m.id === messageId)
  const parts: any[] = msg?.parts || []
  if (blockType === 'tool_call') {
    return parts.findIndex(
      (pp: any) => pp.type === 'tool_call' && (pp.callId === callId || pp.callId === `tool-${blockIndex}`),
    )
  }
  const partType = partTypeOf(blockType)
  for (let i = parts.length - 1; i >= 0; i--) {
    if (parts[i].type === partType) return i
  }
  return -1
}

/** 定位同类型且仍 streaming 的最后一个 part（delta 先于 block_start 的乱序兜底） */
function findStreamingPartIndex(
  pipelineId: string,
  messageId: string,
  blockType: 'text' | 'reasoning',
): number {
  const msgs = pipelineStore.getState().getMessages(pipelineId)
  const msg = msgs.find((m: any) => m.id === messageId)
  const parts: any[] = msg?.parts || []
  const partType = partTypeOf(blockType)
  for (let i = parts.length - 1; i >= 0; i--) {
    if (parts[i].type === partType && parts[i].state === 'streaming') return i
  }
  return -1
}

// ── RAF 批处理：按 (pipelineId, messageId, blockIndex) 合并 delta 为单次 store 更新 ──

interface PendingFlush {
  pipelineId: string
  messageId: string
  blockIndex: number
  blockType: 'text' | 'reasoning'
}

/** 待刷写条目（key = pipelineId::messageId::blockIndex） */
const _pendingFlush = new Map<string, PendingFlush>()
let _flushRafId: number | null = null

function flushEntries(entries: PendingFlush[]): void {
  for (const e of entries) {
    const st = _blockStates.get(stateKey(e.pipelineId, e.messageId))
    if (!st) continue
    const chunks = st.buffers.get(e.blockIndex)
    if (!chunks || chunks.length === 0) continue
    st.buffers.delete(e.blockIndex)
    const partIndex = st.partIndexByBlock.get(e.blockIndex)
    if (partIndex === undefined || partIndex < 0) continue
    pipelineStore.getState().appendToPart(e.pipelineId, e.messageId, partIndex, chunks.join(''))
  }
}

function flushAllPending(): void {
  _flushRafId = null
  const entries = [..._pendingFlush.values()]
  _pendingFlush.clear()
  flushEntries(entries)
}

function flushPendingForMessage(messageId: string): void {
  const entries = [..._pendingFlush.values()].filter((e) => e.messageId === messageId)
  if (entries.length === 0) return
  for (const e of entries) {
    _pendingFlush.delete(flushKey(e.pipelineId, e.messageId, e.blockIndex))
  }
  flushEntries(entries)
}

function flushKey(pipelineId: string, messageId: string, blockIndex: number): string {
  return `${pipelineId}::${messageId}::${blockIndex}`
}

function scheduleFlush(pipelineId: string, messageId: string, blockIndex: number, blockType: 'text' | 'reasoning'): void {
  const key = flushKey(pipelineId, messageId, blockIndex)
  if (!_pendingFlush.has(key)) {
    _pendingFlush.set(key, { pipelineId, messageId, blockIndex, blockType })
  }
  if (_flushRafId === null) {
    _flushRafId = requestAnimationFrame(flushAllPending)
  }
}

/** 立即刷写全部缓冲（block_end/finish 前调用，保证末尾 delta 不丢） */
export function flushBlockBuffers(): void {
  if (_flushRafId !== null) {
    cancelAnimationFrame(_flushRafId)
    _flushRafId = null
  }
  flushAllPending()
}

/** text/reasoning 增量：缓冲进 RAF，按块路由到对应 part（part 由 block_start
 * 预创建；delta 先于 block_start 到达（乱序）时按需补建） */
function bufferTextDelta(
  eventData: any,
  pipelineId: string,
  messageId: string,
  blockIndex: number,
  blockType: 'text' | 'reasoning',
  text: string,
): void {
  const st = getBlockState(pipelineId, messageId)
  // 块已闭合后到达的 delta（乱序/重放）：防御性丢弃
  if (st.closedBlocks.has(blockIndex)) return
  ensurePlaceholder(eventData, pipelineId, messageId, blockType === 'text' ? 'TEXT_DELTA' : 'REASONING_DELTA')

  let partIndex = st.partIndexByBlock.get(blockIndex)
  if (partIndex === undefined) {
    // delta 先于 block_start（乱序）：只复用同类型 streaming part（绝不落到已
    // 闭合的同类型 part），无则补建
    partIndex = findStreamingPartIndex(pipelineId, messageId, blockType)
    if (partIndex < 0) {
      pipelineStore.getState().appendPart(pipelineId, messageId, {
        type: partTypeOf(blockType),
        content: '',
        state: 'streaming',
      })
      partIndex = pipelineStore.getState().findLastPartIndex(pipelineId, messageId, partTypeOf(blockType))
    }
    st.partIndexByBlock.set(blockIndex, partIndex)
  }

  const buf = st.buffers.get(blockIndex) || []
  buf.push(text)
  st.buffers.set(blockIndex, buf)
  scheduleFlush(pipelineId, messageId, blockIndex, blockType)
}

/** 处理块开始事件：登记块索引（text/reasoning 块即建 streaming part——
 * 块索引 → part 的映射在开块时确定，后续 delta 严格按块路由，绝不落到
 * 已闭合的同类型 part；tool-call 建累积态）。 */
export function handleBlockStart(eventData: any) {
  const p = eventPayload(eventData)
  const pipelineId = resolvePipelineId(eventData)
  const messageId = extractMessageId(eventData)
  if (!pipelineId || !messageId || !isRelevantOrDrop(pipelineId)) return
  const blockIndex = extractBlockIndex(p)
  const blockType = String(p.block_type || 'text')
  if (blockIndex === null) {
    _debugLogger.warn('[BLOCK_START] invalid index, skip: %s', p.index)
    return
  }
  if (blockType !== 'text' && blockType !== 'reasoning' && blockType !== 'tool_call') {
    _debugLogger.warn('[BLOCK_START] unknown block_type, skip: %s', blockType)
    return
  }
  const st = getBlockState(pipelineId, messageId)
  if (st.closedBlocks.has(blockIndex)) return
  ensurePlaceholder(eventData, pipelineId, messageId, 'BLOCK_START')

  if (blockType === 'tool_call') {
    if (!st.toolBlocks.has(blockIndex)) {
      st.toolBlocks.set(blockIndex, { id: undefined, name: undefined, argumentsChunks: [], partIndex: -1 })
    }
    return
  }

  // text/reasoning：开块即建 part（streaming），映射块索引 → part 下标。
  // 空块（无 delta）也会建空 part——渲染端按 trim() 过滤，合并端按无实质内容丢弃，
  // 不影响最终展示。
  if (!st.partIndexByBlock.has(blockIndex)) {
    pipelineStore.getState().appendPart(pipelineId, messageId, {
      type: partTypeOf(blockType),
      content: '',
      state: 'streaming',
    })
    st.partIndexByBlock.set(blockIndex, pipelineStore.getState().findLastPartIndex(pipelineId, messageId, partTypeOf(blockType)))
  }
}

/** 处理正文增量事件 */
export function handleTextDelta(eventData: any) {
  const p = eventPayload(eventData)
  const pipelineId = resolvePipelineId(eventData)
  const messageId = extractMessageId(eventData)
  const text = String(p.text || '')
  if (!pipelineId || !messageId || !text || !isRelevantOrDrop(pipelineId)) return
  const blockIndex = extractBlockIndex(p)
  if (blockIndex === null) return
  bufferTextDelta(eventData, pipelineId, messageId, blockIndex, 'text', text)
}

/** 处理思考增量事件 */
export function handleReasoningDelta(eventData: any) {
  const p = eventPayload(eventData)
  const pipelineId = resolvePipelineId(eventData)
  const messageId = extractMessageId(eventData)
  const text = String(p.text || '')
  if (!pipelineId || !messageId || !text || !isRelevantOrDrop(pipelineId)) return
  const blockIndex = extractBlockIndex(p)
  if (blockIndex === null) return
  bufferTextDelta(eventData, pipelineId, messageId, blockIndex, 'reasoning', text)
}

/** 处理工具调用增量事件：按块索引累积 id/name/arguments_delta，首 delta 建 tool_call part */
export function handleToolCallDelta(eventData: any) {
  const p = eventPayload(eventData)
  const pipelineId = resolvePipelineId(eventData)
  const messageId = extractMessageId(eventData)
  if (!pipelineId || !messageId || !isRelevantOrDrop(pipelineId)) return
  const blockIndex = extractBlockIndex(p)
  if (blockIndex === null) return

  const st = getBlockState(pipelineId, messageId)
  if (st.closedBlocks.has(blockIndex)) return
  ensurePlaceholder(eventData, pipelineId, messageId, 'TOOL_CALL_DELTA')

  let tb = st.toolBlocks.get(blockIndex)
  if (!tb) {
    tb = { id: undefined, name: undefined, argumentsChunks: [], partIndex: -1 }
    st.toolBlocks.set(blockIndex, tb)
  }
  if (p.id) tb.id = String(p.id)
  if (p.name) tb.name = String(p.name)
  const argDelta = p.arguments_delta ?? p.arguments
  if (argDelta !== undefined && argDelta !== null) {
    tb.argumentsChunks.push(String(argDelta))
  }

  const callId = tb.id || `tool-${blockIndex}`
  if (tb.partIndex < 0) {
    const existing = findBlockPartIndex(pipelineId, messageId, blockIndex, 'tool_call', callId)
    if (existing >= 0) {
      tb.partIndex = existing
    } else {
      pipelineStore.getState().appendPart(pipelineId, messageId, {
        type: 'tool_call',
        callId,
        name: tb.name || '',
        args: {},
        state: 'calling',
      })
      tb.partIndex = pipelineStore.getState().findToolCallPartIndex(pipelineId, messageId, callId)
    }
  }
  if (tb.partIndex >= 0) {
    const updates: Record<string, unknown> = {}
    if (tb.id) updates.callId = tb.id
    if (tb.name) updates.name = tb.name
    if (Object.keys(updates).length > 0) {
      pipelineStore.getState().updatePart(pipelineId, messageId, tb.partIndex, updates as any)
    }
  }
}

/** 处理块闭合事件：flush 缓冲 → 闭合对应 part（tool-call 解析 args） */
export function handleBlockEnd(eventData: any) {
  const pipelineId = resolvePipelineId(eventData)
  const messageId = extractMessageId(eventData)
  if (!pipelineId || !messageId || !isRelevantOrDrop(pipelineId)) return
  const p = eventPayload(eventData)
  const blockIndex = extractBlockIndex(p)
  if (blockIndex === null) return

  const st = getBlockState(pipelineId, messageId)
  if (st.closedBlocks.has(blockIndex)) return
  st.closedBlocks.add(blockIndex)

  const block = p.block || {}
  const blockType = String(block.block_type || '')

  if (blockType === 'tool_call') {
    // 先 flush 该消息残留正文 delta（tool 块闭合是 part 结构边界点），再落结果
    flushPendingForMessage(messageId)
    const tb = st.toolBlocks.get(blockIndex)
    st.toolBlocks.delete(blockIndex)
    if (tb && tb.partIndex >= 0) {
      const raw = tb.argumentsChunks.join('')
      let args: Record<string, unknown> = {}
      if (raw) {
        try {
          const parsed = JSON.parse(raw)
          args = parsed && typeof parsed === 'object' ? parsed : {}
        } catch {
          _debugLogger.warn('[Block] tool arguments JSON parse failed, keep raw: index=%s', blockIndex)
          args = { raw }
        }
      }
      const updates: Record<string, unknown> = { state: 'done', args }
      if (tb.id) updates.callId = tb.id
      if (tb.name) updates.name = tb.name
      pipelineStore.getState().updatePart(pipelineId, messageId, tb.partIndex, updates as any)
    }
    return
  }

  if (blockType === 'text' || blockType === 'reasoning') {
    // 先 flush 该消息残留 delta，再置 done（保证末尾增量落盘）
    flushPendingForMessage(messageId)
    const partIndex = st.partIndexByBlock.get(blockIndex)
    if (partIndex !== undefined && partIndex >= 0) {
      pipelineStore.getState().updatePart(pipelineId, messageId, partIndex, { state: 'done' } as any)
    }
  }
}

/** 处理用量事件（finish 前发出） */
export function handleUsage(eventData: any) {
  const pipelineId = resolvePipelineId(eventData)
  if (!pipelineId) return
  const p = eventPayload(eventData)
  useContextUsageStore.getState().updateUsage(pipelineId, {
    input_tokens: Number(p.input_tokens) || 0,
    output_tokens: Number(p.output_tokens) || 0,
    total_tokens: Number(p.total_tokens) || 0,
    cached_tokens: Number(p.cached_tokens) || 0,
  })
}

/** 处理终结事件：flush 残留缓冲并清理块状态（stream_end 收尾由内核裁决补发） */
export function handleFinish(eventData: any) {
  const pipelineId = resolvePipelineId(eventData)
  const messageId = extractMessageId(eventData)
  if (!pipelineId || !messageId || !isRelevantOrDrop(pipelineId)) return
  flushPendingForMessage(messageId)
  _blockStates.delete(stateKey(pipelineId, messageId))
}

/** stream_end / stream_error 到达时清理该消息的块状态（复用既有终止路径） */
export function clearBlockStateForMessage(pipelineId: string, messageId: string): void {
  if (!pipelineId || !messageId) return
  flushPendingForMessage(messageId)
  _blockStates.delete(stateKey(pipelineId, messageId))
}
