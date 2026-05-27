/**
 * ArtifactPreview - 制品预览组件
 *
 * 用于嵌入 InteractionCard 的 conversation 模式。
 * 根据 artifact type 自动选择渲染方式，折叠显示，
 * 提供"在工作区打开完整文档"跳转按钮。
 *
 * 增强功能：
 * - 图片预览支持点击展开大图、显示尺寸信息、EXIF 简要摘要
 * - 视频预览支持显示时长标签、分辨率、播放控制改进
 * - 支持 mediaMetadata 透传
 */

import {
  FileText,
  Image,
  Video,
  Music,
  Monitor,
  File,
  ExternalLink,
  ChevronDown,
  ChevronUp,
  X,
  ZoomIn,
  Info,
  Maximize2,
} from 'lucide-react'
import React, { useState, useCallback, useMemo } from 'react'
import type { Artifact, ArtifactType, MediaMetadata } from '@/types/review'

export interface ArtifactPreviewProps {
  /** 制品列表 */
  artifacts: Artifact[]
  /** 跳转到工作区审阅回调 */
  onNavigateToWorkspace?: () => void
  /** 媒体元数据映射（按 artifact id 索引） */
  mediaMetadataMap?: Record<string, MediaMetadata>
}

/** 制品类型图标映射 */
const artifactIcons: Record<ArtifactType, React.ReactNode> = {
  text: <FileText className="h-4 w-4" />,
  image: <Image className="h-4 w-4" />,
  video: <Video className="h-4 w-4" />,
  audio: <Music className="h-4 w-4" />,
  screenshot: <Monitor className="h-4 w-4" />,
  file: <File className="h-4 w-4" />,
}

/** 文本预览最大行数 */
const MAX_PREVIEW_LINES = 5
/** 文本预览最大字符数 */
const MAX_PREVIEW_CHARS = 300

/** 格式化秒数为 mm:ss */
function formatDuration(seconds: number): string {
  const m = Math.floor(seconds / 60)
  const s = Math.floor(seconds % 60)
  return `${m}:${s.toString().padStart(2, '0')}`
}

/**
 * ArtifactPreview
 *
 * 展示制品的折叠预览：
 * - text → 折叠文本
 * - image → 缩略图（增强：尺寸信息、EXIF 摘要、点击展开大图）
 * - video → 视频缩略图（增强：时长标签、分辨率、播放控制）
 * - audio → 播放器
 * - screenshot → 截图缩略图
 * - file → 文件图标 + 名称
 */
export function ArtifactPreview({
  artifacts,
  onNavigateToWorkspace,
  mediaMetadataMap,
}: ArtifactPreviewProps) {
  const [expanded, setExpanded] = useState(false)

  if (!artifacts || artifacts.length === 0) return null

  return (
    <div className="artifact-preview space-y-2 rounded-lg border border-border bg-muted/20 p-3">
      {/* 标题 */}
      <div className="flex items-center justify-between">
        <span className="flex items-center gap-1.5 text-xs font-medium text-muted-foreground">
          📦 制品预览
          <span className="rounded-full bg-muted px-1.5 py-0.5 text-[10px]">
            {artifacts.length}
          </span>
        </span>
        <div className="flex items-center gap-1">
          {artifacts.length > 1 && (
            <button
              className="flex items-center gap-0.5 rounded px-1.5 py-0.5 text-xs text-muted-foreground hover:bg-accent hover:text-foreground transition-colors"
              onClick={() => setExpanded(!expanded)}
            >
              {expanded ? (
                <>
                  <ChevronUp className="h-3 w-3" />
                  收起
                </>
              ) : (
                <>
                  <ChevronDown className="h-3 w-3" />
                  展开
                </>
              )}
            </button>
          )}
          {onNavigateToWorkspace && (
            <button
              className="flex items-center gap-1 rounded px-2 py-0.5 text-xs text-status-info hover:bg-status-info/10 transition-colors"
              onClick={onNavigateToWorkspace}
            >
              <ExternalLink className="h-3 w-3" />
              在工作区打开
            </button>
          )}
        </div>
      </div>

      {/* 制品列表 */}
      <div className="space-y-2">
        {(expanded ? artifacts : artifacts.slice(0, 2)).map((artifact) => (
          <ArtifactItem
            key={artifact.id}
            artifact={artifact}
            mediaMetadata={artifact.mediaMetadata ?? mediaMetadataMap?.[artifact.id]}
          />
        ))}
        {!expanded && artifacts.length > 2 && (
          <div className="text-center text-[10px] text-muted-foreground">
            还有 {artifacts.length - 2} 个制品...
          </div>
        )}
      </div>
    </div>
  )
}

/** 单个制品渲染 */
function ArtifactItem({
  artifact,
  mediaMetadata,
}: {
  artifact: Artifact
  mediaMetadata?: MediaMetadata
}) {
  const { type, content, title, metadata } = artifact

  const label = title || typeLabel(type)
  const versionTag = metadata?.version ? `v${metadata.version}` : null

  return (
    <div className="flex items-start gap-2 rounded-md border border-border/50 bg-background p-2">
      {/* 图标 */}
      <div className="mt-0.5 shrink-0 text-muted-foreground">
        {artifactIcons[type]}
      </div>

      {/* 内容 */}
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-1.5">
          <span className="truncate text-xs font-medium text-foreground">{label}</span>
          {versionTag && (
            <span className="shrink-0 rounded bg-muted px-1 py-0.5 text-[10px] text-muted-foreground">
              {versionTag}
            </span>
          )}
          {metadata?.source && (
            <span className="shrink-0 text-[10px] text-muted-foreground">
              来自 {metadata.source}
            </span>
          )}
        </div>

        {/* 类型特定预览 */}
        <div className="mt-1">
          {type === 'text' && <TextPreview content={content} />}
          {(type === 'image' || type === 'screenshot') && (
            <ImagePreview url={content} mediaMetadata={mediaMetadata} />
          )}
          {type === 'video' && (
            <VideoPreview url={content} mediaMetadata={mediaMetadata} />
          )}
          {type === 'audio' && <AudioPreview url={content} />}
          {type === 'file' && <FilePreview content={content} />}
        </div>
      </div>
    </div>
  )
}

/** 文本预览：折叠显示 */
function TextPreview({ content }: { content: string }) {
  const truncated =
    content.length > MAX_PREVIEW_CHARS
      ? content.slice(0, MAX_PREVIEW_CHARS) + '...'
      : content
  const lines = truncated.split('\n')
  const displayLines = lines.slice(0, MAX_PREVIEW_LINES)
  const hasMore = lines.length > MAX_PREVIEW_LINES || content.length > MAX_PREVIEW_CHARS

  return (
    <div className="rounded bg-muted/30 p-1.5 text-[11px] text-muted-foreground leading-relaxed">
      {displayLines.map((line, i) => (
        <div key={i}>{line || '\u00A0'}</div>
      ))}
      {hasMore && <div className="text-[10px] opacity-60">...</div>}
    </div>
  )
}

/** 图片预览：缩略图 + 增强功能 */
function ImagePreview({
  url,
  mediaMetadata,
}: {
  url: string
  mediaMetadata?: MediaMetadata
}) {
  const [showLightbox, setShowLightbox] = useState(false)
  const imageResult = mediaMetadata?.type === 'image' ? mediaMetadata.imageResult : undefined

  /** 生成 EXIF 简要摘要 */
  const exifSummary = useMemo(() => {
    if (!imageResult) return null
    const parts: string[] = []
    const exif = imageResult.exif ?? {}
    if (exif.Model) parts.push(exif.Model)
    if (exif.FNumber) parts.push(`f/${exif.FNumber}`)
    if (exif.ISOSpeedRatings) parts.push(`ISO ${exif.ISOSpeedRatings}`)
    return parts.length > 0 ? parts.join(' · ') : null
  }, [imageResult])

  return (
    <>
      <div className="group relative overflow-hidden rounded border border-border/30">
        <img
          src={url}
          alt="预览"
          className="h-auto max-h-32 w-full object-contain"
          loading="lazy"
          onError={(e) => {
            ;(e.target as HTMLImageElement).style.display = 'none'
          }}
        />

        {/* 悬浮操作层 */}
        <div className="absolute inset-0 flex items-center justify-center gap-2 bg-black/0 opacity-100 md:opacity-0 transition-all md:group-hover:bg-black/30 md:group-hover:opacity-100">
          <button
            className="flex h-7 w-7 items-center justify-center rounded-full bg-white/90 text-foreground shadow hover:bg-white transition-colors"
            onClick={() => setShowLightbox(true)}
            title="展开大图"
          >
            <Maximize2 className="h-3.5 w-3.5" />
          </button>
        </div>

        {/* 底部信息条 */}
        {imageResult && (
          <div className="absolute bottom-0 left-0 right-0 flex items-center gap-1.5 bg-gradient-to-t from-black/60 to-transparent px-2 py-1">
            <span className="rounded bg-black/50 px-1 py-0.5 text-[10px] text-white">
              {imageResult.width}×{imageResult.height}
            </span>
            {imageResult.format && (
              <span className="rounded bg-black/50 px-1 py-0.5 text-[10px] text-white">
                {imageResult.format.toUpperCase()}
              </span>
            )}
          </div>
        )}
      </div>

      {/* EXIF 简要摘要 */}
      {exifSummary && (
        <div className="mt-1 flex items-center gap-1 text-[10px] text-muted-foreground">
          <Info className="h-3 w-3 shrink-0" />
          <span className="truncate">{exifSummary}</span>
        </div>
      )}

      {/* 大图 Lightbox */}
      {showLightbox && (
        <ImageLightbox url={url} onClose={() => setShowLightbox(false)} imageResult={imageResult} />
      )}
    </>
  )
}

/** 图片大图预览 Lightbox */
function ImageLightbox({
  url,
  onClose,
  imageResult,
}: {
  url: string
  onClose: () => void
  imageResult?: { width: number; height: number; format: string }
}) {
  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/80"
      onClick={onClose}
    >
      <div
        className="relative max-h-[90vh] max-w-[90vw]"
        onClick={(e) => e.stopPropagation()}
      >
        <img
          src={url}
          alt="大图预览"
          className="max-h-[90vh] max-w-[90vw] rounded-lg object-contain"
        />
        {/* 关闭按钮 */}
        <button
          className="absolute -top-2 -right-2 flex h-7 w-7 items-center justify-center rounded-full bg-white text-foreground shadow-lg hover:bg-muted transition-colors"
          onClick={onClose}
        >
          <X className="h-4 w-4" />
        </button>
        {/* 底部尺寸信息 */}
        {imageResult && (
          <div className="absolute bottom-2 left-1/2 -translate-x-1/2 rounded-full bg-black/70 px-3 py-1 text-xs text-white">
            {imageResult.width} × {imageResult.height} px · {imageResult.format.toUpperCase()}
          </div>
        )}
      </div>
    </div>
  )
}

/** 视频预览：封面帧 + 增强功能 */
function VideoPreview({
  url,
  mediaMetadata,
}: {
  url: string
  mediaMetadata?: MediaMetadata
}) {
  const [playing, setPlaying] = useState(false)
  const videoResult = mediaMetadata?.type === 'video' ? mediaMetadata.videoResult : undefined

  const handleTogglePlay = useCallback((e: React.MouseEvent<HTMLVideoElement>) => {
    const video = e.currentTarget
    if (video.paused) {
      video.play()
      setPlaying(true)
    } else {
      video.pause()
      setPlaying(false)
    }
  }, [])

  return (
    <div className="relative overflow-hidden rounded border border-border/30">
      <video
        src={url}
        className="h-auto max-h-32 w-full"
        preload="metadata"
        muted
        onClick={handleTogglePlay}
        onPlay={() => setPlaying(true)}
        onPause={() => setPlaying(false)}
        onEnded={() => setPlaying(false)}
      />

      {/* 底部信息条 */}
      <div className="absolute bottom-1 left-1 flex items-center gap-1">
        <span className="rounded bg-black/60 px-1 py-0.5 text-[10px] text-white">
          {playing ? '⏸' : '▶'} 视频
        </span>
        {videoResult && videoResult.durationSeconds > 0 && (
          <span className="rounded bg-black/60 px-1 py-0.5 text-[10px] text-white tabular-nums">
            {formatDuration(videoResult.durationSeconds)}
          </span>
        )}
      </div>

      {/* 右下角分辨率 */}
      {videoResult && (
        <div className="absolute bottom-1 right-1">
          <span className="rounded bg-black/60 px-1 py-0.5 text-[10px] text-white">
            {videoResult.width}×{videoResult.height}
          </span>
        </div>
      )}

      {/* 编解码器标签 */}
      {videoResult?.codec && (
        <div className="absolute top-1 right-1">
          <span className="rounded bg-black/60 px-1 py-0.5 text-[10px] text-white">
            {videoResult.codec}
          </span>
        </div>
      )}
    </div>
  )
}

/** 音频预览：迷你播放器 */
function AudioPreview({ url }: { url: string }) {
  return (
    <audio src={url} controls className="h-6 w-full" preload="metadata">
      您的浏览器不支持音频播放
    </audio>
  )
}

/** 文件预览：文件名 */
function FilePreview({ content }: { content: string }) {
  // content 作为文件名显示
  const fileName = content.split('/').pop() || content
  return (
    <div className="text-[11px] text-muted-foreground">📄 {fileName}</div>
  )
}

/** 类型标签 */
function typeLabel(type: ArtifactType): string {
  const labels: Record<ArtifactType, string> = {
    text: '文本文档',
    image: '图片',
    video: '视频',
    audio: '音频',
    screenshot: '截图',
    file: '文件',
  }
  return labels[type]
}
