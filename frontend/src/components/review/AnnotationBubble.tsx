/**
 * AnnotationBubble - 批注气泡组件
 *
 * 选中文字后弹出，显示选中文本片段和修改建议输入框。
 * 浮动定位在选中文本附近。
 */

import { MessageSquare, X, Send } from 'lucide-react'
import React, { useState, useRef, useEffect } from 'react'

export interface AnnotationBubbleProps {
  /** 选中的文字 */
  selectedText: string
  /** 气泡定位（相对于父容器） */
  position: { x: number; y: number }
  /** 提交批注回调 */
  onSubmit: (suggestion: string) => void
  /** 取消回调 */
  onCancel: () => void
}

/** 选中文本的最大展示长度 */
const MAX_DISPLAY_LENGTH = 60

/**
 * AnnotationBubble
 *
 * 浮动定位在选中文本旁边，包含：
 * - 选中文字片段（截断显示）
 * - 修改建议文本输入框
 * - 提交 / 取消按钮
 */
export function AnnotationBubble({
  selectedText,
  position,
  onSubmit,
  onCancel,
}: AnnotationBubbleProps) {
  const [suggestion, setSuggestion] = useState('')
  const inputRef = useRef<HTMLTextAreaElement>(null)

  // 自动聚焦输入框
  useEffect(() => {
    // 短延迟确保 DOM 已渲染
    const timer = setTimeout(() => {
      inputRef.current?.focus()
    }, 50)
    return () => clearTimeout(timer)
  }, [])

  const handleSubmit = () => {
    const trimmed = suggestion.trim()
    if (!trimmed) return
    onSubmit(trimmed)
    setSuggestion('')
  }

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      handleSubmit()
    }
    if (e.key === 'Escape') {
      onCancel()
    }
  }

  /** 截断显示选中文字 */
  const displayText =
    selectedText.length > MAX_DISPLAY_LENGTH
      ? selectedText.slice(0, MAX_DISPLAY_LENGTH) + '...'
      : selectedText

  return (
    <div
      className="annotation-bubble pointer-events-auto absolute z-50"
      style={{
        left: position.x,
        top: position.y,
        transform: 'translate(-50%, -100%)',
      }}
    >
      <div className="w-72 overflow-hidden rounded-lg border border-border bg-background shadow-xl">
        {/* 标题栏 */}
        <div className="flex items-center justify-between border-b border-border bg-muted/50 px-3 py-2">
          <div className="flex items-center gap-1.5 text-xs font-medium text-foreground">
            <MessageSquare className="h-3.5 w-3.5 text-yellow-600" />
            批注
          </div>
          <button
            className="flex h-5 w-5 items-center justify-center rounded text-xs text-muted-foreground hover:bg-accent hover:text-foreground"
            onClick={onCancel}
            title="取消"
          >
            <X className="h-3 w-3" />
          </button>
        </div>

        {/* 选中文字 */}
        <div className="border-b border-border px-3 py-2">
          <div className="text-xs text-muted-foreground mb-1">选中：</div>
          <div className="rounded bg-yellow-100 px-2 py-1 text-xs text-yellow-900 dark:bg-yellow-900/40 dark:text-yellow-200">
            &ldquo;{displayText}&rdquo;
          </div>
        </div>

        {/* 修改建议输入 */}
        <div className="px-3 py-2">
          <div className="text-xs text-muted-foreground mb-1">修改建议：</div>
          <textarea
            ref={inputRef}
            value={suggestion}
            onChange={(e) => setSuggestion(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder="输入修改建议..."
            rows={2}
            className="w-full resize-none rounded-md border border-border bg-background px-2 py-1.5 text-sm outline-none transition-shadow placeholder:text-muted-foreground/50 focus:ring-1 focus:ring-yellow-500"
          />
        </div>

        {/* 操作按钮 */}
        <div className="flex items-center justify-end gap-2 border-t border-border px-3 py-2">
          <button
            className="rounded-md px-3 py-1 text-xs text-muted-foreground hover:bg-accent hover:text-foreground transition-colors"
            onClick={onCancel}
          >
            取消
          </button>
          <button
            className="flex items-center gap-1 rounded-md bg-yellow-600 px-3 py-1 text-xs font-medium text-white hover:bg-yellow-700 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
            onClick={handleSubmit}
            disabled={!suggestion.trim()}
          >
            <Send className="h-3 w-3" />
            添加批注
          </button>
        </div>
      </div>
    </div>
  )
}
