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

import { memo, useCallback, useEffect, useRef, useState, type CSSProperties, type FC } from 'react'
import {
  ChevronLeft,
  ChevronRight,
  Download,
  Image as ImageIcon,
  Info,
  X,
} from '@/assets/icons'
import { useNonPassiveWheel } from '@/hooks/useNonPassiveWheel'
import { downloadAttachment, useAuthedAttachmentUrl } from '@/services/fileLoaderRegistry'
import { formatDate } from '@/utils/format'

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

/** 网格缩略图：/uploads 直链经鉴权取流（useAuthedAttachmentUrl → blob），
 * 其余 src 透传；取流失败降级为图标占位（画廊无文字失败卡的既有形态）。 */
const GalleryThumb: FC<{ src?: string; alt: string }> = ({ src, alt }) => {
  const { url, failed } = useAuthedAttachmentUrl(src)
  if (!url) {
    return failed ? (
      <div className="flex h-full w-full items-center justify-center" aria-label="图片加载失败">
        <ImageIcon className="h-10 w-10 text-muted-foreground/60" />
      </div>
    ) : (
      <div className="h-full w-full" />
    )
  }
  return (
    <img
      src={url}
      alt={alt}
      className="h-full w-full object-cover transition-transform duration-300 group-hover:scale-105"
      loading="lazy"
    />
  )
}

/** Lightbox 大图：优先全图 url，取流后 DOM 渲染失败回退缩略图（鉴权解析后的
 * 回退链路，与直连时代 onError 回退同契约）。 */
const LightboxImage: FC<{ image: ImageItem; style?: CSSProperties }> = ({ image, style }) => {
  const { url: fullUrl } = useAuthedAttachmentUrl(image.url)
  const { url: thumbUrl } = useAuthedAttachmentUrl(image.thumbnailUrl)
  const src = fullUrl || thumbUrl
  if (!src) {
    return <div className="max-h-[70vh] min-h-[30vh] w-[60vw] rounded-lg" />
  }
  return (
    <img
      src={src}
      alt={image.title}
      className="max-h-[70vh] rounded-lg object-contain select-none"
      style={style}
      onError={(e) => {
        const img = e.currentTarget
        if (thumbUrl && img.src !== thumbUrl) {
          img.src = thumbUrl
        }
      }}
      draggable={false}
    />
  )
}

/**
 * ImageGallery 组件
 *
 * 以网格布局展示图像，支持 Lightbox 大图查看、
 * 生成参数信息展示、下载和历史浏览。
 */
export const ImageGallery = memo<ImageGalleryProps>(
  ({ images, className = '', columns = 3 }) => {
    const [lightboxIndex, setLightboxIndex] = useState<number | null>(null)
    const [showInfo, setShowInfo] = useState(false)
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

    /** 下载指定图像（/uploads 直链经鉴权取流转 blob 下载，ADR 2026-10-10） */
    const handleDownload = useCallback(
      (image: ImageItem) => {
        void downloadAttachment(image.url, image.title || `image-${image.id}`)
      },
      []
    )

    if (images.length === 0) {
      return (
        <div
          className={`flex flex-col items-center justify-center rounded-xl border border-dashed border-[var(--border)] bg-muted/20 p-12 ${className}`}
        >
          <ImageIcon className="mb-3 h-12 w-12 text-[var(--muted-foreground)]" />
          <p className="text-sm text-[var(--muted-foreground)]">暂无图像</p>
          <p className="mt-1 text-xs text-muted-foreground/60">生成的图像将在此处展示</p>
        </div>
      )
    }

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
              {/* 图像（/uploads 直链经 GalleryThumb 鉴权取流） */}
              <div className="relative aspect-square overflow-hidden bg-[var(--muted)]">
                <GalleryThumb src={image.thumbnailUrl || image.url} alt={image.title} />

                {/* 悬停遮罩 */}
                <div className="absolute inset-0 flex items-end bg-gradient-to-t from-[var(--on-image-caption)] via-transparent to-transparent opacity-100 md:opacity-0 md:group-hover:opacity-100 transition-opacity">
                  <div className="flex w-full items-center justify-between p-3">
                    <span className="truncate text-sm text-[var(--on-image-text)]">
                      {image.title}
                    </span>
                    <button
                      onClick={(e) => {
                        e.stopPropagation()
                        handleDownload(image)
                      }}
                      className="flex h-8 w-8 flex-shrink-0 items-center justify-center rounded-lg bg-[var(--on-image-chrome)] text-[var(--on-image-text)] backdrop-blur-sm transition-colors hover:bg-[var(--on-image-chrome-strong)]"
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
                    className="mt-1 block text-xs text-muted-foreground/60"
                    dateTime={image.createdAt}
                  >
                    {formatDate(image.createdAt, 'datetime')}
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
            className="fixed inset-0 z-50 flex items-center justify-center bg-[var(--overlay-strong)] backdrop-blur-sm"
            onClick={(e) => {
              if (e.target === e.currentTarget) closeLightbox()
            }}
          >
            {/* 关闭按钮 */}
            <button
              onClick={closeLightbox}
              className="absolute right-4 top-4 z-10 flex h-10 w-10 items-center justify-center rounded-full bg-[var(--on-image-chrome)] text-[var(--on-image-text)] backdrop-blur-sm transition-colors hover:bg-[var(--on-image-chrome-strong)]"
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
                className="absolute left-4 z-10 flex h-10 w-10 items-center justify-center rounded-full bg-[var(--on-image-chrome)] text-[var(--on-image-text)] backdrop-blur-sm transition-colors hover:bg-[var(--on-image-chrome-strong)]"
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
                className="absolute right-4 z-10 flex h-10 w-10 items-center justify-center rounded-full bg-[var(--on-image-chrome)] text-[var(--on-image-text)] backdrop-blur-sm transition-colors hover:bg-[var(--on-image-chrome-strong)]"
                aria-label="下一张"
              >
                <ChevronRight className="h-6 w-6" />
              </button>
            )}

            {/* 图像展示（/uploads 直链经 LightboxImage 鉴权取流） */}
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
              <LightboxImage
                image={currentImage}
                style={{
                  transform: `translate(${translate.x}px, ${translate.y}px) scale(${scale})`,
                  transition: isDragging.current ? 'none' : 'transform 0.2s ease-out',
                }}
              />

              {/* 底部信息栏 */}
              <div className="mt-4 flex w-full items-center justify-between rounded-lg bg-[var(--on-image-chrome)] px-4 py-3 backdrop-blur-sm">
                <div className="min-w-0 flex-1">
                  <h3 className="truncate text-sm font-medium text-[var(--on-image-text)]">
                    {currentImage.title}
                  </h3>
                  <div className="mt-1 flex flex-wrap items-center gap-3 text-xs text-[var(--on-image-text-soft)]">
                    {currentImage.size && <span>{currentImage.size}</span>}
                    {currentImage.seed !== undefined && (
                      <span>种子: {currentImage.seed}</span>
                    )}
                    {currentImage.createdAt && (
                      <span>{formatDate(currentImage.createdAt, 'datetime')}</span>
                    )}
                    <span>
                      {(lightboxIndex ?? 0) + 1} / {images.length}
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
                      className="flex h-8 w-8 items-center justify-center rounded-lg bg-[var(--on-image-chrome)] text-[var(--on-image-text)] transition-colors hover:bg-[var(--on-image-chrome-strong)]"
                      aria-label="查看参数"
                    >
                      <Info className="h-4 w-4" />
                    </button>
                  )}

                  {/* 下载按钮 */}
                  <button
                    onClick={() => handleDownload(currentImage)}
                    className="flex h-8 w-8 items-center justify-center rounded-lg bg-[var(--on-image-chrome)] text-[var(--on-image-text)] transition-colors hover:bg-[var(--on-image-chrome-strong)]"
                    aria-label="下载"
                  >
                    <Download className="h-4 w-4" />
                  </button>
                </div>
              </div>

              {/* 生成参数面板 */}
              {showInfo && currentImage.prompt && (
                <div className="mt-2 w-full rounded-lg bg-[var(--on-image-chrome)] p-4 backdrop-blur-sm">
                  <h4 className="mb-2 text-xs font-medium text-[var(--on-image-text)]">
                    生成参数
                  </h4>
                  <div className="space-y-1.5 text-xs text-[var(--on-image-text-soft)]">
                    <div>
                      <span className="text-[var(--on-image-text-faint)]">Prompt: </span>
                      {currentImage.prompt}
                    </div>
                    {currentImage.size && (
                      <div>
                        <span className="text-[var(--on-image-text-faint)]">尺寸: </span>
                        {currentImage.size}
                      </div>
                    )}
                    {currentImage.seed !== undefined && (
                      <div>
                        <span className="text-[var(--on-image-text-faint)]">种子: </span>
                        {currentImage.seed}
                      </div>
                    )}
                    {currentImage.metadata &&
                      Object.entries(currentImage.metadata).map(
                        ([key, value]) => (
                          <div key={key}>
                            <span className="text-[var(--on-image-text-faint)]">{key}: </span>
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
