/**
 * MediaTimelineView - 媒体时间轴视图组件
 *
 * 视频播放器 + 时间轴标注系统。
 * 时间轴上显示标注标记点，支持播放控制和标注列表。
 */

import React, { useState, useCallback, useRef, useMemo } from 'react'
import type { Annotation } from '@/types/review'

export interface MediaTimelineViewProps {
  /** 媒体 URL */
  mediaUrl: string
  /** 媒体类型 */
  mediaType: 'video' | 'audio'
  /** 视频时长（秒），未知时由 <video> 元素获取 */
  duration?: number
  /** 已有时间轴批注 */
  annotations: Annotation[]
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
 * MediaTimelineView
 *
 * 媒体播放器 + 时间轴标注视图。
 */
export function MediaTimelineView({
  mediaUrl,
  mediaType,
  duration: propDuration,
  annotations,
  readOnly = false,
}: MediaTimelineViewProps) {
  const mediaRef = useRef<HTMLVideoElement | HTMLAudioElement>(null)
  const timelineRef = useRef<HTMLDivElement>(null)
  const [isPlaying, setIsPlaying] = useState(false)
  const [currentTime, setCurrentTime] = useState(0)
  const [mediaDuration, setMediaDuration] = useState(propDuration ?? 0)

  // 过滤出视频时间轴批注
  const videoAnnotations = useMemo(
    () =>
      annotations
        .filter((a) => a.type === 'video_timestamp' && a.timestamp != null)
        .sort((a, b) => (a.timestamp ?? 0) - (b.timestamp ?? 0)),
    [annotations],
  )

  /** 媒体元数据加载 */
  const handleLoadedMetadata = useCallback(() => {
    if (mediaRef.current) {
      setMediaDuration((mediaRef.current as HTMLVideoElement).duration || 0)
    }
  }, [])

  /** 时间更新 */
  const handleTimeUpdate = useCallback(() => {
    if (!mediaRef.current) return
    setCurrentTime(mediaRef.current.currentTime)
  }, [])

  /** 播放/暂停切换 */
  const togglePlay = useCallback(() => {
    if (!mediaRef.current) return
    if (isPlaying) {
      mediaRef.current.pause()
    } else {
      mediaRef.current.play()
    }
    setIsPlaying(!isPlaying)
  }, [isPlaying])

  /** 播放结束重置 */
  const handleEnded = useCallback(() => {
    setIsPlaying(false)
  }, [])

  /** 点击时间轴跳转 */
  const handleTimelineClick = useCallback(
    (e: React.MouseEvent) => {
      if (!timelineRef.current || mediaDuration <= 0) return
      const rect = timelineRef.current.getBoundingClientRect()
      const x = e.clientX - rect.left
      const ratio = x / rect.width
      const time = ratio * mediaDuration

      if (mediaRef.current) {
        mediaRef.current.currentTime = time
      }
    },
    [mediaDuration],
  )

  const progressPercent = mediaDuration > 0 ? (currentTime / mediaDuration) * 100 : 0

  return (
    <div className="media-timeline-view flex h-full flex-col" data-testid="media-timeline-view">
      {/* 媒体播放器 */}
      <div className={`relative flex-1 ${mediaType === 'video' ? 'bg-black' : 'bg-muted'}`}>
        {mediaType === 'video' ? (
          <video
            ref={mediaRef as React.RefObject<HTMLVideoElement>}
            src={mediaUrl}
            className="h-full w-full object-contain"
            onLoadedMetadata={handleLoadedMetadata}
            onTimeUpdate={handleTimeUpdate}
            onEnded={handleEnded}
            data-testid="video-player"
          />
        ) : (
          <audio
            ref={mediaRef as React.RefObject<HTMLAudioElement>}
            src={mediaUrl}
            onLoadedMetadata={handleLoadedMetadata}
            onTimeUpdate={handleTimeUpdate}
            onEnded={handleEnded}
            data-testid="audio-player"
          />
        )}

        {/* 播放控制 */}
        <div className="flex items-center gap-2 bg-background border-t border-border px-3 py-2">
          <button
            onClick={togglePlay}
            className="flex h-8 w-8 items-center justify-center rounded-full bg-blue-600 text-white hover:bg-blue-700"
            data-testid="play-pause-btn"
          >
            {isPlaying ? '⏸' : '▶'}
          </button>
          <span className="text-xs text-muted-foreground tabular-nums" data-testid="time-display">
            {formatTime(currentTime)} / {formatTime(mediaDuration)}
          </span>
        </div>
      </div>

      {/* 时间轴 */}
      <div className="border-t border-border bg-background px-3 py-2">
        <div
          ref={timelineRef}
          className="relative h-8 cursor-pointer rounded bg-muted"
          onClick={handleTimelineClick}
          data-testid="timeline-bar"
        >
          {/* 播放进度 */}
          <div
            className="absolute left-0 top-0 h-full rounded bg-blue-500/30"
            style={{ width: `${progressPercent}%` }}
            data-testid="timeline-progress"
          />

          {/* 播放头 */}
          <div
            className="absolute top-0 h-full w-0.5 bg-blue-500"
            style={{ left: `${progressPercent}%` }}
            data-testid="timeline-playhead"
          />

          {/* 标注标记点 */}
          {videoAnnotations.map((a, idx) => {
            const left = mediaDuration > 0 ? ((a.timestamp ?? 0) / mediaDuration) * 100 : 0
            return (
              <div
                key={a.id}
                className="absolute top-0 flex h-full flex-col items-center"
                style={{ left: `${left}%` }}
                data-testid={`timeline-marker-${idx}`}
                title={`${formatTime(a.timestamp ?? 0)} - ${a.suggestion}`}
              >
                <div className="mt-0.5 flex h-4 w-4 items-center justify-center rounded-full bg-yellow-500 text-[8px] font-bold text-white">
                  {idx + 1}
                </div>
              </div>
            )
          })}
        </div>

        {/* 标注列表 */}
        {videoAnnotations.length > 0 && (
          <div className="mt-2 space-y-1" data-testid="annotation-list">
            {videoAnnotations.map((a, idx) => (
              <div
                key={a.id}
                className="flex items-center gap-2 rounded bg-muted/50 px-2 py-1 text-xs"
                data-testid={`annotation-item-${idx}`}
              >
                <span className="flex h-4 w-4 shrink-0 items-center justify-center rounded-full bg-yellow-500 text-[8px] font-bold text-white">
                  {idx + 1}
                </span>
                <span className="shrink-0 text-muted-foreground tabular-nums">
                  {formatTime(a.timestamp ?? 0)}
                </span>
                <span className="flex-1 truncate text-foreground">{a.suggestion}</span>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
