/**
 * MediaRenderer 组件
 * 渲染音频和视频内容
 */

import {
  AlertCircle,
  Download,
  Maximize,
  Pause,
  Play,
  Volume2,
  VolumeX,
} from 'lucide-react'
import { memo, useRef, useState } from 'react'

interface AudioRendererProps {
  /** 音频 URL 或 base64 数据 */
  src: string
  /** 音频标题 */
  title?: string
  /** 自定义类名 */
  className?: string
}

/**
 * 音频播放器组件
 */
export const AudioRenderer = memo<AudioRendererProps>(
  ({ src, title, className = '' }) => {
    const [isPlaying, setIsPlaying] = useState(false)
    const [isMuted, setIsMuted] = useState(false)
    const [currentTime, setCurrentTime] = useState(0)
    const [duration, setDuration] = useState(0)
    const [error, setError] = useState<string>('')
    const [isLoading, setIsLoading] = useState(true)
    const audioRef = useRef<HTMLAudioElement>(null)

    /** 格式化时间显示 */
    const formatTime = (time: number) => {
      if (!isFinite(time) || isNaN(time)) return '0:00'
      const minutes = Math.floor(time / 60)
      const seconds = Math.floor(time % 60)
      return `${minutes}:${seconds.toString().padStart(2, '0')}`
    }

    /** 播放/暂停切换 */
    const togglePlay = () => {
      if (!audioRef.current) return
      if (isPlaying) {
        audioRef.current.pause()
      } else {
        audioRef.current.play().catch(err => {
          setError('播放失败: ' + err.message)
        })
      }
    }

    /** 静音切换 */
    const toggleMute = () => {
      if (!audioRef.current) return
      audioRef.current.muted = !isMuted
      setIsMuted(!isMuted)
    }

    /** 进度条点击 */
    const handleProgressClick = (e: React.MouseEvent<HTMLDivElement>) => {
      if (!audioRef.current || !duration) return
      const rect = e.currentTarget.getBoundingClientRect()
      const percent = (e.clientX - rect.left) / rect.width
      audioRef.current.currentTime = percent * duration
    }

    /** 下载音频 */
    const handleDownload = () => {
      const link = document.createElement('a')
      link.href = src
      link.download = title || 'audio'
      link.click()
    }

    if (error) {
      return (
        <div
          className={`audio-error my-4 p-4 bg-destructive/10 rounded-xl border border-destructive/20 ${className}`}
        >
          <div className="flex items-center gap-2 text-destructive">
            <AlertCircle className="h-4 w-4 flex-shrink-0" />
            <span className="text-sm">{error}</span>
          </div>
          <details className="mt-2 text-xs text-muted-foreground">
            <summary className="cursor-pointer hover:text-foreground">
              查看音频链接
            </summary>
            <code className="mt-1 block p-2 bg-muted rounded text-xs break-all">
              {src}
            </code>
          </details>
        </div>
      )
    }

    return (
      <div
        className={`audio-player my-4 p-4 bg-muted/30 rounded-xl border border-border ${className}`}
      >
        <audio
          ref={audioRef}
          src={src}
          onLoadedMetadata={() => {
            if (audioRef.current) {
              setDuration(audioRef.current.duration)
              setIsLoading(false)
            }
          }}
          onTimeUpdate={() =>
            setCurrentTime(audioRef.current?.currentTime || 0)
          }
          onPlay={() => setIsPlaying(true)}
          onPause={() => setIsPlaying(false)}
          onEnded={() => setIsPlaying(false)}
          onError={() => {
            setError('音频加载失败，请检查链接是否有效')
            setIsLoading(false)
          }}
          preload="metadata"
        />

        {title && (
          <div className="text-sm font-medium text-foreground mb-3 truncate">
            {title}
          </div>
        )}

        <div className="flex items-center gap-3">
          <button
            onClick={togglePlay}
            disabled={isLoading}
            className="h-10 w-10 rounded-full bg-primary text-primary-foreground flex items-center justify-center hover:bg-primary/90 transition-colors disabled:opacity-50"
            aria-label={isPlaying ? '暂停' : '播放'}
          >
            {isPlaying ? (
              <Pause className="h-5 w-5" />
            ) : (
              <Play className="h-5 w-5 ml-0.5" />
            )}
          </button>

          <div className="flex-1 flex items-center gap-2">
            <span className="text-xs text-muted-foreground w-10 text-right">
              {formatTime(currentTime)}
            </span>
            <div
              className="flex-1 h-2 bg-muted rounded-full cursor-pointer relative overflow-hidden"
              onClick={handleProgressClick}
            >
              <div
                className="absolute inset-y-0 left-0 bg-primary rounded-full transition-all"
                style={{
                  width: duration ? `${(currentTime / duration) * 100}%` : '0%',
                }}
              />
            </div>
            <span className="text-xs text-muted-foreground w-10">
              {formatTime(duration)}
            </span>
          </div>

          <button
            onClick={toggleMute}
            className="h-8 w-8 rounded-lg hover:bg-muted flex items-center justify-center transition-colors"
            aria-label={isMuted ? '取消静音' : '静音'}
          >
            {isMuted ? (
              <VolumeX className="h-4 w-4" />
            ) : (
              <Volume2 className="h-4 w-4" />
            )}
          </button>

          <button
            onClick={handleDownload}
            className="h-8 w-8 rounded-lg hover:bg-muted flex items-center justify-center transition-colors"
            aria-label="下载音频"
          >
            <Download className="h-4 w-4" />
          </button>
        </div>
      </div>
    )
  }
)

AudioRenderer.displayName = 'AudioRenderer'

interface VideoRendererProps {
  /** 视频 URL 或 base64 数据 */
  src: string
  /** 视频标题 */
  title?: string
  /** 封面图 */
  poster?: string
  /** 自定义类名 */
  className?: string
}

/**
 * 视频播放器组件
 */
export const VideoRenderer = memo<VideoRendererProps>(
  ({ src, title, poster, className = '' }) => {
    const [error, setError] = useState<string>('')
    const [isLoading, setIsLoading] = useState(true)
    const videoRef = useRef<HTMLVideoElement>(null)

    /** 全屏播放 */
    const handleFullscreen = () => {
      if (videoRef.current) {
        if (videoRef.current.requestFullscreen) {
          videoRef.current.requestFullscreen()
        }
      }
    }

    /** 下载视频 */
    const handleDownload = () => {
      const link = document.createElement('a')
      link.href = src
      link.download = title || 'video'
      link.click()
    }

    if (error) {
      return (
        <div
          className={`video-error my-4 p-4 bg-destructive/10 rounded-xl border border-destructive/20 ${className}`}
        >
          <div className="flex items-center gap-2 text-destructive">
            <AlertCircle className="h-4 w-4 flex-shrink-0" />
            <span className="text-sm">{error}</span>
          </div>
          <details className="mt-2 text-xs text-muted-foreground">
            <summary className="cursor-pointer hover:text-foreground">
              查看视频链接
            </summary>
            <code className="mt-1 block p-2 bg-muted rounded text-xs break-all">
              {src}
            </code>
          </details>
        </div>
      )
    }

    return (
      <div
        className={`video-player my-4 rounded-xl border border-border overflow-hidden bg-black ${className}`}
      >
        {title && (
          <div className="px-4 py-2 bg-muted/50 border-b border-border">
            <span className="text-sm font-medium text-foreground truncate">
              {title}
            </span>
          </div>
        )}

        <div className="relative">
          {isLoading && (
            <div className="absolute inset-0 flex items-center justify-center bg-muted/50 z-10">
              <span className="text-muted-foreground animate-pulse">
                加载视频中...
              </span>
            </div>
          )}

          <video
            ref={videoRef}
            src={src}
            poster={poster}
            controls
            controlsList="nodownload"
            className="w-full max-h-[500px] object-contain"
            onLoadedMetadata={() => setIsLoading(false)}
            onError={() => {
              setError('视频加载失败，请检查链接是否有效')
              setIsLoading(false)
            }}
            preload="metadata"
          >
            您的浏览器不支持视频播放
          </video>
        </div>

        <div className="px-4 py-2 bg-muted/30 border-t border-border flex items-center justify-end gap-2">
          <button
            onClick={handleFullscreen}
            className="h-8 w-8 rounded-lg hover:bg-muted flex items-center justify-center transition-colors"
            aria-label="全屏播放"
          >
            <Maximize className="h-4 w-4" />
          </button>
          <button
            onClick={handleDownload}
            className="h-8 w-8 rounded-lg hover:bg-muted flex items-center justify-center transition-colors"
            aria-label="下载视频"
          >
            <Download className="h-4 w-4" />
          </button>
        </div>
      </div>
    )
  }
)

VideoRenderer.displayName = 'VideoRenderer'

/**
 * 解析媒体代码块内容
 */
export function parseMediaContent(content: string): {
  src: string
  title?: string
  poster?: string
} {
  const trimmed = content.trim()

  if (trimmed.startsWith('{')) {
    try {
      const parsed = JSON.parse(trimmed)
      return {
        src: parsed.src || parsed.url || '',
        title: parsed.title,
        poster: parsed.poster,
      }
    } catch {
      // JSON 解析失败，当作 URL 处理
    }
  }

  return { src: trimmed }
}
