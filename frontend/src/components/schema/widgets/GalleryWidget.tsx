/**
 * 图片画廊组件
 *
 * 根据 Schema 渲染图片画廊，支持网格布局、点击放大预览和懒加载占位。
 *
 * @module GalleryWidget
 */

import React, { useState, useCallback } from 'react'

/** 画廊项定义 */
interface GalleryItem {
  /** 图片地址 */
  src: string
  /** 替代文本 */
  alt: string
  /** 标题 */
  title?: string
  /** 描述 */
  description?: string
}

/**
 * 提取画廊项数组
 *
 * @param items - 原始数据
 * @returns 类型安全的 GalleryItem 数组
 */
function extractItems(items: unknown): GalleryItem[] {
  if (!Array.isArray(items)) return []
  return items.filter(
    (item): item is GalleryItem =>
      typeof item === 'object' && item !== null && typeof (item as GalleryItem).src === 'string',
  )
}

/**
 * 图片画廊组件
 *
 * 支持网格布局、点击放大预览和空数据状态提示。
 *
 * @param props - 组件属性，包含 items、columns 等
 * @returns 画廊渲染结果
 */
export function GalleryWidget(props: Record<string, unknown>) {
  const items = extractItems(props.items)
  const columns = (props.columns as number) ?? 3
  const [previewIndex, setPreviewIndex] = useState<number | null>(null)

  const handlePreview = useCallback((index: number) => {
    setPreviewIndex(index)
  }, [])

  const handleClosePreview = useCallback(() => {
    setPreviewIndex(null)
  }, [])

  const handlePrev = useCallback(() => {
    if (previewIndex !== null && previewIndex > 0) {
      setPreviewIndex(previewIndex - 1)
    }
  }, [previewIndex])

  const handleNext = useCallback(() => {
    if (previewIndex !== null && previewIndex < items.length - 1) {
      setPreviewIndex(previewIndex + 1)
    }
  }, [previewIndex, items.length])

  if (items.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center rounded-lg border border-dashed p-8">
        <svg
          className="text-muted-foreground mb-2 h-12 w-12"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth={1.5}
        >
          <rect x="3" y="3" width="18" height="18" rx="2" />
          <circle cx="8.5" cy="8.5" r="1.5" />
          <path d="M21 15l-5-5L5 21" />
        </svg>
        <p className="text-muted-foreground text-sm">暂无图片</p>
      </div>
    )
  }

  const gridColsClass =
    columns <= 2
      ? 'grid-cols-2'
      : columns <= 3
        ? 'grid-cols-3'
        : columns <= 4
          ? 'grid-cols-4'
          : 'grid-cols-4 sm:grid-cols-2 md:grid-cols-3 lg:grid-cols-4'

  return (
    <div className="w-full">
      <div className={`grid ${gridColsClass} gap-3`}>
        {items.map((item, index) => (
          <div
            key={index}
            className="group cursor-pointer overflow-hidden rounded-lg border transition-shadow hover:shadow-md"
            onClick={() => handlePreview(index)}
          >
            {/* 图片容器 - 懒加载占位 */}
            <div className="relative aspect-square bg-muted">
              <img
                src={item.src}
                alt={item.alt}
                loading="lazy"
                className="h-full w-full object-cover transition-transform group-hover:scale-105"
                onError={(e) => {
                  const target = e.target as HTMLImageElement
                  target.style.display = 'none'
                  if (target.nextElementSibling) {
                    ;(target.nextElementSibling as HTMLElement).style.display = 'flex'
                  }
                }}
              />
              {/* 加载失败占位 */}
              <div
                className="absolute inset-0 hidden items-center justify-center"
                style={{ display: undefined }}
              >
                <div className="text-center">
                  <svg
                    className="text-muted-foreground mx-auto h-8 w-8"
                    viewBox="0 0 24 24"
                    fill="none"
                    stroke="currentColor"
                    strokeWidth={1.5}
                  >
                    <rect x="3" y="3" width="18" height="18" rx="2" />
                    <path d="M9.5 9.5l5 5M14.5 9.5l-5 5" />
                  </svg>
                  <span className="text-muted-foreground mt-1 text-xs">加载失败</span>
                </div>
              </div>
            </div>

            {/* 信息区 */}
            {(item.title || item.description) && (
              <div className="bg-background p-2">
                {item.title && (
                  <p className="text-foreground truncate text-sm font-medium">
                    {item.title}
                  </p>
                )}
                {item.description && (
                  <p className="text-muted-foreground truncate text-xs">
                    {item.description}
                  </p>
                )}
              </div>
            )}
          </div>
        ))}
      </div>

      {/* 放大预览模态框 */}
      {previewIndex !== null && (
        <div
          className="bg-background/80 fixed inset-0 z-50 flex items-center justify-center backdrop-blur-sm"
          onClick={handleClosePreview}
        >
          <div
            className="relative max-h-[90vh] max-w-[90vw]"
            onClick={(e) => e.stopPropagation()}
          >
            {/* 关闭按钮 */}
            <button
              onClick={handleClosePreview}
              className="absolute -right-2 -top-2 z-10 flex h-8 w-8 items-center justify-center rounded-full bg-black/60 text-white transition-colors hover:bg-black/80"
            >
              ✕
            </button>

            {/* 上一张 */}
            {previewIndex > 0 && (
              <button
                onClick={handlePrev}
                className="absolute -left-10 top-1/2 z-10 flex h-8 w-8 -translate-y-1/2 items-center justify-center rounded-full bg-black/60 text-white transition-colors hover:bg-black/80"
              >
                ‹
              </button>
            )}

            {/* 下一张 */}
            {previewIndex < items.length - 1 && (
              <button
                onClick={handleNext}
                className="absolute -right-10 top-1/2 z-10 flex h-8 w-8 -translate-y-1/2 items-center justify-center rounded-full bg-black/60 text-white transition-colors hover:bg-black/80"
              >
                ›
              </button>
            )}

            <img
              src={items[previewIndex].src}
              alt={items[previewIndex].alt}
              className="max-h-[85vh] max-w-[85vw] rounded-lg object-contain shadow-xl"
            />

            {/* 图片信息 */}
            {(items[previewIndex].title || items[previewIndex].description) && (
              <div className="absolute bottom-0 left-0 right-0 rounded-b-lg bg-gradient-to-t from-black/60 to-transparent p-4">
                {items[previewIndex].title && (
                  <p className="text-white text-sm font-medium">
                    {items[previewIndex].title}
                  </p>
                )}
                {items[previewIndex].description && (
                  <p className="text-white/80 text-xs">
                    {items[previewIndex].description}
                  </p>
                )}
              </div>
            )}

            {/* 计数 */}
            <div className="absolute bottom-4 right-4 rounded-full bg-black/50 px-2 py-1 text-xs text-white">
              {previewIndex + 1} / {items.length}
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
