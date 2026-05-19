/**
 * MediaMetadataPanel - 媒体元数据面板
 *
 * 展示图片 EXIF 信息（相机、GPS、方向、时间等）或
 * 视频元数据（时长、分辨率、帧率、编解码器）。
 * 支持紧凑模式和完整模式。
 */

import {
  Camera,
  Video,
  AlertTriangle,
  AlertCircle,
  ChevronDown,
  ChevronUp,
  MapPin,
  Clock,
  Monitor,
  Aperture,
  Hash,
} from 'lucide-react'
import React, { useState, useMemo } from 'react'
import type { MediaMetadata, ImageReviewResult, VideoReviewResult } from '@/types/review'

export interface MediaMetadataPanelProps {
  /** 媒体元数据 */
  metadata: MediaMetadata
  /** 是否紧凑模式：只显示关键摘要 */
  compact?: boolean
}

/** 格式化时长为 mm:ss 或 hh:mm:ss */
function formatDuration(seconds: number): string {
  const h = Math.floor(seconds / 3600)
  const m = Math.floor((seconds % 3600) / 60)
  const s = Math.floor(seconds % 60)
  if (h > 0) {
    return `${h}:${m.toString().padStart(2, '0')}:${s.toString().padStart(2, '0')}`
  }
  return `${m}:${s.toString().padStart(2, '0')}`
}

/** 计算宽高比描述 */
function aspectRatioLabel(ratio: number): string {
  const common: Record<string, [number, number]> = {
    '1:1': [1, 1],
    '4:3': [4, 3],
    '3:2': [3, 2],
    '16:9': [16, 9],
    '16:10': [16, 10],
    '21:9': [21, 9],
    '2:1': [2, 1],
    '9:16': [9, 16],
    '3:4': [3, 4],
    '2:3': [2, 3],
  }
  for (const [label, [w, h]] of Object.entries(common)) {
    if (Math.abs(ratio - w / h) < 0.02) return label
  }
  return ratio.toFixed(2)
}

/** EXIF 字段中文映射 */
const EXIF_LABELS: Record<string, string> = {
  Make: '相机厂商',
  Model: '相机型号',
  LensModel: '镜头型号',
  DateTimeOriginal: '拍摄时间',
  ExposureTime: '曝光时间',
  FNumber: '光圈值',
  ISOSpeedRatings: 'ISO',
  FocalLength: '焦距',
  Flash: '闪光灯',
  WhiteBalance: '白平衡',
  ExposureProgram: '曝光程序',
  MeteringMode: '测光模式',
  Orientation: '方向',
  XResolution: '水平分辨率',
  YResolution: '垂直分辨率',
  Software: '软件',
  GPSLatitude: '纬度',
  GPSLongitude: '经度',
  GPSAltitude: '海拔',
  ImageWidth: '图片宽度',
  ImageHeight: '图片高度',
  ColorSpace: '色彩空间',
}

/** 生成图片紧凑摘要 */
function imageSummary(result: ImageReviewResult): string {
  const parts: string[] = []
  parts.push(`${result.width}x${result.height}`)
  parts.push(result.format.toUpperCase())
  parts.push(`${aspectRatioLabel(result.aspectRatio)}`)
  return parts.join(', ')
}

/** 生成视频紧凑摘要 */
function videoSummary(result: VideoReviewResult): string {
  const parts: string[] = []
  parts.push(`${result.width}x${result.height}`)
  parts.push(`${formatDuration(result.durationSeconds)}`)
  parts.push(result.codec)
  parts.push(`${result.fps}fps`)
  return parts.join(', ')
}

/** 警告标签组件 */
function WarningBadge({ message }: { message: string }) {
  return (
    <div className="flex items-start gap-1.5 rounded-md bg-yellow-50 px-2 py-1.5 text-[11px] text-yellow-800 dark:bg-yellow-900/20 dark:text-yellow-200">
      <AlertTriangle className="mt-0.5 h-3 w-3 shrink-0" />
      <span>{message}</span>
    </div>
  )
}

/** 错误标签组件 */
function ErrorBadge({ message }: { message: string }) {
  return (
    <div className="flex items-start gap-1.5 rounded-md bg-red-50 px-2 py-1.5 text-[11px] text-red-800 dark:bg-red-900/20 dark:text-red-200">
      <AlertCircle className="mt-0.5 h-3 w-3 shrink-0" />
      <span>{message}</span>
    </div>
  )
}

/** 键值对行 */
function MetaRow({ label, value, icon }: { label: string; value: string; icon?: React.ReactNode }) {
  return (
    <div className="flex items-center justify-between py-1.5">
      <span className="flex items-center gap-1.5 text-xs text-muted-foreground">
        {icon}
        {label}
      </span>
      <span className="text-xs font-medium text-foreground">{value}</span>
    </div>
  )
}

/** 分组标题 */
function SectionTitle({ children }: { children: React.ReactNode }) {
  return (
    <div className="mt-3 mb-1 flex items-center gap-1.5 border-b border-border/50 pb-1 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
      {children}
    </div>
  )
}

/** 图片元数据完整展示 */
function ImageMetadataFull({ result }: { result: ImageReviewResult }) {
  const exif = result.exif ?? {}

  // 按类别分组 EXIF
  const cameraFields: [string, string][] = []
  const captureFields: [string, string][] = []
  const gpsFields: [string, string][] = []
  const otherFields: [string, string][] = []

  const cameraKeys = new Set(['Make', 'Model', 'LensModel', 'Software'])
  const captureKeys = new Set([
    'DateTimeOriginal',
    'ExposureTime',
    'FNumber',
    'ISOSpeedRatings',
    'FocalLength',
    'Flash',
    'WhiteBalance',
    'ExposureProgram',
    'MeteringMode',
    'Orientation',
  ])
  const gpsKeys = new Set(['GPSLatitude', 'GPSLongitude', 'GPSAltitude'])

  for (const [key, rawVal] of Object.entries(exif)) {
    if (rawVal == null || rawVal === '') continue
    const val = String(rawVal)
    if (cameraKeys.has(key)) cameraFields.push([EXIF_LABELS[key] ?? key, val])
    else if (captureKeys.has(key)) captureFields.push([EXIF_LABELS[key] ?? key, val])
    else if (gpsKeys.has(key)) gpsFields.push([EXIF_LABELS[key] ?? key, val])
    else otherFields.push([EXIF_LABELS[key] ?? key, val])
  }

  return (
    <div className="space-y-0.5">
      {/* 基本信息 */}
      <SectionTitle>
        <Monitor className="h-3 w-3" />
        基本信息
      </SectionTitle>
      <MetaRow label="格式" value={result.format.toUpperCase()} icon={<Hash className="h-3 w-3" />} />
      <MetaRow label="尺寸" value={`${result.width} × ${result.height} px`} icon={<Monitor className="h-3 w-3" />} />
      <MetaRow
        label="宽高比"
        value={aspectRatioLabel(result.aspectRatio)}
        icon={<Aperture className="h-3 w-3" />}
      />

      {/* 相机信息 */}
      {cameraFields.length > 0 && (
        <>
          <SectionTitle>
            <Camera className="h-3 w-3" />
            相机信息
          </SectionTitle>
          {cameraFields.map(([label, val]) => (
            <MetaRow key={label} label={label} value={val} />
          ))}
        </>
      )}

      {/* 拍摄参数 */}
      {captureFields.length > 0 && (
        <>
          <SectionTitle>
            <Clock className="h-3 w-3" />
            拍摄参数
          </SectionTitle>
          {captureFields.map(([label, val]) => (
            <MetaRow key={label} label={label} value={val} />
          ))}
        </>
      )}

      {/* GPS 信息 */}
      {gpsFields.length > 0 && (
        <>
          <SectionTitle>
            <MapPin className="h-3 w-3" />
            位置信息
          </SectionTitle>
          {gpsFields.map(([label, val]) => (
            <MetaRow key={label} label={label} value={val} />
          ))}
        </>
      )}

      {/* 其他 EXIF */}
      {otherFields.length > 0 && (
        <>
          <SectionTitle>
            <Aperture className="h-3 w-3" />
            其他信息
          </SectionTitle>
          {otherFields.map(([label, val]) => (
            <MetaRow key={label} label={label} value={val} />
          ))}
        </>
      )}
    </div>
  )
}

/** 视频元数据完整展示 */
function VideoMetadataFull({ result }: { result: VideoReviewResult }) {
  return (
    <div className="space-y-0.5">
      <SectionTitle>
        <Video className="h-3 w-3" />
        基本信息
      </SectionTitle>
      <MetaRow label="格式" value={result.format.toUpperCase()} icon={<Hash className="h-3 w-3" />} />
      <MetaRow
        label="时长"
        value={formatDuration(result.durationSeconds)}
        icon={<Clock className="h-3 w-3" />}
      />
      <MetaRow label="分辨率" value={`${result.width} × ${result.height} px`} icon={<Monitor className="h-3 w-3" />} />
      <MetaRow label="帧率" value={`${result.fps} fps`} icon={<Aperture className="h-3 w-3" />} />
      <MetaRow label="编解码器" value={result.codec} icon={<Video className="h-3 w-3" />} />
    </div>
  )
}

/**
 * MediaMetadataPanel
 *
 * 展示媒体元数据的面板组件。
 * compact 模式显示单行摘要；完整模式分组展示所有元数据。
 */
export function MediaMetadataPanel({ metadata, compact = false }: MediaMetadataPanelProps) {
  const [expanded, setExpanded] = useState(false)

  const isImage = metadata.type === 'image'
  const imageResult = metadata.imageResult
  const videoResult = metadata.videoResult

  const warnings = useMemo(() => {
    const list: string[] = []
    if (imageResult) list.push(...imageResult.warnings)
    if (videoResult) list.push(...videoResult.warnings)
    return list
  }, [imageResult, videoResult])

  const errors = useMemo(() => {
    const list: string[] = []
    if (imageResult) list.push(...imageResult.errors)
    if (videoResult) list.push(...videoResult.errors)
    return list
  }, [imageResult, videoResult])

  // 紧凑模式
  if (compact) {
    const summary = isImage && imageResult
      ? imageSummary(imageResult)
      : videoResult
        ? videoSummary(videoResult)
        : ''

    const hasIssues = warnings.length > 0 || errors.length > 0

    return (
      <div className="flex items-center gap-2 text-[11px] text-muted-foreground">
        {isImage ? (
          <Camera className="h-3 w-3 shrink-0" />
        ) : (
          <Video className="h-3 w-3 shrink-0" />
        )}
        <span className="truncate">{summary}</span>
        {errors.length > 0 && (
          <span className="shrink-0 rounded-full bg-red-100 px-1.5 py-0.5 text-[10px] font-medium text-red-700 dark:bg-red-900/30 dark:text-red-300">
            {errors.length} 错误
          </span>
        )}
        {warnings.length > 0 && (
          <span className="shrink-0 rounded-full bg-yellow-100 px-1.5 py-0.5 text-[10px] font-medium text-yellow-700 dark:bg-yellow-900/30 dark:text-yellow-300">
            {warnings.length} 警告
          </span>
        )}
        {!hasIssues && (
          <span className="shrink-0 rounded-full bg-green-100 px-1.5 py-0.5 text-[10px] font-medium text-green-700 dark:bg-green-900/30 dark:text-green-300">
            ✓ 有效
          </span>
        )}
      </div>
    )
  }

  // 完整模式
  return (
    <div className="media-metadata-panel rounded-lg border border-border bg-background">
      {/* 标题栏 */}
      <button
        className="flex w-full items-center justify-between px-3 py-2 text-xs font-medium text-foreground hover:bg-muted/50 transition-colors"
        onClick={() => setExpanded(!expanded)}
      >
        <span className="flex items-center gap-1.5">
          {isImage ? <Camera className="h-3.5 w-3.5" /> : <Video className="h-3.5 w-3.5" />}
          媒体元数据
          {!expanded && (isImage && imageResult) && (
            <span className="text-muted-foreground">
              — {imageSummary(imageResult)}
            </span>
          )}
          {!expanded && (!isImage && videoResult) && (
            <span className="text-muted-foreground">
              — {videoSummary(videoResult)}
            </span>
          )}
        </span>
        {expanded ? (
          <ChevronUp className="h-3.5 w-3.5 text-muted-foreground" />
        ) : (
          <ChevronDown className="h-3.5 w-3.5 text-muted-foreground" />
        )}
      </button>

      {/* 展开内容 */}
      {expanded && (
        <div className="border-t border-border px-3 pb-3">
          {/* 有效状态指示 */}
          <div className="mt-2 flex items-center gap-2">
            {(isImage ? imageResult?.isValid : videoResult?.isValid) ? (
              <span className="inline-flex items-center gap-1 rounded-full bg-green-100 px-2 py-0.5 text-[10px] font-medium text-green-700 dark:bg-green-900/30 dark:text-green-300">
                ✓ 有效文件
              </span>
            ) : (
              <span className="inline-flex items-center gap-1 rounded-full bg-red-100 px-2 py-0.5 text-[10px] font-medium text-red-700 dark:bg-red-900/30 dark:text-red-300">
                ✗ 无效文件
              </span>
            )}
          </div>

          {/* 分组元数据 */}
          {isImage && imageResult && <ImageMetadataFull result={imageResult} />}
          {!isImage && videoResult && <VideoMetadataFull result={videoResult} />}

          {/* 警告列表 */}
          {warnings.length > 0 && (
            <div className="mt-3 space-y-1">
              <div className="flex items-center gap-1 text-[11px] font-medium text-yellow-700 dark:text-yellow-300">
                <AlertTriangle className="h-3 w-3" />
                警告 ({warnings.length})
              </div>
              {warnings.map((w, i) => (
                <WarningBadge key={i} message={w} />
              ))}
            </div>
          )}

          {/* 错误列表 */}
          {errors.length > 0 && (
            <div className="mt-3 space-y-1">
              <div className="flex items-center gap-1 text-[11px] font-medium text-red-700 dark:text-red-300">
                <AlertCircle className="h-3 w-3" />
                错误 ({errors.length})
              </div>
              {errors.map((e, i) => (
                <ErrorBadge key={i} message={e} />
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  )
}
