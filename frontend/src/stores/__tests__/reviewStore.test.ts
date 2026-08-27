/**
 * reviewStore 单测：审批请求缓存、反馈提交、WS 事件、待审批计数
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useReviewStore } from '../reviewStore'

describe('reviewStore', () => {
  const originalFetch = globalThis.fetch

  beforeEach(() => {
    useReviewStore.setState({
      reviewRequests: {},
      activeReviewId: null,
      feedbacks: {},
      pendingReviewCount: 0,
      loading: false,
      error: null,
    })
  })

  afterEach(() => {
    globalThis.fetch = originalFetch
    vi.restoreAllMocks()
  })

  describe('fetchReview', () => {
    it('成功：规范化并写入缓存，带 Authorization 头', async () => {
      globalThis.fetch = vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ id: 'r1', taskId: 't1', title: '审批', status: 'pending' }), { status: 200 }),
      )

      const review = await useReviewStore.getState().fetchReview('r1')

      expect(review).toMatchObject({ id: 'r1', taskId: 't1', title: '审批', status: 'pending' })
      expect(useReviewStore.getState().reviewRequests['r1']).toBe(review)
      expect(useReviewStore.getState().loading).toBe(false)
      const [url, init] = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls[0]
      expect(url).toBe('/api/v1/reviews/r1')
      expect((init as RequestInit).headers).toMatchObject({ Authorization: 'Bearer null' })
    })

    it('响应含 error → 返回 null 且 error 置位', async () => {
      globalThis.fetch = vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ error: { message: 'denied' } }), { status: 403 }),
      )

      const review = await useReviewStore.getState().fetchReview('r1')

      expect(review).toBeNull()
      expect(useReviewStore.getState().error).toBe('denied')
    })

    it('网络异常 → 返回 null 且 error 置位', async () => {
      globalThis.fetch = vi.fn().mockRejectedValue(new Error('network down'))

      const review = await useReviewStore.getState().fetchReview('r1')

      expect(review).toBeNull()
      expect(useReviewStore.getState().error).toBe('network down')
    })
  })

  describe('fetchReviewsByTask', () => {
    it('成功：统计 pending/in_review 计数', async () => {
      globalThis.fetch = vi.fn().mockResolvedValue(
        new Response(JSON.stringify({
          items: [
            { id: 'r1', status: 'pending' },
            { id: 'r2', status: 'in_review' },
            { id: 'r3', status: 'approved' },
          ],
        }), { status: 200 }),
      )

      const items = await useReviewStore.getState().fetchReviewsByTask('t1')

      expect(items).toHaveLength(3)
      expect(useReviewStore.getState().pendingReviewCount).toBe(2)
      expect(globalThis.fetch).toHaveBeenCalledWith('/api/v1/reviews?task_id=t1', expect.anything())
    })

    it('网络异常 → 返回 [] 且 error 置位', async () => {
      globalThis.fetch = vi.fn().mockRejectedValue(new Error('boom'))

      const items = await useReviewStore.getState().fetchReviewsByTask('t1')

      expect(items).toEqual([])
      expect(useReviewStore.getState().error).toBe('boom')
    })
  })

  describe('submitFeedback', () => {
    it('approved：反馈入缓存、审批状态置 approved、请求体字段映射', async () => {
      globalThis.fetch = vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ id: 'fb1', reviewRequestId: 'r1', responseType: 'approved' }), { status: 200 }),
      )
      useReviewStore.setState({
        reviewRequests: { r1: { id: 'r1', taskId: '', threadId: '', sessionId: '', tabId: '', title: '', description: '', artifactIds: [], status: 'pending', priority: 'normal', timeoutSeconds: 86400, createdAt: '', updatedAt: '', metadata: {} } },
      })

      const fb = await useReviewStore.getState().submitFeedback('r1', {
        responseType: 'approved',
        overallComment: 'ok',
        annotations: [{ artifactId: 'a1', targetType: 'whole_artifact', targetData: {}, content: 'c' }],
        userId: 'u1',
      })

      expect(fb).toMatchObject({ id: 'fb1', responseType: 'approved' })
      expect(useReviewStore.getState().feedbacks['r1']).toBe(fb)
      expect(useReviewStore.getState().reviewRequests['r1'].status).toBe('approved')
      const [, init] = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls[0]
      const body = JSON.parse((init as RequestInit).body as string)
      expect(body).toEqual({
        response_type: 'approved',
        overall_comment: 'ok',
        annotations: [{ artifact_id: 'a1', target_type: 'whole_artifact', target_data: {}, content: 'c' }],
        user_id: 'u1',
      })
    })

    it('denied：审批状态置 rejected', async () => {
      globalThis.fetch = vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ id: 'fb2', reviewRequestId: 'r1', responseType: 'denied' }), { status: 200 }),
      )
      useReviewStore.setState({
        reviewRequests: { r1: { id: 'r1', taskId: '', threadId: '', sessionId: '', tabId: '', title: '', description: '', artifactIds: [], status: 'pending', priority: 'normal', timeoutSeconds: 86400, createdAt: '', updatedAt: '', metadata: {} } },
      })

      await useReviewStore.getState().submitFeedback('r1', { responseType: 'denied', overallComment: 'no' })

      expect(useReviewStore.getState().reviewRequests['r1'].status).toBe('rejected')
    })

    it('响应含 error → 返回 null 且 error 置位', async () => {
      globalThis.fetch = vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ error: { message: 'bad' } }), { status: 400 }),
      )

      const fb = await useReviewStore.getState().submitFeedback('r1', { responseType: 'approved', overallComment: '' })

      expect(fb).toBeNull()
      expect(useReviewStore.getState().error).toBe('bad')
    })

    it('网络异常 → 返回 null 且 error 置位', async () => {
      globalThis.fetch = vi.fn().mockRejectedValue(new Error('boom'))

      const fb = await useReviewStore.getState().submitFeedback('r1', { responseType: 'approved', overallComment: '' })

      expect(fb).toBeNull()
      expect(useReviewStore.getState().error).toBe('boom')
    })
  })

  describe('markAsViewed / cancelReview', () => {
    it('markAsViewed 成功：状态置 in_review 返回 true', async () => {
      globalThis.fetch = vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ viewed: true }), { status: 200 }),
      )
      useReviewStore.setState({
        reviewRequests: { r1: { id: 'r1', taskId: '', threadId: '', sessionId: '', tabId: '', title: '', description: '', artifactIds: [], status: 'pending', priority: 'normal', timeoutSeconds: 86400, createdAt: '', updatedAt: '', metadata: {} } },
      })

      const ok = await useReviewStore.getState().markAsViewed('r1')

      expect(ok).toBe(true)
      expect(useReviewStore.getState().reviewRequests['r1'].status).toBe('in_review')
    })

    it('markAsViewed 未确认 → 返回 false 不改状态', async () => {
      globalThis.fetch = vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ viewed: false }), { status: 200 }),
      )

      const ok = await useReviewStore.getState().markAsViewed('r1')

      expect(ok).toBe(false)
    })

    it('markAsViewed 异常 → 返回 false 不抛', async () => {
      globalThis.fetch = vi.fn().mockRejectedValue(new Error('boom'))

      const ok = await useReviewStore.getState().markAsViewed('r1')

      expect(ok).toBe(false)
    })

    it('cancelReview 成功：状态置 cancelled 返回 true', async () => {
      globalThis.fetch = vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ cancelled: true }), { status: 200 }),
      )
      useReviewStore.setState({
        reviewRequests: { r1: { id: 'r1', taskId: '', threadId: '', sessionId: '', tabId: '', title: '', description: '', artifactIds: [], status: 'pending', priority: 'normal', timeoutSeconds: 86400, createdAt: '', updatedAt: '', metadata: {} } },
      })

      const ok = await useReviewStore.getState().cancelReview('r1', '不再需要')

      expect(ok).toBe(true)
      expect(useReviewStore.getState().reviewRequests['r1'].status).toBe('cancelled')
      const [, init] = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls[0]
      expect(JSON.parse((init as RequestInit).body as string)).toEqual({ reason: '不再需要' })
    })

    it('cancelReview 未确认 → 返回 false', async () => {
      globalThis.fetch = vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ cancelled: false }), { status: 200 }),
      )

      const ok = await useReviewStore.getState().cancelReview('r1')

      expect(ok).toBe(false)
    })
  })

  describe('WS 事件与查询', () => {
    it('addReviewFromWS：创建 pending 审批并计数 +1，缺省字段兜底', () => {
      useReviewStore.getState().addReviewFromWS({ review_id: 'r1', task_id: 't1', title: '审批' })

      const review = useReviewStore.getState().reviewRequests['r1']
      expect(review).toMatchObject({
        id: 'r1',
        taskId: 't1',
        title: '审批',
        status: 'pending',
        priority: 'normal',
        timeoutSeconds: 86400,
        artifactIds: [],
      })
      expect(useReviewStore.getState().pendingReviewCount).toBe(1)
    })

    it('updateReviewStatusFromWS：pending → approved 计数 -1', () => {
      const s = useReviewStore.getState()
      s.addReviewFromWS({ review_id: 'r1' })
      s.updateReviewStatusFromWS({ review_id: 'r1', status: 'approved' })

      const state = useReviewStore.getState()
      expect(state.reviewRequests['r1'].status).toBe('approved')
      expect(state.pendingReviewCount).toBe(0)
    })

    it('updateReviewStatusFromWS：approved → in_review 计数 +1', () => {
      const s = useReviewStore.getState()
      s.addReviewFromWS({ review_id: 'r1' })
      s.updateReviewStatusFromWS({ review_id: 'r1', status: 'approved' })
      s.updateReviewStatusFromWS({ review_id: 'r1', status: 'in_review' })

      expect(useReviewStore.getState().pendingReviewCount).toBe(1)
    })

    it('updateReviewStatusFromWS：未知 review_id 不改变状态', () => {
      const s = useReviewStore.getState()
      s.addReviewFromWS({ review_id: 'r1' })
      s.updateReviewStatusFromWS({ review_id: 'ghost', status: 'approved' })

      const state = useReviewStore.getState()
      expect(state.reviewRequests['r1'].status).toBe('pending')
      expect(state.pendingReviewCount).toBe(1)
    })

    it('setActiveReview 设置当前审查 ID', () => {
      useReviewStore.getState().setActiveReview('r1')
      expect(useReviewStore.getState().activeReviewId).toBe('r1')
      useReviewStore.getState().setActiveReview(null)
      expect(useReviewStore.getState().activeReviewId).toBeNull()
    })

    it('getPendingForContainer 只返回 pending/in_review', () => {
      const s = useReviewStore.getState()
      s.addReviewFromWS({ review_id: 'r1' })
      s.addReviewFromWS({ review_id: 'r2' })
      s.updateReviewStatusFromWS({ review_id: 'r2', status: 'approved' })

      const pending = useReviewStore.getState().getPendingForContainer()

      expect(pending.map((r) => r.id)).toEqual(['r1'])
    })
  })
})
