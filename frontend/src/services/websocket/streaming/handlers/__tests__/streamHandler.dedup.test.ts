/** @feature FP-T12 前端组件补测 | @ci: frontend-test */
/**
 * streamHandler 失败卡去重表的容量治理补测：
 * 去重表按管道记时；条目数越过容量上界时清掉过期条目（本轮新增条目保留），
 * 保证长跑会话不会无界增长，且清理后去重语义不变。
 *
 * 行为断言：同管道短窗内第二次错误不再弹卡；清理后新管道仍正常弹卡；
 * 通知条数与管道数一致（去重表容量治理不吞新鲜事件）。
 */

import { beforeEach, describe, expect, it } from 'vitest'
import { useNotificationStore } from '@/stores/notificationStore'
import { usePipelineMessageStore } from '@/stores/pipelineMessageStore'
import { usePipelineRegistryStore } from '@/stores/pipelineRegistryStore'
import { handleStreamError } from '../streamHandler'

function streamError(pipelineId: string) {
  // 消息按管道区分：通知中心的「同 title+message 短窗去重」不与待测的
  // 管道级失败卡去重表相互遮蔽
  handleStreamError({
    data: { pipeline_id: pipelineId, error: `引擎执行失败：${pipelineId}` },
  })
}

beforeEach(() => {
  useNotificationStore.setState({ notifications: [] })
  usePipelineRegistryStore.setState({ entries: {} })
  usePipelineMessageStore.setState({
    messagesByPipeline: {},
    activePipelineId: null,
    streamingState: {},
  } as never)
})

describe('handleStreamError — 失败卡去重表容量治理', () => {
  it('条目数越过上界时清理过期条目但保留新鲜条目，去重与弹卡语义不变', () => {
    // 101 个不同管道各弹一张失败卡；第 101 次触发容量清理分支
    for (let i = 0; i < 101; i += 1) {
      streamError(`pipe-cap-${i}`)
    }
    const afterFill = useNotificationStore.getState().notifications.length
    expect(afterFill).toBe(101)

    // 同管道短窗内再报错 → 去重生效，不重复弹卡
    streamError('pipe-cap-0')
    expect(useNotificationStore.getState().notifications.length).toBe(afterFill)

    // 清理后新管道照常弹卡（新鲜条目未被误删）
    streamError('pipe-after-cleanup')
    expect(useNotificationStore.getState().notifications.length).toBe(afterFill + 1)
    // 列表时间倒序：最新条目在最前
    expect(useNotificationStore.getState().notifications.at(0)?.title).toBe('流式响应错误')
  })

  it('缺 pipeline_id 的错误事件不弹卡（无法归因不冒充归因）', () => {
    handleStreamError({ data: { error: '无处归属' } })

    expect(useNotificationStore.getState().notifications).toHaveLength(0)
  })
})
