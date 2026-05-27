/**
 * ImageAreaAnnotation - 图片区域标注组件
 *
 * 在图片上画矩形标注区域，添加文字反馈。
 * 支持鼠标拖拽创建标注、已有标注显示为带编号矩形框。
 */

import { X, Plus, Move } from 'lucide-react'
import React, { useState, useCallback, useRef } from 'react'
import type { Annotation } from '@/types/review'

/** 生成简易唯一 ID */
function uid(): string {
  return Date.now().toString(36) + Math.random().toString(36).slice(2)
}

export interface ImageAreaAnnotationProps {
  /** 图片 URL */
  imageUrl: string
  /** 已有图片区域批注 */
  annotations: Annotation[]
  /** 添加批注回调 */
  onAddAnnotation?: (annotation: Annotation) => void
  /** 删除批注回调 */
  onRemoveAnnotation?: (id: string) => void
  /** 是否只读 */
  readOnly?: boolean
}

/** 正在绘制的矩形状态 */
interface DrawingRect {
  startX: number
  startY: number
  endX: number
  endY: number
}

/** 标注颜色 */
const AREA_COLORS = [
  'rgba(239, 68, 68, 0.35)',  // 红
  'rgba(59, 130, 246, 0.35)', // 蓝
  'rgba(16, 185, 129, 0.35)', // 绿
  'rgba(245, 158, 11, 0.35)', // 黄
  'rgba(168, 85, 247, 0.35)', // 紫
]

const AREA_BORDERS = [
  'rgba(239, 68, 68, 0.8)',
  'rgba(59, 130, 246, 0.8)',
  'rgba(16, 185, 129, 0.8)',
  'rgba(245, 158, 11, 0.8)',
  'rgba(168, 85, 247, 0.8)',
]

/**
 * ImageAreaAnnotation
 *
 * 显示图片，支持在其上拖拽绘制矩形标注区域。
 * 每个标注区域有编号和文字反馈。
 */
export function ImageAreaAnnotation({
  imageUrl,
  annotations,
  onAddAnnotation,
  onRemoveAnnotation,
  readOnly = false,
}: ImageAreaAnnotationProps) {
  const containerRef = useRef<HTMLDivElement>(null)
  const [drawing, setDrawing] = useState(false)
  const [drawingRect, setDrawingRect] = useState<DrawingRect | null>(null)
  const [pendingSuggestion, setPendingSuggestion] = useState<{
    area: { x: number; y: number; width: number; height: number }
    position: { x: number; y: number }
  } | null>(null)
  const [suggestionText, setSuggestionText] = useState('')
  const [imageSize, setImageSize] = useState<{ width: number; height: number }>({
    width: 0,
    height: 0,
  })

  /** 图片加载完成，记录尺寸 */
  const handleImageLoad = useCallback(
    (e: React.SyntheticEvent<HTMLImageElement>) => {
      setImageSize({
        width: e.currentTarget.naturalWidth,
        height: e.currentTarget.naturalHeight,
      })
    },
    [],
  )

  /** 鼠标按下开始绘制 */
  const handleMouseDown = useCallback(
    (e: React.MouseEvent) => {
      if (readOnly || pendingSuggestion) return
      const rect = e.currentTarget.getBoundingClientRect()
      const x = e.clientX - rect.left
      const y = e.clientY - rect.top

      setDrawing(true)
      setDrawingRect({ startX: x, startY: y, endX: x, endY: y })
    },
    [readOnly, pendingSuggestion],
  )

  /** 鼠标移动更新矩形 */
  const handleMouseMove = useCallback(
    (e: React.MouseEvent) => {
      if (!drawing || !drawingRect) return
      const rect = e.currentTarget.getBoundingClientRect()
      const x = e.clientX - rect.left
      const y = e.clientY - rect.top

      setDrawingRect((prev) => (prev ? { ...prev, endX: x, endY: y } : null))
    },
    [drawing, drawingRect],
  )

  /** 鼠标松开完成绘制 */
  const handleMouseUp = useCallback(() => {
    if (!drawing || !drawingRect) return

    setDrawing(false)

    const x = Math.min(drawingRect.startX, drawingRect.endX)
    const y = Math.min(drawingRect.startY, drawingRect.endY)
    const width = Math.abs(drawingRect.endX - drawingRect.startX)
    const height = Math.abs(drawingRect.endY - drawingRect.startY)

    // 太小的区域忽略
    if (width < 10 || height < 10) {
      setDrawingRect(null)
      return
    }

    // 转换为百分比坐标（相对于图片显示尺寸）
    const imgEl = containerRef.current?.querySelector('img')
    if (!imgEl) return

    const displayWidth = imgEl.clientWidth
    const displayHeight = imgEl.clientHeight

    setPendingSuggestion({
      area: {
        x: (x / displayWidth) * 100,
        y: (y / displayHeight) * 100,
        width: (width / displayWidth) * 100,
        height: (height / displayHeight) * 100,
      },
      position: { x: x + width / 2, y },
    })
    setDrawingRect(null)
  }, [drawing, drawingRect])

  /** 提交标注 */
  const handleSubmitSuggestion = useCallback(() => {
    if (!pendingSuggestion || !suggestionText.trim() || !onAddAnnotation) return

    onAddAnnotation({
      id: uid(),
      type: 'image_area',
      area: pendingSuggestion.area,
      imageUrl,
      suggestion: suggestionText.trim(),
      createdAt: new Date().toISOString(),
    })

    setPendingSuggestion(null)
    setSuggestionText('')
  }, [pendingSuggestion, suggestionText, onAddAnnotation, imageUrl])

  /** 取消标注 */
  const handleCancelSuggestion = useCallback(() => {
    setPendingSuggestion(null)
    setSuggestionText('')
  }, [])

  /** 渲染矩形区域为 CSS 像素坐标 */
  const renderAnnotationRect = (
    area: { x: number; y: number; width: number; height: number },
    index: number,
    annotation: Annotation,
  ) => {
    const colorIdx = index % AREA_COLORS.length
    return (
      <div
        key={annotation.id}
        className="absolute group"
        style={{
          left: `${area.x}%`,
          top: `${area.y}%`,
          width: `${area.width}%`,
          height: `${area.height}%`,
          backgroundColor: AREA_COLORS[colorIdx],
          border: `2px solid ${AREA_BORDERS[colorIdx]}`,
        }}
      >
        {/* 编号标记 */}
        <div
          className="absolute -top-3 -left-1 flex h-5 w-5 items-center justify-center rounded-full text-[10px] font-bold text-white"
          style={{ backgroundColor: AREA_BORDERS[colorIdx] }}
        >
          {index + 1}
        </div>

        {/* 悬停显示建议 */}
        <div className="absolute bottom-0 left-0 right-0 block md:hidden md:group-hover:block bg-black/70 p-1 text-[10px] text-white">
          {annotation.suggestion}
        </div>

        {/* 删除按钮 */}
        {!readOnly && onRemoveAnnotation && (
          <button
            className="absolute -top-2 -right-2 hidden h-4 w-4 items-center justify-center rounded-full bg-red-500 text-[10px] text-white hover:bg-red-600 group-hover:flex"
            onClick={(e) => {
              e.stopPropagation()
              onRemoveAnnotation(annotation.id)
            }}
            title="删除标注"
          >
            <X className="h-2.5 w-2.5" />
          </button>
        )}
      </div>
    )
  }

  /** 当前绘制中的矩形 */
  const renderDrawingRect = () => {
    if (!drawingRect) return null
    const x = Math.min(drawingRect.startX, drawingRect.endX)
    const y = Math.min(drawingRect.startY, drawingRect.endY)
    const width = Math.abs(drawingRect.endX - drawingRect.startX)
    const height = Math.abs(drawingRect.endY - drawingRect.startY)

    return (
      <div
        className="pointer-events-none absolute border-2 border-blue-500 bg-blue-500/20"
        style={{ left: x, top: y, width, height }}
      />
    )
  }

  return (
    <div className="image-area-annotation flex h-full flex-col">
      {/* 工具栏 */}
      {!readOnly && (
        <div className="flex items-center gap-2 border-b border-border px-3 py-2">
          <Move className="h-3.5 w-3.5 text-muted-foreground" />
          <span className="text-xs text-muted-foreground">
            在图片上拖拽绘制标注区域
          </span>
          <span className="ml-auto text-[10px] text-muted-foreground">
            {annotations.length} 个标注
          </span>
        </div>
      )}

      {/* 图片区域 */}
      <div className="flex-1 overflow-auto">
        <div
          ref={containerRef}
          className={`relative inline-block ${readOnly ? '' : 'cursor-crosshair'}`}
          onMouseDown={handleMouseDown}
          onMouseMove={handleMouseMove}
          onMouseUp={handleMouseUp}
          onMouseLeave={() => {
            if (drawing) handleMouseUp()
          }}
        >
          <img
            src={imageUrl}
            alt="标注图片"
            className="max-h-full max-w-full object-contain"
            onLoad={handleImageLoad}
            draggable={false}
          />

          {/* 已有标注 */}
          {annotations
            .filter((a) => a.type === 'image_area' && a.area)
            .map((a, idx) => renderAnnotationRect(a.area!, idx, a))}

          {/* 正在绘制的矩形 */}
          {drawing && renderDrawingRect()}

          {/* 标注建议输入框 */}
          {pendingSuggestion && (
            <div
              className="absolute z-50"
              style={{
                left: pendingSuggestion.position.x,
                top: pendingSuggestion.position.y,
                transform: 'translate(-50%, -100%)',
              }}
            >
              <div className="w-60 overflow-hidden rounded-lg border border-border bg-background shadow-xl">
                <div className="border-b border-border px-3 py-1.5 text-xs font-medium text-foreground">
                  <Plus className="mr-1 inline h-3 w-3" />
                  添加标注
                </div>
                <div className="px-3 py-2">
                  <textarea
                    value={suggestionText}
                    onChange={(e) => setSuggestionText(e.target.value)}
                    placeholder="输入标注说明..."
                    rows={2}
                    autoFocus
                    className="w-full resize-none rounded border border-border bg-background px-2 py-1 text-xs outline-none focus:ring-1 focus:ring-blue-500"
                    onKeyDown={(e) => {
                      if (e.key === 'Enter' && !e.shiftKey) {
                        e.preventDefault()
                        handleSubmitSuggestion()
                      }
                      if (e.key === 'Escape') handleCancelSuggestion()
                    }}
                  />
                </div>
                <div className="flex justify-end gap-1.5 border-t border-border px-3 py-1.5">
                  <button
                    className="rounded px-2 py-0.5 text-[10px] text-muted-foreground hover:bg-accent"
                    onClick={handleCancelSuggestion}
                  >
                    取消
                  </button>
                  <button
                    className="rounded bg-blue-600 px-2 py-0.5 text-[10px] text-white hover:bg-blue-700 disabled:opacity-50"
                    onClick={handleSubmitSuggestion}
                    disabled={!suggestionText.trim()}
                  >
                    添加
                  </button>
                </div>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
