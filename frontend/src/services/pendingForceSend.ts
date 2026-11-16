/**
 * 队列条目强制发送（2026-10-19 用户裁定；2026-10-27 权威化单帧原语）
 *
 * 语义：选中的那条立即发出，其余排队条目全程留队继续排队。
 * 编排本体在内核 dispatch_force_pending：目标条目重排至队首 + 停当前回复
 * （不退回队列）+ 链尾 drain 首个消费目标（原参数原样保留）。
 * 旧「DELETE→停止→重发」前端编排退役——停止的队列退回语义会把其余排队
 * 条目一并退回输入框（2026-10-27 用户实测），且重发在链忙时只能排队尾，
 * 无法保证目标条目先跑。
 */
import { globalWS } from '@/services/websocket/GlobalWebSocket'
import { useNotificationStore } from '@/stores/notificationStore'
import { usePendingInputStore } from '@/stores/pendingInputStore'

export interface ForceSendPendingOptions {
  /** 会话坐标（WS 帧的 thread 绑定） */
  sessionId: string
  /** 队列所在管道（与队列条同一坐标） */
  pipelineId: string
  /** 目标条目 ID */
  inputId: string
}

export async function forceSendPendingInput(opts: ForceSendPendingOptions): Promise<void> {
  const { sessionId, pipelineId, inputId } = opts
  const target = (usePendingInputStore.getState().byPipeline[pipelineId] ?? []).find(
    (i) => i.id === inputId,
  )
  if (!target) {
    // 条目已被消费（队列视图滞后）：对账收敛即愈，不发重复消息
    void usePendingInputStore.getState().load(pipelineId)
    useNotificationStore.getState().addNotification({
      title: '未发送该消息',
      message: '该条目已不在队列中（可能已被消费），队列视图已刷新',
      priority: 'normal',
      category: 'alert',
      isBlocking: false,
      autoDismissMs: 6000,
      sourceLabel: '系统',
    })
    return
  }
  globalWS.sendForcePendingInput(sessionId, { pipelineId, inputId })
}
