/**
 * ImageGallery 组件
 *
 * 图像画廊组件，用于展示生成的图像结果。
 * 支持网格展示、Lightbox 大图查看、生成参数信息显示、下载和历史浏览。
 *
 * 功能：
 * - 网格展示生成的图像
 * - 点击查看大图（Lightbox 效果）
 * - 显示生成参数信息（prompt, size, seed 等）
 * - 支持下载
 * - 支持历史记录浏览（前后导航）
 * - 响应式布局（桌面多列、移动端单列）
 *
 * @example
 * ```tsx
 * const images = [
 *   {
 *     id: '1',
 *     url: 'https://example.com/image.png',
 *     thumbnailUrl: 'https://example.com/thumb.png',
 *     title: '日落风景',
 *     prompt: '一个美丽的日落',
 *     size: '1024x1024',
 *     seed: 42,
 *     createdAt: '2026-01-01T00:00:00Z',
 *   },
 * ]
 * <ImageGallery images={images} />
 * ```
 */

import {
  ChevronLeft,
  ChevronRight,
  Download,
  Image as ImageIcon,
  Info,
  X,
} from 'lucide-react'
import { memo, useCallback, useEffect, useRef, useState } from 'react'
import { useNonPassiveWheel } from '@/hooks/useNonPassiveWheel'

// 缩放范围
const MIN_SCALE = 0.5
const MAX_SCALE = 5
const SCALE_STEP = 0.2

/** 图像数据项 */
export interface ImageItem {
  /** 唯一标识 */
  id: string
  /** 图像 URL */
  url: string
  /** 缩略图 URL（可选，默认使用 url） */
  thumbnailUrl?: string
  /** 图像标题 */
  title: string
  /** 生成提示词 */
  prompt?: string
  /** 图像尺寸（如 '1024x1024'） */
  size?: string
  /** 生成种子 */
  seed?: number
  /** 创建时间（ISO 字符串） */
  createdAt?: string
  /** 扩展元数据 */
  metadata?: Record<string, string>
}

/** ImageGallery 组件属性 */
export interface ImageGalleryProps {
  /** 图像数据列表 */
  images: ImageItem[]
  /** 自定义类名 */
  className?: string
  /** 每行显示的列数（桌面端），默认 3 */
  columns?: number
}

/**
 * 格式化日期为可读字符串。
 *
 * @param isoString - ISO 日期字符串
 * @returns 格式化后的日期字符串
 */
function formatDate(isoString: string): string {
  try {
    const date = new Date(isoString)
    return date.toLocaleDateString('zh-CN', {
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
    })
  } catch {
    return isoString
  }
}

/**
 * 图像画廊组件
 *
 * 以网格布局展示图像，支持 Lightbox 大图查看、
 * 生成参数展示、下载和历史记录浏览。
 */
export const ImageGallery = memo<ImageGalleryProps>(
  ({ images, className = '', columns = 3 }) => {
    const [lightboxIndex, setLightboxIndex] = useState<number | null>(null)
    const [showInfo, setShowInfo] = useState(false)
    // 缩放和平移状态
    const [scale, setScale] = useState(1)
    const [translate, setTranslate] = useState({ x: 0, y: 0 })
    const isDragging = useRef(false)
    const dragStart = useRef({ x: 0, y: 0 })

    /** 重置变换状态 */
    const resetTransform = useCallback(() => {
      setScale(1)
      setTranslate({ x: 0, y: 0 })
    }, [])

    /** 打开 Lightbox */
    const openLightbox = useCallback((index: number) => {
      setLightboxIndex(index)
      setShowInfo(false)
      resetTransform()
    }, [resetTransform])

    /** 关闭 Lightbox */
    const closeLightbox = useCallback(() => {
      setLightboxIndex(null)
      setShowInfo(false)
      resetTransform()
    }, [resetTransform])

    /** 上一张 */
    const goToPrev = useCallback(() => {
      if (lightboxIndex === null) return
      setLightboxIndex(
        lightboxIndex > 0 ? lightboxIndex - 1 : images.length - 1
      )
      resetTransform()
    }, [lightboxIndex, images.length, resetTransform])

    /** 下一张 */
    const goToNext = useCallback(() => {
      if (lightboxIndex === null) return
      setLightboxIndex(
        lightboxIndex < images.length - 1 ? lightboxIndex + 1 : 0
      )
      resetTransform()
    }, [lightboxIndex, images.length, resetTransform])

    /** 滚轮缩放 */
    const handleWheel = useCallback((e: WheelEvent) => {
      e.preventDefault()
      setScale((prev) => {
        const delta = e.deltaY < 0 ? SCALE_STEP : -SCALE_STEP
        const next = Math.min(MAX_SCALE, Math.max(MIN_SCALE, prev + delta))
        // 缩到 1 时重置平移
        if (next === 1) setTranslate({ x: 0, y: 0 })
        return next
      })
    }, [])

    // 以非被动方式绑定 wheel，使 preventDefault() 生效（React 默认的 onWheel 是被动的）
    const wheelRef = useNonPassiveWheel<HTMLDivElement>(handleWheel)

    /** 拖动开始 */
    const handleMouseDown = useCallback(
      (e: React.MouseEvent) => {
        if (scale <= 1) return
        isDragging.current = true
        dragStart.current = {
          x: e.clientX - translate.x,
          y: e.clientY - translate.y,
        }
      },
      [scale, translate]
    )

    /** 拖动中 */
    const handleMouseMove = useCallback((e: React.MouseEvent) => {
      if (!isDragging.current) return
      setTranslate({
        x: e.clientX - dragStart.current.x,
        y: e.clientY - dragStart.current.y,
      })
    }, [])

    /** 拖动结束 */
    const handleMouseUp = useCallback(() => {
      isDragging.current = false
    }, [])

    /** 双击切换缩放 */
    const handleDoubleClick = useCallback(() => {
      if (scale > 1) {
        resetTransform()
      } else {
        setScale(2)
      }
    }, [scale, resetTransform])

    /** 键盘导航 */
    useEffect(() => {
      if (lightboxIndex === null) return

      const handleKeyDown = (e: KeyboardEvent) => {
        switch (e.key) {
          case 'Escape':
            closeLightbox()
            break
          case 'ArrowLeft':
            goToPrev()
            break
          case 'ArrowRight':
            goToNext()
            break
        }
      }

      document.addEventListener('keydown', handleKeyDown)
      return () => document.removeEventListener('keydown', handleKeyDown)
    }, [lightboxIndex, closeLightbox, goToPrev, goToNext])

    /** 下载指定图像 */
    const handleDownload = useCallback(
      (image: ImageItem) => {
        const link = document.createElement('a')
        link.href = image.url
        link.download = image.title || `image-${image.id}`
        document.body.appendChild(link)
        link.click()
        document.body.removeChild(link)
      },
      []
    )

    // 空状态
    if (images.length === 0) {
      return (
        <div
          className={`flex flex-col items-center justify-center rounded-xl border border-dashed border-[var(--border)] bg-[var(--muted)]/20 p-12 ${className}`}
        >
          <ImageIcon className="mb-3 h-12 w-12 text-[var(--muted-foreground)]" />
          <p className="text-sm text-[var(--muted-foreground)]">暂无图像</p>
          <p className="mt-1 text-xs text-[var(--muted-foreground)]/60">
            生成的图像将在此处展示
          </p>
        </div>
      )
    }

    // 动态网格列样式
    const gridColsClass =
      columns === 2
        ? 'grid-cols-1 sm:grid-cols-2'
        : columns === 4
          ? 'grid-cols-1 sm:grid-cols-2 lg:grid-cols-4'
          : 'grid-cols-1 sm:grid-cols-2 lg:grid-cols-3'

    const currentImage =
      lightboxIndex !== null ? images[lightboxIndex] : null

    return (
      <div className={className}>
        {/* 网格布局 */}
        <div
          data-testid="gallery-grid"
          className={`grid ${gridColsClass} gap-4`}
        >
          {images.map((image, index) => (
            <div
              key={image.id}
              data-testid="gallery-card"
              className="group cursor-pointer overflow-hidden rounded-xl border border-[var(--border)] bg-[var(--card-bg,var(--card))] shadow-[var(--shadow-card,0_2px_8px_rgba(0,0,0,0.06))] transition-all hover:shadow-[var(--card-hover-shadow,0_4px_16px_rgba(0,0,0,0.1))]"
              onClick={() => openLightbox(index)}
            >
              {/* 图像 */}
              <div className="relative aspect-square overflow-hidden bg-[var(--muted)]">
                <img
                  src={image.thumbnailUrl || image.url}
                  alt={image.title}
                  className="h-full w-full object-cover transition-transform duration-300 group-hover:scale-105"
                  loading="lazy"
                />

                {/* 悬停遮罩 */}
                <div className="absolute inset-0 flex items-end bg-gradient-to-t from-black/60 via-transparent to-transparent opacity-100 md:opacity-0 md:group-hover:opacity-100 transition-opacity">
                  <div className="flex w-full items-center justify-between p-3">
                    <span className="truncate text-sm text-white">
                      {image.title}
                    </span>
                    <button
                      onClick={(e) => {
                        e.stopPropagation()
                        handleDownload(image)
                      }}
                      className="flex h-8 w-8 flex-shrink-0 items-center justify-center rounded-lg bg-white/20 text-white backdrop-blur-sm transition-colors hover:bg-white/30"
                      aria-label="下载"
                    >
                      <Download className="h-4 w-4" />
                    </button>
                  </div>
                </div>
              </div>

              {/* 信息区 */}
              <div className="p-3">
                <h4 className="truncate text-sm font-medium text-foreground">
                  {image.title}
                </h4>
                <div className="mt-1 flex items-center gap-2 text-xs text-muted-foreground">
                  {image.size && <span>{image.size}</span>}
                  {image.seed !== undefined && (
                    <span>种子: {image.seed}</span>
                  )}
                </div>
                {image.createdAt && (
                  <time
                    data-testid="image-time"
                    className="mt-1 block text-xs text-[var(--muted-foreground)]/60"
                    dateTime={image.createdAt}
                  >
                    {formatDate(image.createdAt)}
                  </time>
                )}
              </div>
            </div>
          ))}
        </div>

        {/* Lightbox 大图查看 */}
        {currentImage && (
          <div
            data-testid="lightbox"
            className="fixed inset-0 z-50 flex items-center justify-center bg-black/80 backdrop-blur-sm"
            onClick={(e) => {
              if (e.target === e.currentTarget) closeLightbox()
            }}
          >
            {/* 关闭按钮 */}
            <button
              onClick={closeLightbox}
              className="absolute right-4 top-4 z-10 flex h-10 w-10 items-center justify-center rounded-full bg-white/10 text-white backdrop-blur-sm transition-colors hover:bg-white/20"
              aria-label="关闭"
            >
              <X className="h-5 w-5" />
            </button>

            {/* 上一张 */}
            {images.length > 1 && (
              <button
                onClick={(e) => {
                  e.stopPropagation()
                  goToPrev()
                }}
                className="absolute left-4 z-10 flex h-10 w-10 items-center justify-center rounded-full bg-white/10 text-white backdrop-blur-sm transition-colors hover:bg-white/20"
                aria-label="上一张"
              >
                <ChevronLeft className="h-6 w-6" />
              </button>
            )}

            {/* 下一张 */}
            {images.length > 1 && (
              <button
                onClick={(e) => {
                  e.stopPropagation()
                  goToNext()
                }}
                className="absolute right-4 z-10 flex h-10 w-10 items-center justify-center rounded-full bg-white/10 text-white backdrop-blur-sm transition-colors hover:bg-white/20"
                aria-label="下一张"
              >
                <ChevronRight className="h-6 w-6" />
              </button>
            )}

            {/* 图像展示 */}
            <div
              ref={wheelRef}
              className="relative flex max-h-[85vh] max-w-[85vw] flex-col items-center"
              onClick={(e) => e.stopPropagation()}
              onMouseDown={handleMouseDown}
              onMouseMove={handleMouseMove}
              onMouseUp={handleMouseUp}
              onMouseLeave={handleMouseUp}
              onDoubleClick={handleDoubleClick}
              style={{ cursor: scale > 1 ? (isDragging.current ? 'grabbing' : 'grab') : 'zoom-in' }}
            >
              <img
                src={currentImage.url || currentImage.thumbnailUrl}
                alt={currentImage.title}
                className="max-h-[70vh] rounded-lg object-contain select-none"
                style={{
                  transform: `translate(${translate.x}px, ${translate.y}px) scale(${scale})`,
                  transition: isDragging.current ? 'none' : 'transform 0.2s ease-out',
                }}
                onError={(e) => {
                  const img = e.currentTarget
                  if (img.src !== currentImage.thumbnailUrl && currentImage.thumbnailUrl) {
                    img.src = currentImage.thumbnailUrl
                  }
                }}
                draggable={false}
              />

              {/* 底部信息栏 */}
              <div className="mt-4 flex w-full items-center justify-between rounded-lg bg-white/10 px-4 py-3 backdrop-blur-sm">
                <div className="min-w-0 flex-1">
                  <h3 className="truncate text-sm font-medium text-white">
                    {currentImage.title}
                  </h3>
                  <div className="mt-1 flex flex-wrap items-center gap-3 text-xs text-white/70">
                    {currentImage.size && <span>{currentImage.size}</span>}
                    {currentImage.seed !== undefined && (
                      <span>种子: {currentImage.seed}</span>
                    )}
                    {currentImage.createdAt && (
                      <span>{formatDate(currentImage.createdAt)}</span>
                    )}
                    <span>
                      {lightboxIndex + 1} / {images.length}
                    </span>
                    {scale !== 1 && (
                      <span>{Math.round(scale * 100)}%</span>
                    )}
                  </div>
                </div>

                <div className="flex items-center gap-2">
                  {/* 参数信息按钮 */}
                  {currentImage.prompt && (
                    <button
                      onClick={() => setShowInfo(!showInfo)}
                      className="flex h-8 w-8 items-center justify-center rounded-lg bg-white/10 text-white transition-colors hover:bg-white/20"
                      aria-label="查看参数"
                    >
                      <Info className="h-4 w-4" />
                    </button>
                  )}

                  {/* 下载按钮 */}
                  <button
                    onClick={() => handleDownload(currentImage)}
                    className="flex h-8 w-8 items-center justify-center rounded-lg bg-white/10 text-white transition-colors hover:bg-white/20"
                    aria-label="下载"
                  >
                    <Download className="h-4 w-4" />
                  </button>
                </div>
              </div>

              {/* 生成参数面板 */}
              {showInfo && currentImage.prompt && (
                <div className="mt-2 w-full rounded-lg bg-white/10 p-4 backdrop-blur-sm">
                  <h4 className="mb-2 text-xs font-medium text-white/90">
                    生成参数
                  </h4>
                  <div className="space-y-1.5 text-xs text-white/70">
                    <div>
                      <span className="text-white/50">Prompt: </span>
                      {currentImage.prompt}
                    </div>
                    {currentImage.size && (
                      <div>
                        <span className="text-white/50">尺寸: </span>
                        {currentImage.size}
                      </div>
                    )}
                    {currentImage.seed !== undefined && (
                      <div>
                        <span className="text-white/50">种子: </span>
                        {currentImage.seed}
                      </div>
                    )}
                    {currentImage.metadata &&
                      Object.entries(currentImage.metadata).map(
                        ([key, value]) => (
                          <div key={key}>
                            <span className="text-white/50">{key}: </span>
                            {value}
                          </div>
                        )
                      )}
                  </div>
                </div>
              )}
            </div>
          </div>
        )}
      </div>
    )
  }
)

ImageGallery.displayName = 'ImageGallery'

export default ImageGallery
