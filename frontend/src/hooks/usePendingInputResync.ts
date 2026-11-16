/**
 * 待处理输入队列对账（F10：WS 断线期间的队列变更事件丢失后，条目永久挂显示）
 *
 * 断线窗口内内核侧 consumed/deleted 事件到不了前端，本地镜像残留已消费条目
 * （2026-10-23 实证：内核重启窗口后队列条挂 3 条"待处理"，而内核队列实为空）。
 * 本钩子提供三通道 GET 全量对账（已消费条目后端已删，天然收敛）：
 * 队列条挂载 / WS 重连（WS_LOCAL_EVENTS.RECONNECTED，同 useInteractionHandler
 * 恢复范式）/ 有界兜底轮询。**无条件启用**（2026-10-29）：本地镜像为空时若
 * 漏接 enqueued 事件，旧 enabled=false 会让队列条永久不出现——空队列也必须
 * 拉一次权威列表，这是唯一自愈通道。
 */
import { useEffect } from 'react'
import { WS_LOCAL_EVENTS } from '@/constants/websocket'
import { globalWS } from '@/services/websocket/GlobalWebSocket'
import { usePendingInputStore } from '@/stores/pendingInputStore'

/** 兜底轮询间隔（有界补偿：推送丢失时不再依赖"恰好送达"单点） */
const RESYNC_POLL_INTERVAL_MS = 60_000

export function usePendingInputResync(pipelineId: string): void {
  useEffect(() => {
    if (!pipelineId) return
    void usePendingInputStore.getState().load(pipelineId)

    const handleReconnect = (): void => {
      void usePendingInputStore.getState().load(pipelineId)
    }
    globalWS.subscribe(WS_LOCAL_EVENTS.RECONNECTED, handleReconnect)
    const pollTimer = setInterval(
      () => void usePendingInputStore.getState().load(pipelineId),
      RESYNC_POLL_INTERVAL_MS,
    )
    return () => {
      clearInterval(pollTimer)
      globalWS.unsubscribe(WS_LOCAL_EVENTS.RECONNECTED, handleReconnect)
    }
  }, [pipelineId])
}
