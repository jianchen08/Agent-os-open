/**
 * ImageAnnotationView - 图片标注视图组件
 *
 * 在图片上显示标注层叠加，支持区域标注的渲染。
 * 用于审批流程中展示图片制品及对应批注。
 */

import React from 'react'
import type { Annotation } from '@/types/review'

export interface ImageAnnotationViewProps {
  /** 图片 URL */
  imageUrl: string
  /** 图片描述文本（alt） */
  altText?: string
  /** 已有图片区域批注列表 */
  annotations: Annotation[]
  /** 是否只读模式 */
  readOnly?: boolean
}

/** 标注颜色 */
const AREA_COLORS = [
  'rgba(239, 68, 68, 0.35)',
  'rgba(59, 130, 246, 0.35)',
  'rgba(16, 185, 129, 0.35)',
  'rgba(245, 158, 11, 0.35)',
  'rgba(168, 85, 247, 0.35)',
]

const AREA_BORDERS = [
  'rgba(239, 68, 68, 0.8)',
  'rgba(59, 130, 246, 0.8)',
  'rgba(16, 185, 129, 0.8)',
  'rgba(245, 158, 11, 0.8)',
  'rgba(168, 85, 247, 0.8)',
]

/**
 * ImageAnnotationView
 *
 * 显示图片并叠加标注层，每个标注以带编号的半透明矩形呈现。
 */
export function ImageAnnotationView({
  imageUrl,
  altText = '标注图片',
  annotations,
  readOnly = false,
}: ImageAnnotationViewProps) {
  /** 过滤出图片区域类型的批注 */
  const imageAnnotations = annotations.filter(
    (a) => a.type === 'image_area' && a.area,
  )

  return (
    <div className="image-annotation-view flex h-full flex-col" data-testid="image-annotation-view">
      {/* 工具栏 */}
      <div className="flex items-center gap-2 border-b border-border px-3 py-2">
        <span className="text-sm font-medium text-foreground">图片标注</span>
        <span className="ml-auto text-xs text-muted-foreground" data-testid="annotation-count">
          {imageAnnotations.length} 个标注
        </span>
      </div>

      {/* 图片区域 */}
      <div className="flex-1 overflow-auto">
        <div className="relative inline-block">
          <img
            src={imageUrl}
            alt={altText}
            className="max-h-full max-w-full object-contain"
            data-testid="annotation-image"
            draggable={false}
          />

          {/* 标注叠加层 */}
          {imageAnnotations.map((annotation, idx) => {
            if (!annotation.area) return null
            const colorIdx = idx % AREA_COLORS.length
            return (
              <div
                key={annotation.id}
                className="absolute group"
                data-testid={`annotation-overlay-${idx}`}
                style={{
                  left: `${annotation.area.x}%`,
                  top: `${annotation.area.y}%`,
                  width: `${annotation.area.width}%`,
                  height: `${annotation.area.height}%`,
                  backgroundColor: AREA_COLORS[colorIdx],
                  border: `2px solid ${AREA_BORDERS[colorIdx]}`,
                }}
              >
                {/* 编号标记 */}
                <div
                  className="absolute -top-3 -left-1 flex h-5 w-5 items-center justify-center rounded-full text-[10px] font-bold text-white"
                  style={{ backgroundColor: AREA_BORDERS[colorIdx] }}
                  data-testid={`annotation-badge-${idx}`}
                >
                  {idx + 1}
                </div>

                {/* 悬停显示建议 */}
                <div className="absolute bottom-0 left-0 right-0 block md:hidden md:group-hover:block bg-black/70 p-1 text-[10px] text-white">
                  {annotation.suggestion}
                </div>
              </div>
            )
          })}

          {/* 无标注占位 */}
          {imageAnnotations.length === 0 && (
            <div
              className="absolute inset-0 flex items-center justify-center text-sm text-muted-foreground"
              data-testid="no-annotations"
            >
              暂无标注
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
