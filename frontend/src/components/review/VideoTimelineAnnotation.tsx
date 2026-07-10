/**
 * VideoTimelineAnnotation - 视频时间轴标注组件
 *
 * HTML5 视频播放器 + 下方时间轴。
 * 时间轴上显示标注标记点，支持在时间轴上点击添加标注。
 */

import { Play, Pause, Plus, MessageSquare, SkipBack, SkipForward } from 'lucide-react'
import React, { useState, useCallback, useRef, useEffect } from 'react'
import type { Annotation } from '@/types/review'

/** 生成简易唯一 ID */
function uid(): string {
  return Date.now().toString(36) + Math.random().toString(36).slice(2)
}

export interface VideoTimelineAnnotationProps {
  /** 视频 URL */
  videoUrl: string
  /** 视频时长（秒），未知时由 <video> 元素获取 */
  duration?: number
  /** 已有时间轴批注 */
  annotations: Annotation[]
  /** 添加批注回调 */
  onAddAnnotation?: (annotation: Annotation) => void
  /** 删除批注回调 */
  onRemoveAnnotation?: (id: string) => void
  /** 当前播放时间 */
  currentTime?: number
  /** 时间更新回调 */
  onTimeUpdate?: (time: number) => void
  /** 是否只读 */
  readOnly?: boolean
}

/** 格式化秒数为 mm:ss */
function formatTime(seconds: number): string {
  const m = Math.floor(seconds / 60)
  const s = Math.floor(seconds % 60)
  return `${m.toString().padStart(2, '0')}:${s.toString().padStart(2, '0')}`
}

/**
 * VideoTimelineAnnotation
 *
 * 视频播放器 + 时间轴标注系统。
 * - 视频播放器支持播放/暂停、前进/后退
 * - 时间轴上显示标注标记点
 * - 点击时间轴可跳转或添加标注
 */
export function VideoTimelineAnnotation({
  videoUrl,
  duration: propDuration,
  annotations,
  onAddAnnotation,
  onRemoveAnnotation,
  onTimeUpdate,
  readOnly = false,
}: VideoTimelineAnnotationProps) {
  const videoRef = useRef<HTMLVideoElement>(null)
  const timelineRef = useRef<HTMLDivElement>(null)
  const [isPlaying, setIsPlaying] = useState(false)
  const [currentTime, setCurrentTime] = useState(0)
  const [videoDuration, setVideoDuration] = useState(propDuration ?? 0)
  const [addingAtTime, setAddingAtTime] = useState<number | null>(null)
  const [suggestionText, setSuggestionText] = useState('')

  // 过滤出视频时间轴批注
  const videoAnnotations = annotations.filter(
    (a) => a.type === 'video_timestamp' && a.timestamp != null,
  )

  // 按 timestamp 排序
  const sortedAnnotations = [...videoAnnotations].sort(
    (a, b) => (a.timestamp ?? 0) - (b.timestamp ?? 0),
  )

  /** 视频元数据加载 */
  const handleLoadedMetadata = useCallback(() => {
    if (videoRef.current) {
      setVideoDuration(videoRef.current.duration)
    }
  }, [])

  /** 时间更新 */
  const handleTimeUpdate = useCallback(() => {
    if (!videoRef.current) return
    const time = videoRef.current.currentTime
    setCurrentTime(time)
    onTimeUpdate?.(time)
  }, [onTimeUpdate])

  /** 播放/暂停 */
  const togglePlay = useCallback(() => {
    if (!videoRef.current) return
    if (isPlaying) {
      videoRef.current.pause()
    } else {
      videoRef.current.play()
    }
    setIsPlaying(!isPlaying)
  }, [isPlaying])

  /** 快退 5 秒 */
  const skipBack = useCallback(() => {
    if (!videoRef.current) return
    videoRef.current.currentTime = Math.max(0, videoRef.current.currentTime - 5)
  }, [])

  /** 快进 5 秒 */
  const skipForward = useCallback(() => {
    if (!videoRef.current) return
    videoRef.current.currentTime = Math.min(
      videoDuration,
      videoRef.current.currentTime + 5,
    )
  }, [videoDuration])

  /** 点击时间轴跳转或添加标注 */
  const handleTimelineClick = useCallback(
    (e: React.MouseEvent) => {
      if (!timelineRef.current || videoDuration <= 0) return

      const rect = timelineRef.current.getBoundingClientRect()
      const x = e.clientX - rect.left
      const ratio = x / rect.width
      const time = ratio * videoDuration

      if (readOnly) {
        // 只读模式：跳转播放位置
        if (videoRef.current) {
          videoRef.current.currentTime = time
        }
        return
      }

      // 检查是否点击了已有的标注标记
      const clickedAnnotation = sortedAnnotations.find((a) => {
        const markerRatio = (a.timestamp ?? 0) / videoDuration
        const markerX = markerRatio * rect.width
        return Math.abs(x - markerX) < 8
      })

      if (clickedAnnotation) {
        // 跳转到标注时间点
        if (videoRef.current) {
          videoRef.current.currentTime = clickedAnnotation.timestamp!
        }
        return
      }

      // 添加标注模式
      setAddingAtTime(time)
      setSuggestionText('')
    },
    [videoDuration, readOnly, sortedAnnotations],
  )

  /** 提交时间轴标注 */
  const handleSubmitAnnotation = useCallback(() => {
    if (addingAtTime == null || !suggestionText.trim() || !onAddAnnotation) return

    onAddAnnotation({
      id: uid(),
      type: 'video_timestamp',
      timestamp: addingAtTime,
      suggestion: suggestionText.trim(),
      createdAt: new Date().toISOString(),
    })

    setAddingAtTime(null)
    setSuggestionText('')
  }, [addingAtTime, suggestionText, onAddAnnotation])

  /** 播放结束时重置状态 */
  const handleEnded = useCallback(() => {
    setIsPlaying(false)
  }, [])

  return (
    <div className="video-timeline-annotation flex h-full flex-col">
      {/* 视频播放器 */}
      <div className="relative flex-1 bg-black">
        <video
          ref={videoRef}
          src={videoUrl}
          className="h-full w-full object-contain"
          onLoadedMetadata={handleLoadedMetadata}
          onTimeUpdate={handleTimeUpdate}
          onEnded={handleEnded}
          onClick={togglePlay}
        />

        {/* 播放控制叠加层 */}
        <div className="absolute bottom-0 left-0 right-0 flex items-center gap-2 bg-gradient-to-t from-black/60 to-transparent px-3 py-2">
          <button
            className="flex h-7 w-7 items-center justify-center rounded-full text-white/80 hover:bg-white/20 hover:text-white transition-colors"
            onClick={skipBack}
            title="后退 5 秒"
          >
            <SkipBack className="h-3.5 w-3.5" />
          </button>
          <button
            className="flex h-8 w-8 items-center justify-center rounded-full bg-white/20 text-white hover:bg-white/30 transition-colors"
            onClick={togglePlay}
            title={isPlaying ? '暂停' : '播放'}
          >
            {isPlaying ? (
              <Pause className="h-4 w-4" />
            ) : (
              <Play className="h-4 w-4 ml-0.5" />
            )}
          </button>
          <button
            className="flex h-7 w-7 items-center justify-center rounded-full text-white/80 hover:bg-white/20 hover:text-white transition-colors"
            onClick={skipForward}
            title="前进 5 秒"
          >
            <SkipForward className="h-3.5 w-3.5" />
          </button>
          <span className="ml-2 text-xs text-white/80 tabular-nums">
            {formatTime(currentTime)} / {formatTime(videoDuration)}
          </span>
        </div>
      </div>

      {/* 时间轴 */}
      <div className="border-t border-border bg-background px-3 py-2">
        {/* 时间轴条 */}
        <div
          ref={timelineRef}
          className="relative h-8 cursor-pointer rounded bg-muted"
          onClick={handleTimelineClick}
        >
          {/* 播放进度 */}
          <div
            className="absolute left-0 top-0 h-full rounded bg-blue-500/30 transition-all duration-100"
            style={{
              width: `${videoDuration > 0 ? (currentTime / videoDuration) * 100 : 0}%`,
            }}
          />

          {/* 播放头 */}
          <div
            className="absolute top-0 h-full w-0.5 bg-blue-500 transition-all duration-100"
            style={{
              left: `${videoDuration > 0 ? (currentTime / videoDuration) * 100 : 0}%`,
            }}
          />

          {/* 标注标记点 */}
          {sortedAnnotations.map((a, idx) => {
            const left =
              videoDuration > 0 ? ((a.timestamp ?? 0) / videoDuration) * 100 : 0
            return (
              <div
                key={a.id}
                className="group absolute top-0 flex h-full flex-col items-center"
                style={{ left: `${left}%` }}
                title={`${formatTime(a.timestamp ?? 0)} - ${a.suggestion}`}
              >
                {/* 标记三角 */}
                <div className="mt-0.5 flex h-4 w-4 items-center justify-center rounded-full bg-yellow-500 text-[8px] font-bold text-white">
                  {idx + 1}
                </div>
                {/* 悬停提示 */}
                <div className="absolute top-full mt-1 block md:hidden md:group-hover:block w-32 rounded bg-black/80 p-1 text-[10px] text-white z-10">
                  <div className="font-medium">{formatTime(a.timestamp ?? 0)}</div>
                  <div className="truncate">{a.suggestion}</div>
                </div>
              </div>
            )
          })}

          {/* 添加标注提示 */}
          {addingAtTime != null && (
            <div
              className="absolute top-0 flex h-full items-end"
              style={{
                left: `${videoDuration > 0 ? (addingAtTime / videoDuration) * 100 : 0}%`,
                transform: 'translateX(-50%)',
              }}
            >
              <Plus className="h-5 w-5 animate-bounce text-green-500" />
            </div>
          )}
        </div>

        {/* 添加标注输入框 */}
        {addingAtTime != null && !readOnly && (
          <div className="mt-2 flex items-center gap-2 rounded border border-border bg-muted/30 p-2">
            <MessageSquare className="h-4 w-4 shrink-0 text-yellow-600" />
            <span className="shrink-0 text-xs text-muted-foreground">
              {formatTime(addingAtTime)}:
            </span>
            <input
              type="text"
              value={suggestionText}
              onChange={(e) => setSuggestionText(e.target.value)}
              placeholder="输入标注说明..."
              autoFocus
              className="flex-1 bg-transparent text-xs outline-none placeholder:text-muted-foreground/50"
              onKeyDown={(e) => {
                if (e.key === 'Enter') handleSubmitAnnotation()
                if (e.key === 'Escape') {
                  setAddingAtTime(null)
                  setSuggestionText('')
                }
              }}
            />
            <button
              className="shrink-0 rounded bg-yellow-600 px-2 py-0.5 text-[10px] text-white hover:bg-yellow-700 disabled:opacity-50"
              onClick={handleSubmitAnnotation}
              disabled={!suggestionText.trim()}
            >
              添加
            </button>
            <button
              className="shrink-0 rounded px-2 py-0.5 text-[10px] text-muted-foreground hover:bg-accent"
              onClick={() => {
                setAddingAtTime(null)
                setSuggestionText('')
              }}
            >
              取消
            </button>
          </div>
        )}

        {/* 标注列表 */}
        {sortedAnnotations.length > 0 && (
          <div className="mt-2 space-y-1">
            {sortedAnnotations.map((a, idx) => (
              <div
                key={a.id}
                className="flex items-center gap-2 rounded bg-muted/50 px-2 py-1 text-xs"
              >
                <span className="flex h-4 w-4 shrink-0 items-center justify-center rounded-full bg-yellow-500 text-[8px] font-bold text-white">
                  {idx + 1}
                </span>
                <span className="shrink-0 text-muted-foreground tabular-nums">
                  {formatTime(a.timestamp ?? 0)}
                </span>
                <span className="flex-1 truncate text-foreground">{a.suggestion}</span>
                {!readOnly && onRemoveAnnotation && (
                  <button
                    className="shrink-0 text-muted-foreground hover:text-red-500"
                    onClick={() => onRemoveAnnotation(a.id)}
                    title="删除标注"
                  >
                    ×
                  </button>
                )}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
