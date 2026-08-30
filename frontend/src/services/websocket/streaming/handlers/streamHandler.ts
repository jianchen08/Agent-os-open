/** 流式事件处理器（start / end / error） 正文/思考/工具增量已迁移到 8 事件
 * 协议（blockHandler.ts，方案 2026-08-26 定稿）：stream_chunk / thinking_*
 * 旧事件退役，本模块只保留内核生命周期事件（stream_start / stream_end /
 * stream_error）与收尾清理。 */
import { useAgentTabStore } from '@/stores/agentTabStore'
import { useContextUsageStore } from '@/stores/contextUsageStore'
import { useNotificationStore } from '@/stores/notificationStore'
import { usePipelineMessageStore as pipelineStore } from '@/stores/pipelineMessageStore'
import { usePipelineRegistryStore } from '@/stores/pipelineRegistryStore'
import { useSessionListStore } from '@/stores/sessionListStore'
import { loggers } from '@/utils/logger'

import { isPipelineRelevant, resolvePipelineId } from '../router'

import { clearBlockStateForMessage, flushBlockBuffers } from './blockHandler'
import { ensureStreamingPlaceholder, extractMessageId, extractThreadId, mergeStreamingParts, terminatePipeline } from './utils'

const _debugLogger = loggers.websocket

/** 立即刷写块协议残留缓冲（正文/思考 delta）。streamEnd / streamError /
 * 手动 Stop（router.tsx）必须在收尾前调用，保证末尾 delta 不丢。 */
export function flushStreamChunkBuffer(): void {
  flushBlockBuffers()
}

/** 处理流式开始事件 */
export function handleStreamStart(eventData: any) {
  const pipelineId = resolvePipelineId(eventData)
  if (!pipelineId) {
    // pipeline_id 为空时 warn 并 return，_threadId 不参与消息路由
    _debugLogger.warn(
      `[STREAM_START] pipeline_id missing, discarding event: _threadId=%s msgId=%s`,
      eventData._threadId?.slice(0, 12),
      extractMessageId(eventData)?.slice(0, 12),
    )
    return
  }
  const messageId = extractMessageId(eventData)
  if (!messageId) return

  // 管道注册表实时同步：running（在相关性门控之前——面板要展示未打开/未注册的管道）
  usePipelineRegistryStore.getState().applyStreamStatus(pipelineId, 'running')

  // 相关性门控：非关注 pipeline（非活跃/未注册/未开 Tab）的事件直接丢弃，
  // 不创建占位消息、不写 store。pipeline 仅由前端主动注册（如 agentHandler
  // 在用户实际打开/创建子任务时 registerPipeline），后端广播的事件不再触发注册。
  if (!isPipelineRelevant(pipelineId)) {
    _debugLogger.info(
      `[STREAM_START] drop irrelevant pipeline event: pipelineId=%s`,
      pipelineId.slice(0, 12),
    )
    return
  }

  const threadId = extractThreadId(eventData)

  const currentActivePipelineId = pipelineStore.getState().activePipelineId
  _debugLogger.info(
    `[STREAM_START] pipelineId=${pipelineId.slice(0, 12)} threadId=${threadId?.slice(0, 12) || 'null'} msgId=${messageId.slice(0, 12)} activePipelineId=${currentActivePipelineId?.slice(0, 12) || 'null'}`,
  )

  // stream_start 事件不携带消息 seq（后端在引擎执行时才分配；事件信封顶层
  // sequence 是全局事件计数器，非消息 seq——严禁当消息序使用）。占位 seq 挂空，
  // stream_end final_sequence / new_message
  // 权威值到达后由对账纠正。
  ensureStreamingPlaceholder(pipelineId, messageId, threadId)

  if (currentActivePipelineId === pipelineId) return

  const agentTabStore = useAgentTabStore.getState()
  const activeTab = agentTabStore.getActiveTab()
  if (activeTab?.pipelineRunId === pipelineId) {
    pipelineStore.getState().activatePipeline(pipelineId)
  }
}

/** 处理流式结束事件 */
export function handleStreamEnd(eventData: any) {
  // 先刷写缓冲区中的残留 chunk，再进行最终合并，避免数据丢失
  flushStreamChunkBuffer()

  const pipelineId = resolvePipelineId(eventData)
  const threadId = extractThreadId(eventData)
  const messageId = extractMessageId(eventData)

  _debugLogger.info(
    `[STREAM_END] pipelineId=${pipelineId?.slice(0, 12) || 'null'} threadId=${threadId?.slice(0, 12) || 'null'} msgId=${messageId?.slice(0, 12) || 'null'} activePipelineId=${pipelineStore.getState().activePipelineId?.slice(0, 12) || 'null'}`,
  )

  if (pipelineId) {
    // 引擎逐轮发射 stream_end（一轮 = 一条消息）：轮收尾 ≠ 整次执行结束。
    // 多轮执行的工具轮间在此终止生成态，会把"执行中"误判回"空闲"——
    // busy 发送分支失效，乐观气泡与待发队列同屏（互斥破坏）。生成态的
    // 终止信号是 run 级 pipeline_round_finished / stream_error，此处只做
    // 当轮消息收尾。
    if (messageId) {
      // 清理块协议累积状态（正文/思考 delta 已 flush，残留缓冲丢弃——权威内容由
      // 下方 mergeStreamingParts / 对账兜底）
      clearBlockStateForMessage(pipelineId, messageId)
      const msgs = pipelineStore.getState().getMessages(pipelineId)
      const msg = msgs.find((m: any) => m.id === messageId)

      if (msg) {
        // msg 存在：合并后端权威 parts/sequence，收尾占位。
        // 同步后端权威 sequence（final_sequence）：stream_start 不携带消息 seq，
        // 占位 seq 挂空，stream_end 携带 final_sequence 在此
        // 同步权威值——对账（isCoveredByApi 按 id/cmid）与排序才正确。
        const finalSeq = eventData?.data?.final_sequence ?? eventData?.final_sequence
        if (finalSeq != null && finalSeq !== msg.sequence) {
          pipelineStore.getState().updateMessage(pipelineId, messageId, {
            sequence: finalSeq,
          } as any)
        }

        // 合并而非覆盖：本地有实质内容就优先保留本地，serverParts 仅作兜底（详见 mergeStreamingParts）。
        // stream_end 的 parts 是末轮快照（可能只有 tool_call 或只有 text，残缺），
        // 本地已按真实时序累积了完整 parts（text→tool_call→text）时优先保留本地，
        // server 快照只用于补充本地缺失的权威增量（如断线期间丢失的 chunk）。
        const serverParts = eventData?.data?.parts
        const localParts = msg.parts || []
        if (serverParts && Array.isArray(serverParts) && serverParts.length > 0) {
          const fullContent = eventData?.data?.full_content
          const { parts: finalParts, content } = mergeStreamingParts(
            localParts, serverParts, fullContent, msg.content,
          )
          const updatePayload: any = {
            parts: finalParts,
            content,
            status: 'completed',
          }
          pipelineStore.getState().updateMessage(pipelineId, messageId, updatePayload)
        } else {
          // fallback: 后端未发 parts，走原有 finalizeMessage
          const hasContent = (msg.content || '').length > 0 || (msg.parts || []).length > 0
          if (hasContent) {
            pipelineStore.getState().finalizeMessage(pipelineId, messageId)
            if (msg.status === 'streaming') {
              pipelineStore.getState().updateMessage(pipelineId, messageId, {
                status: 'completed',
              } as any)
            }
          } else {
            // 空轮次收尾：stream_end 前未收到 new_message 的占位是「本轮无产出」
            // （引擎轮次模型：init/exit 等无消息轮次照常发 stream_end），该消息未
            // 持久化、也不参与重放——静默移除占位。真实「整轮无回复」由 stream_error
            // (NO_ASSISTANT_REPLY) 显式通知，此处不再补空回复提示（旧形态会为每个
            // 前处理轮次弹一次「回复内容为空」通知）。
            _debugLogger.warn(
              '[STREAM_END] 空轮次无消息，移除占位: pipeline=%s msgId=%s',
              pipelineId.slice(0, 12), messageId.slice(0, 12),
            )
            const current = pipelineStore.getState().getMessages(pipelineId)
            if (current.find((m: any) => m.id === messageId)) {
              pipelineStore.getState().removeMessage(pipelineId, messageId)
            }
          }
        }
      } else {
        // stream_end 找不到本地消息：说明 stream_start/chunk 在断线期间丢失，本地无对应占位。
        // 后端已完成并持久化，由重连/重进入时的统一重新加载（useRealtimeEvents 的 initFromAPI
        // 对账）拉取权威内容。此处不主动发请求（handler 是纯事件处理，不触发 HTTP）。
        _debugLogger.warn(
          '[STREAM_END] 本地无对应消息（断线期间 stream_start 丢失），将由重新加载对账: pipeline=%s msgId=%s',
          pipelineId?.slice(0, 12), messageId?.slice(0, 12),
        )
      }
    }

  } else {
    // 「清别人状态」已废除：缺 pipeline_id 的终止事件无法定位归属
    // 管道——不拿 activePipelineId 顶替（可能误杀活跃管道的流式）、不拿
    // threadId 当管道清。记 error 等对账补正（90s 单消息超时兜底仍在）。
    _debugLogger.error(
      `[STREAM_END] pipeline_id 缺失，跳过清理（不用 activePipelineId 顶替）: _threadId=%s msgId=%s`,
      threadId?.slice(0, 12) || 'null',
      messageId?.slice(0, 12) || 'null',
    )
    return
  }

  const usage = eventData?.usage || eventData?.data?.usage
  if (usage && typeof usage === 'object') {
    useContextUsageStore.getState().updateUsage(pipelineId, usage)
  }

  // REQ-28: 首次 AI 回复完成后自动重命名会话
  if (threadId && pipelineId) {
    useSessionListStore.getState().autoRenameSessionIfNeeded(threadId, pipelineId)
  }
}

/** 处理流式错误事件 */
export function handleStreamError(eventData: any) {  // 先刷写缓冲区，确保错误前的内容不丢失
  flushStreamChunkBuffer()

  const pipelineId = resolvePipelineId(eventData)

  // 只清事件明确归属的管道；缺失即记 error 跳过（不拿 threadId 顶替）
  if (pipelineId) {
    // 标记管道已终止（错误），防止 ensureStreamingPlaceholder 重新启动
    terminatePipeline(pipelineId)
    // 管道注册表实时同步：failed
    usePipelineRegistryStore.getState().applyStreamStatus(pipelineId, 'failed')
  } else {
    _debugLogger.error(
      '[STREAM_ERROR] pipeline_id 缺失，跳过清理: _threadId=%s',
      extractThreadId(eventData)?.slice(0, 12) || 'null',
    )
  }

  if (!pipelineId) return

  // 统一错误信封（config/error_codes.json）：error 可能为对象（{code, message,
  // source, retryable}）或旧形态字符串——提前提取供消息落元数据与通知渲染。
  const errorMsg = eventData?.data?.error || eventData?.error || '流式响应异常'
  // error 为对象时提取 message 保留具体信息，不再降级成通用文案（错误透传收口）。
  const errorText =
    typeof errorMsg === 'string'
      ? errorMsg
      : typeof errorMsg?.message === 'string'
        ? errorMsg.message
        : '生成过程中发生错误，请重试'

  const messageId = extractMessageId(eventData)
  if (messageId) {
    // 清理块协议累积状态（错误终止：残留缓冲丢弃，内容由对账/重试补回）
    clearBlockStateForMessage(pipelineId, messageId)
    // error 为对象时落消息顶层 error 字段（source 渲染来源标签、retryable
    // 驱动重试）；旧形态字符串不落（渲染端已按 status='error' 展示文案）。
    const errorEnvelope =
      typeof errorMsg === 'object' && errorMsg !== null
        ? {
            code: typeof errorMsg.code === 'string' ? errorMsg.code : 'UNKNOWN',
            message: typeof errorMsg.message === 'string' ? errorMsg.message : errorText,
            source: errorMsg.source,
            retryable: errorMsg.retryable,
          }
        : undefined
    pipelineStore.getState().updateMessage(pipelineId, messageId, {
      status: 'error',
      ...(errorEnvelope ? { error: errorEnvelope } : {}),
    } as any)

    // 将所有 streaming 状态的 part 标记为 done/error
    const store = pipelineStore.getState()
    const msg = store.getMessages(pipelineId)?.find((m: any) => m.id === messageId)
    if (msg?.parts) {
      msg.parts.forEach((p: any, i: number) => {
        if (p.type === 'text' || p.type === 'thinking') {
          if (p.state === 'streaming') {
            store.updatePart(pipelineId, messageId, i, { state: 'done' })
          }
        }
        if (p.type === 'tool_call') {
          if (p.state === 'streaming' || p.state === 'calling') {
            store.updatePart(pipelineId, messageId, i, { state: 'error' })
          }
        }
      })
    }
  }

  useNotificationStore.getState().addNotification({
    title: '流式响应错误',
    message: errorText,
    priority: 'high',
    category: 'error',
    isBlocking: false,
    // 统一错误信封来源（config/error_codes.json）：通知中心渲染来源标签
    errorSource:
      typeof errorMsg === 'object' && errorMsg !== null && typeof errorMsg.source === 'string'
        ? errorMsg.source
        : undefined,
  })
}

/** 处理 run 级收尾事件（pipeline_round_finished）——生成态的唯一成功终止信号。
 *
 * 引擎逐轮发射 stream_end（一轮 = 一条消息），轮收尾不代表整次执行结束：
 * 多轮执行的工具轮间曾在此误判，导致执行中发送分支失效（乐观气泡与待发
 * 队列同屏互斥破坏）。stream_end 只收尾当轮消息；终止生成态改由本事件承载，
 * 失败路径 stream_error 已先行终止（此处再终止幂等）。
 */
export function handlePipelineRoundFinished(eventData: any) {
  const pipelineId = resolvePipelineId(eventData)
  if (!pipelineId) {
    _debugLogger.error(
      '[ROUND_FINISHED] pipeline_id 缺失，跳过清理: _threadId=%s',
      extractThreadId(eventData)?.slice(0, 12) || 'null',
    )
    return
  }
  const failed = eventData?.data?.failed === true
  terminatePipeline(pipelineId)
  usePipelineRegistryStore.getState().applyStreamStatus(pipelineId, failed ? 'failed' : 'completed')
}

/** 处理插件执行错误事件（非终止信号）
 *
 * 引擎 warn+继续的插件失败（result.error / invoker Err）经 plugin_error 事件
 * 送达——消息本身正常收尾（new_message/stream_end 照常），此处只弹通知中心
 * （errorSource=plugin），不标记消息失败、不终止管道。统一错误信封
 * （config/error_codes.json）：code 缺省 PLUGIN_EXEC_FAILED，retryable=false。
 */
export function handlePluginError(eventData: any) {
  const pipelineId = resolvePipelineId(eventData)
  if (!pipelineId) {
    _debugLogger.warn(
      '[PLUGIN_ERROR] pipeline_id 缺失，跳过通知: _threadId=%s',
      extractThreadId(eventData)?.slice(0, 12) || 'null',
    )
    return
  }

  const errorMsg = eventData?.data?.error || eventData?.error
  const errorText =
    typeof errorMsg === 'string'
      ? errorMsg
      : typeof errorMsg?.message === 'string'
        ? errorMsg.message
        : '插件执行失败'
  const code =
    typeof errorMsg === 'object' && errorMsg !== null && typeof errorMsg.code === 'string'
      ? errorMsg.code
      : 'PLUGIN_EXEC_FAILED'
  const pluginId = eventData?.data?.plugin_id || eventData?.plugin_id

  _debugLogger.warn(
    '[PLUGIN_ERROR] pipelineId=%s plugin=%s code=%s msg=%s',
    pipelineId.slice(0, 12),
    pluginId || 'unknown',
    code,
    errorText,
  )

  useNotificationStore.getState().addNotification({
    title: '插件执行失败',
    message: pluginId ? `插件 ${pluginId} 执行失败：${errorText}` : `插件执行失败：${errorText}`,
    priority: 'normal',
    category: 'error',
    isBlocking: false,
    errorSource: 'plugin',
  })
}
