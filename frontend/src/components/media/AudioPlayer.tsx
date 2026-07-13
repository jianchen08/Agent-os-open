/**
 * AudioPlayer 组件
 *
 * 支持播放/暂停/下载功能的音频播放器，可接收音频 URL 或 blob 数据。
 * 适用于 TTS 输出音频的播放展示，可内嵌在消息流中。
 *
 * 功能：
 * - 播放/暂停切换
 * - 进度条拖拽与点击
 * - 播放时长显示
 * - 静音切换
 * - 音频下载
 * - 支持多种音频格式（mp3, wav, ogg）
 * - 响应式设计，适配移动端
 *
 * @example
 * ```tsx
 * <AudioPlayer src="https://example.com/tts-output.mp3" title="TTS 输出" />
 * <AudioPlayer src={blobUrl} title="实时合成" />
 * ```
 */

import {
  Download,
  Pause,
  Play,
  Volume2,
  VolumeX,
} from 'lucide-react'
import { memo, useCallback, useRef, useState } from 'react'

/** AudioPlayer 组件属性 */
export interface AudioPlayerProps {
  /** 音频 URL 或 blob 数据 URL */
  src: string
  /** 音频标题（可选） */
  title?: string
  /** 自定义类名 */
  className?: string
}

/**
 * 格式化秒数为 "m:ss" 格式。
 *
 * @param seconds - 秒数
 * @returns 格式化后的时间字符串
 */
function formatTime(seconds: number): string {
  if (!isFinite(seconds) || isNaN(seconds)) return '0:00'
  const minutes = Math.floor(seconds / 60)
  const secs = Math.floor(seconds % 60)
  return `${minutes}:${secs.toString().padStart(2, '0')}`
}

/**
 * 音频播放器组件
 *
 * 支持播放/暂停/下载，可接收 URL 或 blob 数据，
 * 播放进度条和时长显示，可内嵌在消息流中。
 */
export const AudioPlayer = memo<AudioPlayerProps>(
  ({ src, title, className = '' }) => {
    const [isPlaying, setIsPlaying] = useState(false)
    const [isMuted, setIsMuted] = useState(false)
    const [currentTime, setCurrentTime] = useState(0)
    const [duration, setDuration] = useState(0)
    const [error, setError] = useState<string>('')
    const [isLoading, setIsLoading] = useState(true)
    const audioRef = useRef<HTMLAudioElement>(null)
    const progressRef = useRef<HTMLDivElement>(null)

    /** 播放/暂停切换 */
    const togglePlay = useCallback(() => {
      const audio = audioRef.current
      if (!audio) return

      if (isPlaying) {
        audio.pause()
      } else {
        audio.play().catch((err: Error) => {
          setError('播放失败: ' + err.message)
        })
      }
    }, [isPlaying])

    /** 静音切换 */
    const toggleMute = useCallback(() => {
      const audio = audioRef.current
      if (!audio) return
      audio.muted = !isMuted
      setIsMuted(!isMuted)
    }, [isMuted])

    /** 进度条点击定位 */
    const handleProgressClick = useCallback(
      (e: React.MouseEvent<HTMLDivElement>) => {
        const audio = audioRef.current
        if (!audio || !duration) return
        const rect = e.currentTarget.getBoundingClientRect()
        const percent = Math.max(
          0,
          Math.min(1, (e.clientX - rect.left) / rect.width)
        )
        audio.currentTime = percent * duration
      },
      [duration]
    )

    /** 下载音频文件 */
    const handleDownload = useCallback(() => {
      const link = document.createElement('a')
      link.href = src
      link.download = title || 'audio'
      document.body.appendChild(link)
      link.click()
      document.body.removeChild(link)
    }, [src, title])

    /** 音频元数据加载完成 */
    const handleLoadedMetadata = useCallback(() => {
      const audio = audioRef.current
      if (audio) {
        setDuration(audio.duration)
        setIsLoading(false)
      }
    }, [])

    /** 播放时间更新 */
    const handleTimeUpdate = useCallback(() => {
      setCurrentTime(audioRef.current?.currentTime || 0)
    }, [])

    /** 音频加载错误 */
    const handleError = useCallback(() => {
      setError('音频加载失败，请检查链接是否有效')
      setIsLoading(false)
    }, [])

    // 计算进度百分比
    const progressPercent = duration
      ? (currentTime / duration) * 100
      : 0

    // 渲染错误状态
    if (error) {
      return (
        <div
          className={`my-4 rounded-xl border border-[var(--accent-error,#ef4444)]/20 bg-[var(--accent-error,#ef4444)]/10 p-4 ${className}`}
        >
          <div className="flex items-center gap-2 text-[var(--accent-error,#ef4444)]">
            <span className="text-sm">{error}</span>
          </div>
          <details className="mt-2 text-xs text-muted-foreground">
            <summary className="cursor-pointer hover:text-foreground">
              查看音频链接
            </summary>
            <code className="mt-1 block rounded bg-muted p-2 text-xs break-all">
              {src}
            </code>
          </details>
        </div>
      )
    }

    return (
      <div
        data-testid="audio-player"
        className={`my-4 rounded-xl border border-border bg-muted/30 p-4 ${className}`}
      >
        {/* 隐藏的 audio 元素 */}
        <audio
          ref={audioRef}
          src={src}
          onLoadedMetadata={handleLoadedMetadata}
          onTimeUpdate={handleTimeUpdate}
          onPlay={() => setIsPlaying(true)}
          onPause={() => setIsPlaying(false)}
          onEnded={() => setIsPlaying(false)}
          onError={handleError}
          preload="metadata"
        />

        {/* 标题 */}
        {title && (
          <div className="mb-3 truncate text-sm font-medium text-foreground">
            {title}
          </div>
        )}

        {/* 控制栏 */}
        <div className="flex items-center gap-3">
          {/* 播放/暂停按钮 */}
          <button
            onClick={togglePlay}
            disabled={isLoading}
            className="flex h-10 w-10 flex-shrink-0 items-center justify-center rounded-full bg-[var(--btn-primary-bg)] text-[var(--btn-primary-text)] transition-colors hover:bg-[var(--btn-primary-hover-bg)] disabled:opacity-50"
            aria-label={isPlaying ? '暂停' : '播放'}
          >
            {isPlaying ? (
              <Pause className="h-5 w-5" />
            ) : (
              <Play className="ml-0.5 h-5 w-5" />
            )}
          </button>

          {/* 进度区域 */}
          <div className="flex flex-1 items-center gap-2">
            <span className="w-10 text-right text-xs text-[var(--muted-foreground)]">
              {formatTime(currentTime)}
            </span>

            {/* 进度条 */}
            <div
              ref={progressRef}
              data-testid="progress-bar"
              className="relative h-2 flex-1 cursor-pointer overflow-hidden rounded-full bg-[var(--muted)]"
              onClick={handleProgressClick}
            >
              <div
                className="absolute inset-y-0 left-0 rounded-full bg-[var(--btn-primary-bg)] transition-all duration-150"
                style={{ width: `${progressPercent}%` }}
              />
            </div>

            <span className="w-10 text-xs text-[var(--muted-foreground)]">
              {isLoading ? '加载中' : formatTime(duration)}
            </span>
          </div>

          {/* 静音按钮 */}
          <button
            onClick={toggleMute}
            className="flex h-8 w-8 items-center justify-center rounded-lg transition-colors hover:bg-[var(--muted)]"
            aria-label={isMuted ? '取消静音' : '静音'}
          >
            {isMuted ? (
              <VolumeX className="h-4 w-4" />
            ) : (
              <Volume2 className="h-4 w-4" />
            )}
          </button>

          {/* 下载按钮 */}
          <button
            onClick={handleDownload}
            className="flex h-8 w-8 items-center justify-center rounded-lg transition-colors hover:bg-[var(--muted)]"
            aria-label="下载音频"
          >
            <Download className="h-4 w-4" />
          </button>
        </div>
      </div>
    )
  }
)

AudioPlayer.displayName = 'AudioPlayer'

export default AudioPlayer
