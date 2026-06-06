/**
 * Review Store
 *
 * 管理审批请求和反馈的状态。
 */

import { create } from 'zustand'
import type { ReviewRequest, ReviewFeedback } from '@/types/review'
import { tokenManager } from './tokenManager'

interface ReviewState {
  /** 以 review_id 为 key 的审批请求缓存 */
  reviewRequests: Record<string, ReviewRequest>
  /** 当前正在审查的审批 ID */
  activeReviewId: string | null
  /** 已提交的反馈缓存 */
  feedbacks: Record<string, ReviewFeedback>
  /** 待处理审批数量 */
  pendingReviewCount: number
  /** 加载状态 */
  loading: boolean
  /** 错误信息 */
  error: string | null
}

interface ReviewActions {
  /** 加载审批详情 */
  fetchReview: (reviewId: string) => Promise<ReviewRequest | null>
  /** 加载任务的审批列表 */
  fetchReviewsByTask: (taskId: string) => Promise<ReviewRequest[]>
  /** 提交审批反馈 */
  submitFeedback: (reviewId: string, feedback: {
    responseType: string
    overallComment: string
    annotations?: Array<{ artifactId: string; targetType: string; targetData: Record<string, any>; content: string }>
    userId?: string
  }) => Promise<ReviewFeedback | null>
  /** 标记已查看 */
  markAsViewed: (reviewId: string) => Promise<boolean>
  /** 取消审批 */
  cancelReview: (reviewId: string, reason?: string) => Promise<boolean>
  /** 处理 WebSocket review_request 事件 */
  addReviewFromWS: (event: {
    review_id: string
    task_id?: string
    thread_id?: string
    session_id?: string
    tab_id?: string
    title?: string
    description?: string
    artifact_ids?: string[]
    priority?: string
    timeout_seconds?: number
    [key: string]: any
  }) => void
  /** 处理 WebSocket review_status_update 事件 */
  updateReviewStatusFromWS: (event: {
    review_id: string
    status: string
    [key: string]: any
  }) => void
  /** 设置当前审查的审批 */
  setActiveReview: (reviewId: string | null) => void
  /** 获取容器任务的待审批列表 */
  getPendingForContainer: (containerTaskId: string) => ReviewRequest[]
}

const API_BASE = '/api/v1/reviews'

export const useReviewStore = create<ReviewState & ReviewActions>()((set, get) => ({
  reviewRequests: {},
  activeReviewId: null,
  feedbacks: {},
  pendingReviewCount: 0,
  loading: false,
  error: null,

  fetchReview: async (reviewId) => {
    set({ loading: true, error: null })
    try {
      const token = tokenManager.getToken()
      const resp = await fetch(`${API_BASE}/${reviewId}`, {
        headers: { Authorization: `Bearer ${token}` },
      })
      const data = await resp.json()
      if (data.error) {
        set({ loading: false, error: data.error.message })
        return null
      }
      const review = _normalizeReview(data)
      set((state) => ({
        reviewRequests: { ...state.reviewRequests, [review.id]: review },
        loading: false,
      }))
      return review
    } catch (e: any) {
      set({ loading: false, error: e.message })
      return null
    }
  },

  fetchReviewsByTask: async (taskId) => {
    set({ loading: true, error: null })
    try {
      const token = tokenManager.getToken()
      const resp = await fetch(`${API_BASE}?task_id=${encodeURIComponent(taskId)}`, {
        headers: { Authorization: `Bearer ${token}` },
      })
      const data = await resp.json()
      const items = (data.items || []).map(_normalizeReview)
      set((state) => {
        const updated = { ...state.reviewRequests }
        let pendingCount = 0
        for (const r of items) {
          updated[r.id] = r
          if (r.status === 'pending' || r.status === 'in_review') {
            pendingCount++
          }
        }
        return { reviewRequests: updated, loading: false, pendingReviewCount: pendingCount }
      })
      return items
    } catch (e: any) {
      set({ loading: false, error: e.message })
      return []
    }
  },

  submitFeedback: async (reviewId, feedback) => {
    set({ loading: true, error: null })
    try {
      const body: Record<string, any> = {
        response_type: feedback.responseType,
        overall_comment: feedback.overallComment,
      }
      if (feedback.annotations) {
        body.annotations = feedback.annotations.map((a) => ({
          artifact_id: a.artifactId,
          target_type: a.targetType,
          target_data: a.targetData,
          content: a.content,
        }))
      }
      if (feedback.userId) {
        body.user_id = feedback.userId
      }
      const token = tokenManager.getToken()
      const resp = await fetch(`${API_BASE}/${reviewId}/feedback`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${token}`,
        },
        body: JSON.stringify(body),
      })
      const data = await resp.json()
      if (data.error) {
        set({ loading: false, error: data.error.message })
        return null
      }
      const fb = _normalizeFeedback(data)
      set((state) => ({
        feedbacks: { ...state.feedbacks, [reviewId]: fb },
        loading: false,
      }))
      // 更新审批状态
      set((s) => {
        const review = s.reviewRequests[reviewId]
        if (!review) return {}
        const newStatus = feedback.responseType === 'denied' ? 'rejected' :
          feedback.responseType === 'approved' ? 'approved' : review.status
        return {
          reviewRequests: {
            ...s.reviewRequests,
            [reviewId]: { ...review, status: newStatus as any },
          },
        }
      })
      return fb
    } catch (e: any) {
      set({ loading: false, error: e.message })
      return null
    }
  },

  markAsViewed: async (reviewId) => {
    try {
      const token = tokenManager.getToken()
      const resp = await fetch(`${API_BASE}/${reviewId}/viewed`, {
        method: 'POST',
        headers: { Authorization: `Bearer ${token}` },
      })
      const data = await resp.json()
      if (data.viewed) {
        set((state) => {
          const review = state.reviewRequests[reviewId]
          if (!review) return state
          return {
            reviewRequests: {
              ...state.reviewRequests,
              [reviewId]: { ...review, status: 'in_review' },
            },
          }
        })
        return true
      }
      return false
    } catch {
      return false
    }
  },

  cancelReview: async (reviewId, reason) => {
    try {
      const token = tokenManager.getToken()
      const resp = await fetch(`${API_BASE}/${reviewId}/cancel`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${token}`,
        },
        body: JSON.stringify({ reason }),
      })
      const data = await resp.json()
      if (data.cancelled) {
        set((state) => {
          const review = state.reviewRequests[reviewId]
          if (!review) return state
          return {
            reviewRequests: {
              ...state.reviewRequests,
              [reviewId]: { ...review, status: 'cancelled' },
            },
          }
        })
        return true
      }
      return false
    } catch {
      return false
    }
  },

  addReviewFromWS: (event) => {
    const review: ReviewRequest = {
      id: event.review_id,
      taskId: event.task_id ?? '',
      threadId: event.thread_id ?? '',
      sessionId: event.session_id ?? '',
      tabId: event.tab_id ?? '',
      title: event.title ?? '',
      description: event.description ?? '',
      artifactIds: event.artifact_ids ?? [],
      status: 'pending',
      priority: (event.priority as any) ?? 'normal',
      timeoutSeconds: event.timeout_seconds ?? 86400,
      metadata: {},
      createdAt: new Date().toISOString(),
      updatedAt: new Date().toISOString(),
    }
    set((state) => ({
      reviewRequests: { ...state.reviewRequests, [review.id]: review },
      pendingReviewCount: state.pendingReviewCount + 1,
    }))
  },

  updateReviewStatusFromWS: (event) => {
    const { review_id, status } = event
    set((state) => {
      const review = state.reviewRequests[review_id]
      if (!review) return state
      const prevPending = review.status === 'pending' || review.status === 'in_review' ? 1 : 0
      const newPending = status === 'pending' || status === 'in_review' ? 1 : 0
      return {
        reviewRequests: {
          ...state.reviewRequests,
          [review_id]: { ...review, status: status as any },
        },
        pendingReviewCount: state.pendingReviewCount - prevPending + newPending,
      }
    })
  },

  setActiveReview: (reviewId) => {
    set({ activeReviewId: reviewId })
  },

  getPendingForContainer: (containerTaskId) => {
    const { reviewRequests } = get()
    return Object.values(reviewRequests).filter(
      (r) => r.status === 'pending' || r.status === 'in_review',
    )
  },
}))

/** 将后端 API 响应转换为前端 ReviewRequest 类型 */
function _normalizeReview(data: Record<string, any>): ReviewRequest {
  if (!data.id) {
    console.error('[reviewStore] _normalizeReview: id 字段缺失', data)
  }
  return {
    id: data.id ?? '',
    taskId: data.taskId ?? '',
    threadId: data.threadId ?? '',
    sessionId: data.sessionId ?? '',
    tabId: data.tabId ?? '',
    title: data.title ?? '',
    description: data.description ?? '',
    artifactIds: data.artifactIds ?? [],
    status: data.status ?? 'pending',
    priority: data.priority ?? 'normal',
    timeoutSeconds: data.timeoutSeconds ?? 86400,
    createdAt: data.createdAt ?? '',
    updatedAt: data.updatedAt ?? '',
    reviewedAt: data.reviewedAt,
    completedAt: data.completedAt,
    metadata: data.metadata ?? {},
  }
}

function _normalizeFeedback(data: Record<string, any>): ReviewFeedback {
  if (!data.id) {
    console.error('[reviewStore] _normalizeFeedback: id 字段缺失', data)
  }
  return {
    id: data.id ?? '',
    reviewRequestId: data.reviewRequestId ?? '',
    responseType: data.responseType ?? 'approved',
    overallComment: data.overallComment ?? '',
    annotations: data.annotations ?? [],
    userId: data.userId,
    createdAt: data.createdAt ?? '',
  }
}
