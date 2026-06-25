/**
 * ReviewableDocument - 可批注的文档渲染组件
 *
 * 基于 MarkdownRenderer 渲染内容，支持文字选中批注交互。
 * 选中文字后弹出 AnnotationBubble，已有批注高亮显示。
 */

import React, { useState, useCallback, useRef, useEffect } from 'react'
import { AnnotationBubble } from './AnnotationBubble'
import { MarkdownRenderer } from '../markdown/MarkdownRenderer'
import type { Annotation } from '@/types/review'

/** 生成简易唯一 ID */
function uid(): string {
  return Date.now().toString(36) + Math.random().toString(36).slice(2)
}

export interface ReviewableDocumentProps {
  /** 文档文本内容（Markdown） */
  content: string
  /** 已有批注列表 */
  annotations: Annotation[]
  /** 添加批注回调 */
  onAddAnnotation?: (annotation: Annotation) => void
  /** 删除批注回调 */
  onRemoveAnnotation?: (id: string) => void
  /** 是否只读模式 */
  readOnly?: boolean
}

/** 气泡定位信息 */
interface BubblePosition {
  x: number
  y: number
}

/**
 * ReviewableDocument
 *
 * 渲染 Markdown 文档，监听文字选中事件，选中后弹出批注气泡。
 * 已有批注以黄色高亮 + 侧边标记方式显示。
 */
export function ReviewableDocument({
  content,
  annotations,
  onAddAnnotation,
  onRemoveAnnotation,
  readOnly = false,
}: ReviewableDocumentProps) {
  const containerRef = useRef<HTMLDivElement>(null)
  const [bubble, setBubble] = useState<{
    visible: boolean
    selectedText: string
    position: BubblePosition
  }>({ visible: false, selectedText: '', position: { x: 0, y: 0 } })

  /** 文字选中后弹出批注气泡 */
  const handleMouseUp = useCallback(() => {
    if (readOnly) return

    const selection = window.getSelection()
    if (!selection || selection.isCollapsed || !selection.toString().trim()) {
      // 不隐藏已有的 bubble —— 只在点击空白处时才隐藏
      return
    }

    const selectedText = selection.toString().trim()
    if (!selectedText) return

    const range = selection.getRangeAt(0)
    const rect = range.getBoundingClientRect()
    const containerRect = containerRef.current?.getBoundingClientRect()

    if (!containerRect) return

    setBubble({
      visible: true,
      selectedText,
      position: {
        x: rect.left - containerRect.left + rect.width / 2,
        y: rect.top - containerRect.top - 10,
      },
    })
  }, [readOnly])

  /** 点击容器空白处关闭气泡 */
  const handleContainerClick = useCallback(
    (e: React.MouseEvent) => {
      // 如果点击的不是已高亮的批注标记，关闭气泡
      const target = e.target as HTMLElement
      if (!target.closest('[data-annotation-highlight]') && bubble.visible) {
        setBubble((prev) => ({ ...prev, visible: false }))
      }
    },
    [bubble.visible],
  )

  /** 提交批注 */
  const handleSubmitAnnotation = useCallback(
    (suggestion: string) => {
      if (!bubble.selectedText || !onAddAnnotation) return

      const contentText = containerRef.current?.textContent ?? ''
      const start = contentText.indexOf(bubble.selectedText)
      const end = start >= 0 ? start + bubble.selectedText.length : 0

      onAddAnnotation({
        id: uid(),
        type: 'text_selection',
        selectedText: bubble.selectedText,
        textPosition: { start: Math.max(0, start), end },
        suggestion,
        createdAt: new Date().toISOString(),
      })

      setBubble((prev) => ({ ...prev, visible: false }))
      window.getSelection()?.removeAllRanges()
    },
    [bubble.selectedText, onAddAnnotation],
  )

  /** 取消批注 */
  const handleCancelAnnotation = useCallback(() => {
    setBubble((prev) => ({ ...prev, visible: false }))
    window.getSelection()?.removeAllRanges()
  }, [])

  // 按 Escape 关闭气泡
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && bubble.visible) {
        handleCancelAnnotation()
      }
    }
    document.addEventListener('keydown', handler)
    return () => document.removeEventListener('keydown', handler)
  }, [bubble.visible, handleCancelAnnotation])

  /** 过滤出文本批注 */
  const textAnnotations = annotations.filter(
    (a) => a.type === 'text_selection' && a.selectedText,
  )

  return (
    <div
      ref={containerRef}
      className={`reviewable-document relative ${readOnly ? '' : 'select-text cursor-text'}`}
      onMouseUp={handleMouseUp}
      onClick={handleContainerClick}
    >
      {/* 文档内容 */}
      <div className="prose prose-sm dark:prose-invert max-w-none">
        <MarkdownRenderer content={content} />
      </div>

      {/* 已有批注高亮 + 侧边标记 */}
      {textAnnotations.map((annotation) => (
        <div
          key={annotation.id}
          data-annotation-highlight={annotation.id}
          className="group absolute right-0 flex items-center"
          style={{ top: 0 }}
        >
          {/* 侧边标记 */}
          <div className="flex items-center gap-1 rounded-l-md bg-yellow-200/80 px-1.5 py-0.5 text-xs text-yellow-800 dark:bg-yellow-800/60 dark:text-yellow-200">
            <span>📝</span>
            <span className="max-w-[120px] truncate">{annotation.suggestion}</span>
            {!readOnly && onRemoveAnnotation && (
              <button
                className="ml-1 hover:text-red-600"
                onClick={(e) => {
                  e.stopPropagation()
                  onRemoveAnnotation(annotation.id)
                }}
                title="删除批注"
              >
                ×
              </button>
            )}
          </div>
        </div>
      ))}

      {/* 批注计数指示 */}
      {textAnnotations.length > 0 && (
        <div className="absolute right-0 top-0 -translate-y-full">
          <span className="inline-flex items-center gap-1 rounded-md bg-yellow-100 px-2 py-0.5 text-xs font-medium text-yellow-800 dark:bg-yellow-900/50 dark:text-yellow-200">
            📝 {textAnnotations.length} 条批注
          </span>
        </div>
      )}

      {/* 批注气泡 */}
      {bubble.visible && !readOnly && (
        <AnnotationBubble
          selectedText={bubble.selectedText}
          position={bubble.position}
          onSubmit={handleSubmitAnnotation}
          onCancel={handleCancelAnnotation}
        />
      )}
    </div>
  )
}
