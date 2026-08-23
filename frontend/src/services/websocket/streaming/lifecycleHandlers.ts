/** 生命周期事件处理器（WS重连补漏 / 系统通知 / 用量更新 / 终止评估） 从 initStreamingEvents 中提取的独立处理器函数，降低 index.ts 复杂度。
 *
 * 2026-08 清理：handleStateChange（state_change 事件）已删除——后端无该事件发射源。
 */
import { useContextUsageStore } from '@/stores/contextUsageStore'
import { useNotificationStore } from '@/stores/notificationStore'
import { usePipelineMessageStore } from '@/stores/pipelineMessageStore'
import { useTerminationStore } from '@/stores/terminationStore'
import { loggers } from '@/utils/logger'

import { terminatePipeline } from './handlers/utils'
import { resolvePipelineId } from './router'

/** 处理 WS 重连补漏。重连时对每个 streaming 管道执行 backfill 增量补漏，
 * 拉回断线期间后端 replay 缓冲累积的消息（useRealtimeEvents 只补激活会话主管道，
 * 子管道/其他会话的 streaming 管道在此统一补漏，避免消息静默丢失）。
 * 补漏成功不弹警告（消息已恢复）；仅补漏失败（或无法确定 threadId）时才提示「可能丢失」。 */
export async function handleReconnected(): Promise<void> {
  const pipelineStore = usePipelineMessageStore.getState()
  const streamingState = pipelineStore.streamingState
  const logger = loggers.sessionStore

  logger.info('[streaming] WS 重连，清理残留状态，streaming 管道数=%d', Object.keys(streamingState).length)

  // 清理残留 streaming thinking parts
  const messagesByPipeline = pipelineStore.messagesByPipeline
  for (const [pipelineId, messages] of Object.entries(messagesByPipeline)) {
    const stuckMessages = (messages as any[]).filter(
      (m: any) => (m.parts || []).some((p: any) => p.type === 'thinking' && p.state === 'streaming'),
    )
    for (const msg of stuckMessages) {
      const updatedParts = (msg.parts as any[]).map((p: any) =>
        p.state === 'streaming' ? { ...p, state: 'done' as const } : p,
      )
      pipelineStore.updateMessage(pipelineId, msg.id, { parts: updatedParts } as any)
    }
  }

  // 为 streaming 管道补漏（所有管道，不止主管道）。
  // F1：刷新后 streamingState 被 persist merge 重置为 {}，仅靠它会漏掉所有管道（含子管道）。
  // 故取并集——streamingState 里 isStreaming 的 + messagesByPipeline 里有 status==='streaming'
  // 的 assistant 消息的管道（后者在 5min grace 内会从 IndexedDB 恢复，是刷新后唯一的残留信号）。
  const streamingPipelineIdSet = new Set<string>(
    Object.keys(streamingState).filter((pid) => streamingState[pid]?.isStreaming),
  )
  for (const [pid, msgs] of Object.entries(messagesByPipeline)) {
    if ((msgs as any[]).some((m: any) => m.role === 'assistant' && m.status === 'streaming')) {
      streamingPipelineIdSet.add(pid)
    }
  }
  const streamingPipelineIds = [...streamingPipelineIdSet]

  // 补漏失败或无法确定 threadId 的管道列表（补漏成功 → 消息已恢复，不打扰用户）
  const failedPipelines: string[] = []
  await Promise.all(
    streamingPipelineIds.map(async (pipelineId) => {
      const threadId = pipelineStore.pipelineSessionMap[pipelineId]
      if (!threadId) {
        failedPipelines.push(pipelineId)
        return
      }
      try {
        const result = await pipelineStore.loadPipelineMessages(pipelineId, {
          threadId,
          mode: 'backfill',
          skipStreamingCheck: true,
        })
        if (!result.ok) failedPipelines.push(pipelineId)
      } catch (err) {
        logger.warn('[streaming] 重连补漏失败 pipeline=%s err=%s', pipelineId.slice(0, 12), String(err))
        failedPipelines.push(pipelineId)
      }
    }),
  )

  // streamingState 中已有旧记录，占位创建/更新失败，AI 回复无法显示。
  for (const pipelineId of streamingPipelineIds) {
    // F2：backfill 后仍 streaming 的消息 = 未能从后端恢复（真·丢失，如服务端杀流）。
    // 标记 interrupted + 追加 warning system part，让用户看到"输出被中断"而非误以为完成。
    const messages = pipelineStore.messagesByPipeline[pipelineId] || []
    for (const msg of messages as any[]) {
      if (msg.role === 'assistant' && msg.status === 'streaming') {
        pipelineStore.updateMessage(pipelineId, msg.id, { status: 'interrupted' } as any)
        pipelineStore.appendPart(pipelineId, msg.id, {
          type: 'system',
          content: '（输出被中断，内容可能不完整）',
          level: 'warning',
          notificationType: 'stream_interrupted',
        } as any)
      }
    }
    // 清理 streamingState（只清本管道——ADR 2026-08-21 不再顺带清 threadId）
    terminatePipeline(pipelineId)
    logger.info('[streaming] 终止残留流式管道 %s，清理 streamingState', pipelineId.slice(0, 12))
  }

  // 仅补漏失败时提示（此时消息确实可能丢失，需用户手动刷新）
  if (failedPipelines.length > 0) {
    useNotificationStore.getState().addNotification({
      title: '流式消息可能丢失',
      message: `WebSocket 重连期间有 ${failedPipelines.length} 个流式管道可能丢失消息，请检查相关会话或手动刷新`,
      priority: 'high',
      category: 'alert',
      isBlocking: false,
      autoDismissMs: 10000,
    })
  }
}

/** 处理 SYSTEM_NOTIFICATION 事件（任务完成/失败等系统通知） 系统消息气泡的唯一创建入口。后端 emit_notification 生成 record_id（唯一 id 来源），事件 payload 带上它；前端用它作消息 id，与后端落库的 record_id 一致，刷新后按 id 自然去重（与 AI 消息同款 id 契约）。 */
export function handleSystemNotification(eventData: any): void {
  const pipelineId = resolvePipelineId(eventData)
  const data = eventData?.data || eventData
  const content = data?.content || ''
  const level = data?.level || 'info'
  const notificationType = data?.notificationType || ''
  const notificationId = data?.notification_id || ''
  // record_id 是后端 emit_notification 生成的【唯一 id 来源】，事件必须携带。
  // 缺失说明后端未正确生成 id —— 直接报错，不做兜底（兜底会掩盖后端 bug，
  // 且无法与 track 落库的 record_id 对齐，导致刷新后重复渲染）。
  const recordId = data?.record_id
  if (!recordId) {
    loggers.websocket.error(
      '[系统通知] record_id 缺失，拒绝创建气泡（后端 emit_notification 必须生成 record_id）: pipeline=%s content=%.40s',
      pipelineId?.slice(0, 12), content.slice(0, 40),
    )
    return
  }

  if (!pipelineId || !content) return

  const pipelineStore = usePipelineMessageStore.getState()

  const existingMsgs = pipelineStore.getMessages(pipelineId)
  // 内存级去重：同一 record_id 的 system 事件只创建一次（防重复投递）。
  // record_id 即消息 id，与后端落库 record_id 一致，刷新时由 initFromAPI 的
  // id 对账（isCoveredByApi）处理流式气泡 vs API 记录的去重。
  const alreadyExists = existingMsgs.some((m: any) => m.id === recordId)
  if (alreadyExists) return

  loggers.websocket.debug(
    '[MSG-LIFE] 系统通知创建: pipeline=%s content=%.40s',
    pipelineId.slice(0, 12), content.slice(0, 40),
  )

  // ★ 诊断：notification 到达时的 store 状态（INFO 级别确保可见）
  const _diagBefore = pipelineStore.getMessages(pipelineId)
  const _diagLast = _diagBefore[_diagBefore.length - 1]
  loggers.websocket.info(
    '[NOTIF-ARRIVE] total=%d last=[%s/%s/%s] seq=%s',
    _diagBefore.length,
    _diagLast?.role ?? 'null',
    _diagLast?.status ?? 'null',
    (_diagLast?.id ?? '').slice(0, 10),
    data?.sequence ?? 'none',
  )

  pipelineStore.addMessage(pipelineId, {
    // id 用后端 record_id（== track 落库 record_id），刷新后 API 返回同 id，
    // isCoveredByApi 按 id 去重，不再产生「流式气泡 + API 记录」两条。
    id: recordId,
    role: 'system',
    content,
    timestamp: new Date().toISOString(),
    // seq 只用事件携带的权威值（无则挂空排序末尾，对账纠正）——不本地拼 localMax+1
    sequence: data?.sequence,
    parts: [
      {
        type: 'system',
        content,
        level: level as any,
        notificationType,
        sequence: 0,
      },
    ],
    status: 'completed',
    metadata: {
      record_type: 'system',
      type: 'system',
      sender_type: 'system',
      notification_level: level,
      notification_type: notificationType,
      notification_id: notificationId,
    },
  } as any)
}

/**
 * 处理 COST_UPDATE 事件：写入本轮单轮 token 用量到 contextUsageStore。
 *
 * 后端 track 插件在每轮 llm_call 后推送（tool_execute 轮已跳过），
 * payload = { pipeline_id, total_tokens, input_tokens, output_tokens,
 * cached_tokens, missed_tokens, cache_hit_ratio, cumulative.* }，
 * 均为本轮 API 返回的单轮值（cumulative 为管道累计）。进度条据此按
 * pipeline 实时刷新；cache 维度供 CostDashboardWidget 展示命中率与趋势。
 */

/** cache 命中率骤降检测阈值：降幅 ≥30pp 且当前 <70% 视为异常 */
const CACHE_DROP_DELTA = 0.3
const CACHE_DROP_FLOOR = 0.7

/** 已提示骤降未恢复的 pipeline（恢复到 ≥70% 后解除，可再次提示） */
const cacheDropAlerted = new Map<string, boolean>()

/** 命中率骤降检测（task_observability 1b：提示哪轮调用破坏了 cache 前缀） */
function checkCacheDrop(pipelineId: string, prevRatio: number, nextRatio: number): void {
  if (!Number.isFinite(prevRatio) || !Number.isFinite(nextRatio)) return
  if (nextRatio >= CACHE_DROP_FLOOR) {
    cacheDropAlerted.delete(pipelineId)
    return
  }
  const dropped = prevRatio - nextRatio >= CACHE_DROP_DELTA
  if (dropped && !cacheDropAlerted.get(pipelineId)) {
    cacheDropAlerted.set(pipelineId, true)
    useNotificationStore.getState().addNotification({
      category: 'alert',
      title: '缓存命中率骤降',
      message:
        `本轮命中率 ${(nextRatio * 100).toFixed(1)}%（上一轮 ${(prevRatio * 100).toFixed(1)}%）。` +
        '某处输入可能破坏了 cache 前缀，miss 部分将按全价计费。',
      priority: 'normal',
      isBlocking: false,
      sessionId: pipelineId,
      autoDismissMs: 15000,
    })
    loggers.websocket.warn(
      '[COST_UPDATE] cache 命中率骤降: pipeline=%s %.1f%% → %.1f%%',
      pipelineId, prevRatio * 100, nextRatio * 100,
    )
  }
}

export function handleCostUpdate(eventData: any): void {
  const pipelineId = resolvePipelineId(eventData)
  if (!pipelineId) return
  const data = eventData?.data || eventData
  const totalTokens = data?.total_tokens || 0
  // 后端 tool_execute 轮已过滤，前端再兜底防 0 值覆盖
  if (totalTokens <= 0) return
  const prevUsage = useContextUsageStore.getState().getUsage(pipelineId)
  useContextUsageStore.getState().updateUsage(pipelineId, {
    total_tokens: totalTokens,
    input_tokens: data?.input_tokens || 0,
    output_tokens: data?.output_tokens || 0,
    cached_tokens: data?.cached_tokens,
    missed_tokens: data?.missed_tokens,
    cache_hit_ratio: data?.cache_hit_ratio,
    cumulative: data?.cumulative,
  })
  if (typeof data?.cache_hit_ratio === 'number' && prevUsage) {
    checkCacheDrop(pipelineId, prevUsage.hitRatio, data.cache_hit_ratio)
  }
}

/**
 * 处理 TERMINATION_STATUS 事件（task_observability 1c）：
 * termination_advisor Input 插件每轮推送的主动终止评估，
 * 写入 terminationStore（「剩余预算」+「收敛信号」指示器数据源）。
 */
export function handleTerminationStatus(eventData: any): void {
  const pipelineId = resolvePipelineId(eventData)
  if (!pipelineId) return
  const data = eventData?.data || eventData
  useTerminationStore.getState().updateStatus(pipelineId, {
    convergence: data?.convergence ?? 'converging',
    shouldStop: Boolean(data?.should_stop),
    stopReason: data?.stop_reason ?? '',
    remainingBudgetPercent:
      typeof data?.remaining_budget_percent === 'number' ? data.remaining_budget_percent : null,
    iteration: Number(data?.iteration) || 0,
    elapsedS: Number(data?.elapsed_s) || 0,
  })
}
