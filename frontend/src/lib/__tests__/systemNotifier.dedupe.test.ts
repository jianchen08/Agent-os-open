/** @feature FP-0.2.四 系统通知幂等记忆有界淘汰（notifySystemMessage 直测） | @ci: frontend-test */
/**
 * dedupeKey 幂等 + 有界记忆契约：同 key 重复投递只决策一次；记忆超上限
 * （500）按插入序淘汰最旧——被淘汰的旧 key 重新投递可再次决策（有界不等于
 * 永久静默）。showSystemNotification 打桩隔离浏览器通知面。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/utils/systemNotification', () => ({
  showSystemNotification: vi.fn().mockResolvedValue(true),
}))

import { notifyPipelineMessage, resetSystemNotifierForTest } from '@/lib/systemNotifier'

beforeEach(() => {
  resetSystemNotifierForTest()
})

describe('notifyPipelineMessage dedupeKey 幂等与有界淘汰', () => {
  it('同 dedupeKey 只通知一次', async () => {
    const opts = { title: 't', body: '正文', notifyWorthy: true, dedupeKey: 'msg-1' }
    await expect(notifyPipelineMessage(opts)).resolves.toBe(true)
    await expect(notifyPipelineMessage(opts)).resolves.toBe(false)
  })

  it('记忆超上限：最旧 key 被淘汰后重新可通知', async () => {
    for (let i = 0; i < 501; i++) {
      await notifyPipelineMessage({ title: 't', body: 'b', notifyWorthy: false, dedupeKey: `k-${i}` })
    }
    // k-0 已被淘汰出记忆；k-500 仍在记忆内。notifyWorthy=false 时两者都返回
    // false（决策短路在幂等命中之后），用 true 复投验证 k-0 恢复可决策。
    await expect(
      notifyPipelineMessage({ title: 't', body: 'b', notifyWorthy: true, dedupeKey: 'k-0' }),
    ).resolves.toBe(true)
    await expect(
      notifyPipelineMessage({ title: 't', body: 'b', notifyWorthy: true, dedupeKey: 'k-500' }),
    ).resolves.toBe(false)
  })
})
