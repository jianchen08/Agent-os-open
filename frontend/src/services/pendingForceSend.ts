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
  // 本地队列视图只是镜像，不是发送前置条件（2026-11-14）：条目不在本地视图
  // （事件漏接/对账滞后/多端并发）时旧实现只提示不发——按钮点了没反应。
  // 现在恒发帧由内核裁决：条目真在队列则强发生效；已消费则回 route_error
  // 通知（后端原因可见），同时刷新本地视图收敛。
  if (!target) {
    useNotificationStore.getState().addNotification({
      title: '正在发送该消息',
      message: '本地队列视图未命中该条目（可能已消费），已提交后端裁决并按回执收敛',
      priority: 'normal',
      category: 'alert',
      isBlocking: false,
      autoDismissMs: 6000,
      sourceLabel: '系统',
    })
    void usePendingInputStore.getState().load(pipelineId)
  }
  globalWS.sendForcePendingInput(sessionId, { pipelineId, inputId })
}
