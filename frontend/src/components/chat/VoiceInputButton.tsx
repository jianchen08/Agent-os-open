/**
 * 语音输入按钮组件
 *
 * 提供语音输入的交互界面，录音时呈现微信风呼吸脉冲圈动态效果。
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
 * 录音状态指示器 —— 微信风多层呼吸脉冲圈
 *
 * 三层错峰扩散的圆环，配合中心脉动光晕，形成连续的"呼吸"节律。
 * 圆环尺寸严格控制在按钮边界附近，不侵入相邻按钮。
 */
const RecordingIndicator = () => (
  <div className="pointer-events-none absolute inset-0 flex items-center justify-center overflow-visible">
    {/* 外层脉冲圈 */}
    <span
      className="absolute h-8 w-8 rounded-full bg-status-error/40"
      style={{
        animation: 'voice-pulse-ring 1.8s cubic-bezier(0.4, 0, 0.6, 1) infinite',
      }}
    />
    {/* 中层脉冲圈 */}
    <span
      className="absolute h-8 w-8 rounded-full bg-status-error/50"
      style={{
        animation: 'voice-pulse-ring 1.8s cubic-bezier(0.4, 0, 0.6, 1) infinite',
        animationDelay: '0.6s',
      }}
    />
    {/* 内层呼吸光晕 */}
    <span
      className="absolute inset-0 rounded-full bg-status-error/30"
      style={{
        animation: 'voice-pulse-core 1.2s ease-in-out infinite',
      }}
    />
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
          'relative h-8 w-8 overflow-visible rounded-lg transition-all duration-200',
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

      {/* 脉冲动画 keyframes（注入一次） */}
      <style>{`
        @keyframes voice-pulse-ring {
          0% {
            transform: scale(0.8);
            opacity: 0.7;
          }
          80%, 100% {
            transform: scale(1.8);
            opacity: 0;
          }
        }
        @keyframes voice-pulse-core {
          0%, 100% {
            transform: scale(1);
            opacity: 0.4;
          }
          50% {
            transform: scale(1.15);
            opacity: 0.7;
          }
        }
      `}</style>
    </div>
  )
}
