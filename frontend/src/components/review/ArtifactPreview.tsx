/**
 * ArtifactPreview - 制品预览组件
 *
 * 用于嵌入 InteractionCard 的 conversation 模式。
 * 根据 artifact type 自动选择渲染方式，折叠显示，
 * 提供"在工作区打开完整文档"跳转按钮。
 */

import React, { useState } from 'react'
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
} from 'lucide-react'
import type { Artifact, ArtifactType } from '@/types/review'

export interface ArtifactPreviewProps {
  /** 制品列表 */
  artifacts: Artifact[]
  /** 跳转到工作区审阅回调 */
  onNavigateToWorkspace?: () => void
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

/**
 * ArtifactPreview
 *
 * 展示制品的折叠预览：
 * - text → 折叠文本
 * - image → 缩略图
 * - video → 视频缩略图（封面帧）
 * - audio → 播放器
 * - screenshot → 截图缩略图
 * - file → 文件图标 + 名称
 */
export function ArtifactPreview({
  artifacts,
  onNavigateToWorkspace,
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
          <ArtifactItem key={artifact.id} artifact={artifact} />
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
function ArtifactItem({ artifact }: { artifact: Artifact }) {
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
          {(type === 'image' || type === 'screenshot') && <ImagePreview url={content} />}
          {type === 'video' && <VideoPreview url={content} />}
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

/** 图片预览：缩略图 */
function ImagePreview({ url }: { url: string }) {
  return (
    <div className="overflow-hidden rounded border border-border/30">
      <img
        src={url}
        alt="预览"
        className="h-auto max-h-32 w-full object-contain"
        loading="lazy"
        onError={(e) => {
          ;(e.target as HTMLImageElement).style.display = 'none'
        }}
      />
    </div>
  )
}

/** 视频预览：封面帧 */
function VideoPreview({ url }: { url: string }) {
  return (
    <div className="relative overflow-hidden rounded border border-border/30">
      <video
        src={url}
        className="h-auto max-h-32 w-full"
        preload="metadata"
        muted
      />
      <div className="absolute bottom-1 left-1 rounded bg-black/60 px-1 py-0.5 text-[10px] text-white">
        ▶ 视频
      </div>
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
