/**
 * ApprovalReviewOverlay - 审批审阅全局浮层（v0.2 P1-2）
 *
 * 监听 WebSocket 上的 `approval.created` 事件（由审批 sidecar 通过
 * event-bus 能力 emit，经内核转发到前端），在全局浮层中渲染审阅界面：
 *   - review 模式：通过 ReviewDocumentWidget 渲染制品 diff + 批注（ApprovalRouter 接线）
 *   - choice/conversation 模式：渲染简单选项/文本输入
 *
 * 用户提交后调用 submitFeedback REST API（src/review 服务）回传结果，
 * 内核审批服务收到 submit 后通过 pipeline-executor 能力 resume 管道。
 *
 * 挂载点：ProtectedRoute 内（与 GlobalInteractionOverlay 同级），所有受保护页面可见。
 */

import { useCallback, useEffect, useState } from 'react'
import { createPortal } from 'react-dom'
import { X, ChevronLeft, ChevronRight } from 'lucide-react'
import { globalWS } from '@/services/websocket/GlobalWebSocket'
import { submitFeedback } from '@/services/api/reviews'
import { ApprovalRouter } from './ApprovalRouter'
import type { Annotation } from '@/types/review'

/** 审批创建事件的 payload 结构（与 approval/server.py _emit_approval_created 对齐） */
interface ApprovalCreatedPayload {
  request_id: string
  task_id?: string
  title?: string
  message?: string
  summary?: string
  options?: string[]
  artifacts?: Array<string | Record<string, unknown>>
  annotations?: Annotation[]
  mode?: 'review' | 'choice' | 'conversation'
  run_id?: string
}

/** 标准化后的待处理审批请求 */
interface PendingApproval {
  requestId: string
  taskId?: string
  title: string
  options: string[]
  artifacts: Array<Record<string, unknown>>
  annotations: Annotation[]
  mode: 'review' | 'choice' | 'conversation'
  runId?: string
}

/** 将原始 artifacts（可能是字符串 ID 或对象）规范化为 ApprovalRouter 可消费的结构 */
function normalizeArtifacts(
  raw: Array<string | Record<string, unknown>> | undefined,
): Array<Record<string, unknown>> {
  if (!Array.isArray(raw)) return []
  return raw.map((item, idx) => {
    if (typeof item === 'string') {
      return { id: item, title: item, content: '' }
    }
    if (item && typeof item === 'object') {
      const obj = item as Record<string, unknown>
      return {
        id: (obj.id as string) ?? `artifact-${idx}`,
        title: (obj.title as string) ?? undefined,
        content: (obj.content as string) ?? undefined,
        baselineContent: (obj.baselineContent as string) ?? (obj.baseline_content as string) ?? undefined,
      }
    }
    return { id: `artifact-${idx}`, content: '' }
  })
}

/** 从 WS 事件原始数据中提取 payload（兼容 data 嵌套与扁平两种封装） */
function extractPayload(rawData: Record<string, unknown>): ApprovalCreatedPayload | null {
  const data = (rawData.data as Record<string, unknown>) || rawData
  const requestId = (data.request_id as string) || (data.approval_id as string)
  if (!requestId) return null
  return {
    request_id: requestId,
    task_id: data.task_id as string | undefined,
    title: data.title as string | undefined,
    message: data.message as string | undefined,
    summary: data.summary as string | undefined,
    options: data.options as string[] | undefined,
    artifacts: data.artifacts as Array<string | Record<string, unknown>> | undefined,
    annotations: data.annotations as Annotation[] | undefined,
    mode: (data.mode as ApprovalCreatedPayload['mode']) || 'review',
    run_id: data.run_id as string | undefined,
  }
}

/**
 * 审批审阅全局浮层
 *
 * 订阅 approval.created 事件，堆叠展示待审阅请求，提交后调用 REST API。
 */
export function ApprovalReviewOverlay() {
  const [pending, setPending] = useState<PendingApproval[]>([])
  const [currentIndex, setCurrentIndex] = useState(0)
  const [submittingId, setSubmittingId] = useState<string | null>(null)

  const handleApprovalCreated = useCallback((rawData: Record<string, unknown>) => {
    const payload = extractPayload(rawData)
    if (!payload) return

    const title =
      payload.title ||
      payload.summary ||
      payload.message ||
      (payload.mode === 'choice' ? '请选择' : payload.mode === 'conversation' ? '请回复' : '文档审阅')

    const item: PendingApproval = {
      requestId: payload.request_id,
      taskId: payload.task_id,
      title,
      options: payload.options ?? [],
      artifacts: normalizeArtifacts(payload.artifacts),
      annotations: payload.annotations ?? [],
      mode: payload.mode ?? 'review',
      runId: payload.run_id,
    }

    setPending((prev) => {
      // 去重：同一 requestId 不重复入队
      if (prev.some((p) => p.requestId === item.requestId)) return prev
      return [...prev, item]
    })
  }, [])

  // 订阅 approval.created 事件（event-bus 能力经内核转发）
  useEffect(() => {
    globalWS.subscribe('approval.created', handleApprovalCreated as any)
    return () => {
      globalWS.unsubscribe('approval.created', handleApprovalCreated as any)
    }
  }, [handleApprovalCreated])

  const current = pending[currentIndex] || null

  // 自动校正索引
  useEffect(() => {
    if (currentIndex >= pending.length) {
      setCurrentIndex(Math.max(0, pending.length - 1))
    }
  }, [pending.length, currentIndex])

  const removeItem = useCallback((requestId: string) => {
    setPending((prev) => prev.filter((p) => p.requestId !== requestId))
  }, [])

  const handleSubmit = useCallback(
    async (result: string, annotations?: Annotation[]) => {
      if (!current) return
      if (submittingId && submittingId !== current.requestId) return
      setSubmittingId(current.requestId)
      try {
        // 调用既有 review 服务回传结果；内核审批服务收到后 resume 管道。
        // 失败时仅 log，不阻塞浮层关闭（避免审批卡死管道）。
        // 注：当前浮层暂未捕获用户新增批注；annotations 参数预留给后续批注编辑场景。
        const firstArtifactId = current.artifacts[0]?.id
        const feedbackAnnotations = (annotations ?? []).map((a) => ({
          artifactId: firstArtifactId != null ? String(firstArtifactId) : '',
          targetType: 'text',
          targetData: {},
          content: a.suggestion,
        }))
        await submitFeedback(current.requestId, {
          responseType: result,
          overallComment: result,
          annotations: feedbackAnnotations,
        }).catch((err) => {
          console.warn('[ApprovalReviewOverlay] submitFeedback failed:', err)
        })
        removeItem(current.requestId)
      } finally {
        setSubmittingId(null)
      }
    },
    [current, submittingId, removeItem],
  )

  // 无待处理时不渲染
  if (!current) return null

  return createPortal(
    <div className="fixed inset-0 z-[10001] flex items-center justify-center">
      {/* 背景遮罩 */}
      <div className="absolute inset-0 bg-black/40" />

      {/* 审阅面板容器 */}
      <div className="relative z-10 flex h-[85vh] w-full max-w-5xl mx-4 flex-col rounded-lg bg-background shadow-2xl">
        {/* 顶栏：导航 + 关闭 */}
        <div className="flex items-center justify-between border-b px-4 py-2">
          <div className="flex items-center gap-2">
            <button
              onClick={() => setCurrentIndex((p) => Math.max(0, p - 1))}
              disabled={currentIndex === 0}
              className="flex h-8 w-8 items-center justify-center rounded-full border border-border/50 hover:bg-muted disabled:opacity-30"
              title="上一个"
            >
              <ChevronLeft className="h-4 w-4" />
            </button>
            <span className="text-muted-foreground min-w-[60px] text-center text-sm">
              {currentIndex + 1} / {pending.length}
            </span>
            <button
              onClick={() => setCurrentIndex((p) => Math.min(pending.length - 1, p + 1))}
              disabled={currentIndex === pending.length - 1}
              className="flex h-8 w-8 items-center justify-center rounded-full border border-border/50 hover:bg-muted disabled:opacity-30"
              title="下一个"
            >
              <ChevronRight className="h-4 w-4" />
            </button>
          </div>
          <h3 className="text-foreground flex-1 truncate px-4 text-base font-semibold">
            {current.title}
          </h3>
          <button
            onClick={() => removeItem(current.requestId)}
            className="text-muted-foreground hover:text-foreground flex h-8 w-8 items-center justify-center rounded-full border border-border/50"
            title="关闭"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        {/* 内容区：通过 ApprovalRouter 接线 ReviewDiff 等子组件 */}
        <div className="min-h-0 flex-1 overflow-auto p-4">
          {current.mode === 'review' ? (
            <ApprovalRouter
              viewMode="text_diff"
              oldContent={
                (current.artifacts[0]?.baselineContent as string) ??
                ''
              }
              newContent={(current.artifacts[0]?.content as string) ?? ''}
              annotations={current.annotations}
            />
          ) : current.mode === 'choice' ? (
            <div className="flex flex-col gap-2">
              {current.options.map((opt) => (
                <button
                  key={opt}
                  onClick={() => handleSubmit(opt)}
                  disabled={submittingId === current.requestId}
                  className="bg-primary/10 text-primary hover:bg-primary/20 rounded-md px-4 py-2 text-left text-sm disabled:opacity-50"
                >
                  {opt}
                </button>
              ))}
            </div>
          ) : (
            <textarea
              className="border-border text-foreground bg-background h-40 w-full rounded-md border p-3 text-sm"
              placeholder="请输入回复..."
              disabled={submittingId === current.requestId}
            />
          )}
        </div>

        {/* 底栏：操作按钮（review 模式提供 approve/reject） */}
        {current.mode === 'review' && (
          <div className="flex justify-end gap-2 border-t px-4 py-3">
            <button
              onClick={() => handleSubmit('rejected')}
              disabled={submittingId === current.requestId}
              className="bg-destructive/10 text-destructive hover:bg-destructive/20 rounded-md px-4 py-2 text-sm disabled:opacity-50"
            >
              拒绝
            </button>
            <button
              onClick={() => handleSubmit('approved')}
              disabled={submittingId === current.requestId}
              className="bg-primary text-primary-foreground hover:bg-primary/90 rounded-md px-4 py-2 text-sm disabled:opacity-50"
            >
              {submittingId === current.requestId ? '提交中...' : '批准'}
            </button>
          </div>
        )}
      </div>
    </div>,
    document.body,
  )
}
