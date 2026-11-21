/** 发送终局消费（落定语义，2026-11-04）：store 落 landSendByCmid / endSendByCmid /
 *  5s 超时三类 outcome，本 hook 按 id 增量消费——
 *  - landed：本管道在途全部落定（按计数）且当前文本 == 受理快照（比对守卫）→
 *    清空输入面（onLanded）；
 *  - timeout：判死出口 → 操作点报错（2026-11-18 修订：超时不自动停 run，reason
 *    优先透出——默认文案=未确认送达可重试，acked 文案不诱导重发）；
 *  - failed：判死出口的操作点报错（文本未离开输入框，天然可重试）。
 *  挂载即消费一次：组件不存活期间到达的终局经草稿 store 落地（清草稿），
 *  重挂载重放历史终局收敛视图。 */

import { useEffect, useRef } from 'react'
import { useChatInputStore } from '@/stores/chatInputStore'

/** 终局消费的组件侧接缝 */
export interface SendOutcomeConsumerOptions {
  /** 草稿键（同一输入器的稳定锚；无草稿键的发送按受理快照文本归属） */
  draftKey?: string
  /** 当前正文（与受理快照比对：文本已被改写则不清空） */
  textRef: { current: string }
  /** 本管道在途全部落定且文本未变 → 清空输入面 */
  onLanded: () => void
  /** 判死出口（超时/失败）的操作点报错 */
  onError: (message: string) => void
}

/** 订阅发送终局并按 id 增量消费（挂载即消费一次，重挂载重放收敛视图） */
export function useSendOutcomeConsumer({
  draftKey,
  textRef,
  onLanded,
  onError,
}: SendOutcomeConsumerOptions): void {
  const lastOutcomeIdRef = useRef(0)
  useEffect(() => {
    const consumeSendOutcomes = () => {
      const { sendOutcomes, sendInFlightByPipeline } = useChatInputStore.getState()
      for (const outcome of sendOutcomes) {
        if (outcome.id <= lastOutcomeIdRef.current) continue
        lastOutcomeIdRef.current = outcome.id
        // 归属判定：有草稿键按草稿键（同一输入器的稳定锚）；无草稿键的发送按
        // 快照文本匹配（语音/外部插入等不带草稿键的路径）
        const mine = outcome.draftKey
          ? outcome.draftKey === draftKey
          : outcome.content === textRef.current
        if (!mine) continue
        if (outcome.kind === 'landed') {
          const remaining = sendInFlightByPipeline[outcome.pipelineId]?.length ?? 0
          if (remaining === 0 && textRef.current === outcome.content) {
            onLanded()
          }
        } else if (outcome.kind === 'timeout') {
          // 默认文案 = 未确认送达可重试；ack 已受理的超时（宽限耗尽）随 reason
          // 透出——文案不诱导重发（消息在服务端排队/执行中）
          onError(outcome.reason ?? '发送超时，未收到送达确认——请重试')
        } else {
          onError(outcome.reason ? `消息发送失败：${outcome.reason}` : '消息发送失败，请重新发送')
        }
      }
    }
    consumeSendOutcomes()
    return useChatInputStore.subscribe(consumeSendOutcomes)
  }, [draftKey, textRef, onLanded, onError])
}
