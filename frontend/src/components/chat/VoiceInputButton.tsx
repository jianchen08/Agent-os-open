/**
 * 语音输入按钮组件
 *
 * 提供语音输入的交互界面
 */

import { Loader2, Mic, MicOff } from 'lucide-react'
import { useEffect, useState } from 'react'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'
import type { VoiceInputButtonProps, VoiceInputError } from '@/types/voiceInput'

/**
 * 错误提示组件
 */
const ErrorTooltip = ({ error, visible }: { error: VoiceInputError | null; visible: boolean }) => {
  const [show, setShow] = useState(false)

  useEffect(() => {
    if (visible && error) {
      setShow(true)
      const timer = setTimeout(() => setShow(false), 3000)
      return () => clearTimeout(timer)
    }
    return () => setShow(false)
  }, [visible, error])

  if (!show || !error) return null

  return (
    <div className="bg-destructive text-destructive-foreground animate-in fade-in-0 zoom-in-95 absolute bottom-full left-1/2 z-50 mb-2 -translate-x-1/2 rounded-lg px-3 py-1.5 text-xs whitespace-nowrap">
      {error.message}
      <div className="border-t-destructive absolute top-full left-1/2 -translate-x-1/2 border-4 border-transparent" />
    </div>
  )
}

/**
 * 录音状态指示器
 */
const RecordingIndicator = () => (
  <div className="pointer-events-none absolute inset-0 flex items-center justify-center">
    <span className="absolute inset-0 animate-ping rounded-full bg-status-error/50" />
    <span className="absolute inset-0 animate-pulse rounded-full bg-status-error/30" />
  </div>
)

/**
 * 语音输入按钮组件
 */
export const VoiceInputButton = ({
  disabled = false,
  state = 'idle',
  error,
  onClick,
  className,
}: VoiceInputButtonProps) => {
  const isRecording = state === 'recording'
  const isTranscribing = state === 'transcribing'

  /** 根据状态确定提示文本 */
  const getTooltip = () => {
    if (isRecording) return '点击停止录音'
    if (isTranscribing) return '正在处理...'
    if (error?.type === 'permission_denied') return '麦克风权限被拒绝'
    return '语音输入'
  }

  return (
    <div className="relative">
      <ErrorTooltip error={error || null} visible={!!error} />

      <Button
        variant="ghost"
        size="icon"
        className={cn(
          'relative h-8 w-8 overflow-hidden rounded-lg transition-all duration-200',
          isTranscribing && 'text-muted-foreground cursor-wait',
          error?.type === 'permission_denied' && 'text-destructive hover:text-destructive',
          className,
        )}
        style={isRecording ? { backgroundColor: 'rgb(239 68 68)' } : undefined}
        onMouseEnter={(e) => {
          if (isRecording) {
            e.currentTarget.style.backgroundColor = 'rgb(220 38 38)'
          }
        }}
        onMouseLeave={(e) => {
          if (isRecording) {
            e.currentTarget.style.backgroundColor = 'rgb(239 68 68)'
          }
        }}
        onClick={onClick}
        disabled={disabled || isTranscribing}
        title={getTooltip()}
        aria-label={getTooltip()}
      >
        {isRecording && <RecordingIndicator />}

        {isTranscribing ? (
          <Loader2 className="h-4 w-4 animate-spin" />
        ) : isRecording ? (
          <Mic className="relative z-10 h-4 w-4" style={{ color: 'white' }} fill="white" />
        ) : error?.type === 'permission_denied' ? (
          <MicOff className="h-4 w-4" />
        ) : (
          <Mic className="h-4 w-4" />
        )}
      </Button>
    </div>
  )
}
