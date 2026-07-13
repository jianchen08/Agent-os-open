/**
 * ReviewPanel - 审批操作面板
 *
 * 提供通过/驳回按钮、批注计数、整体反馈文本框。
 * 放置在审批视图的底部或侧边。
 */

import { CheckCircle, XCircle, MessageSquare, Send, Loader2 } from 'lucide-react'
import React, { useState } from 'react'
import type { Annotation, ReviewFeedback } from '@/types/review'

export interface ReviewPanelProps {
  /** 已添加的批注列表 */
  annotations: Annotation[]
  /** 提交审批反馈回调 */
  onSubmit: (feedback: ReviewFeedback) => void
  /** 是否正在提交 */
  isSubmitting?: boolean
  /** 是否显示整体反馈输入 */
  showFeedbackInput?: boolean
}

/**
 * ReviewPanel
 *
 * 底部固定的审批操作面板，包含：
 * - 通过 / 驳回按钮
 * - 批注计数显示
 * - 整体反馈文本框
 */
export function ReviewPanel({
  annotations,
  onSubmit,
  isSubmitting = false,
  showFeedbackInput = true,
}: ReviewPanelProps) {
  const [feedbackText, setFeedbackText] = useState('')

  const handleApprove = () => {
    onSubmit({
      requestId: '',
      action: 'approve',
      annotations: [],
      feedbackText: feedbackText.trim() || undefined,
    })
    setFeedbackText('')
  }

  const handleReject = () => {
    onSubmit({
      requestId: '',
      action: 'reject',
      annotations,
      feedbackText: feedbackText.trim() || undefined,
    })
    setFeedbackText('')
  }

  const handleAnnotate = () => {
    if (annotations.length === 0 && !feedbackText.trim()) return
    onSubmit({
      requestId: '',
      action: 'annotate',
      annotations,
      feedbackText: feedbackText.trim() || undefined,
    })
    setFeedbackText('')
  }

  return (
    <div className="review-panel border-t border-border bg-background">
      {/* 批注计数 */}
      {annotations.length > 0 && (
        <div className="flex items-center gap-2 border-b border-border px-4 py-2">
          <MessageSquare className="h-4 w-4 text-yellow-600" />
          <span className="text-sm font-medium text-foreground">
            {annotations.length} 条批注
          </span>
          <div className="ml-auto flex flex-wrap gap-1">
            {annotations.slice(0, 3).map((a) => (
              <span
                key={a.id}
                className="inline-flex items-center rounded-full bg-yellow-100 px-2 py-0.5 text-[10px] text-yellow-800 dark:bg-yellow-900/40 dark:text-yellow-200"
                title={a.suggestion}
              >
                {a.type === 'text_selection' && a.selectedText
                  ? a.selectedText.slice(0, 20) + (a.selectedText.length > 20 ? '...' : '')
                  : a.type === 'image_area'
                    ? '图片标注'
                    : a.type === 'video_timestamp'
                      ? `${a.timestamp?.toFixed(1)}s`
                      : '区域标注'}
              </span>
            ))}
            {annotations.length > 3 && (
              <span className="text-[10px] text-muted-foreground">
                +{annotations.length - 3} 更多
              </span>
            )}
          </div>
        </div>
      )}

      {/* 整体反馈 */}
      {showFeedbackInput && (
        <div className="border-b border-border px-4 py-2">
          <textarea
            value={feedbackText}
            onChange={(e) => setFeedbackText(e.target.value)}
            placeholder="整体反馈意见（可选）..."
            rows={2}
            disabled={isSubmitting}
            className="w-full resize-none rounded-md border border-border bg-background px-3 py-2 text-sm outline-none transition-shadow placeholder:text-muted-foreground/50 focus:ring-1 focus:ring-status-info"
          />
        </div>
      )}

      {/* 操作按钮 */}
      <div className="flex items-center gap-2 px-4 py-3">
        <button
          className="flex items-center gap-1.5 rounded-md bg-green-600 px-4 py-2 text-sm font-medium text-white transition-colors hover:bg-green-700 disabled:opacity-50"
          onClick={handleApprove}
          disabled={isSubmitting}
        >
          {isSubmitting ? (
            <Loader2 className="h-4 w-4 animate-spin" />
          ) : (
            <CheckCircle className="h-4 w-4" />
          )}
          通过
        </button>

        <button
          className="flex items-center gap-1.5 rounded-md bg-red-600 px-4 py-2 text-sm font-medium text-white transition-colors hover:bg-red-700 disabled:opacity-50"
          onClick={handleReject}
          disabled={isSubmitting}
        >
          <XCircle className="h-4 w-4" />
          驳回
        </button>

        {annotations.length > 0 && (
          <button
            className="ml-auto flex items-center gap-1.5 rounded-md border border-border px-3 py-2 text-sm font-medium text-foreground transition-colors hover:bg-accent disabled:opacity-50"
            onClick={handleAnnotate}
            disabled={isSubmitting || (annotations.length === 0 && !feedbackText.trim())}
          >
            <Send className="h-4 w-4" />
            提交批注 ({annotations.length})
          </button>
        )}
      </div>
    </div>
  )
}
