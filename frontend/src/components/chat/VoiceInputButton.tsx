/**
 * 语音输入按钮组件
 *
 * 提供语音输入的交互界面，录音时呈现微信风呼吸脉冲圈动态效果。
 */

import { useEffect, useRef, useState } from 'react'
import { Loader2, Mic, MicOff } from '@/assets/icons'
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
 *
 * 桌面（鼠标）：点按开始/停止切换；移动端（触屏）：微信式按住说话——
 * 按下即录、松手即停并转写落输入框。两条路径经 pointerType 区分互不干扰
 * （触屏的 pointerup 会跟发合成 click，由 suppressClick 哨兵吞掉防二次切换）。
 */
export const VoiceInputButton = ({
  disabled = false,
  state = 'idle',
  error,
  onClick,
  onPressStart,
  onPressEnd,
  className,
}: VoiceInputButtonProps) => {
  const isRecording = state === 'recording'
  const isTranscribing = state === 'transcribing'
  /** 触屏按住模式进行中（pointerdown 已开录，等 pointerup 收） */
  const holdModeRef = useRef(false)
  /** 触屏松手后的合成 click 哨兵：吞掉防 click 路径二次切换 */
  const suppressClickRef = useRef(false)

  /** 触屏按下：开录（鼠标不走此路径，保留点按切换语义） */
  const handlePointerDown = (e: React.PointerEvent<HTMLButtonElement>) => {
    if (e.pointerType === 'mouse') return
    if (disabled || isTranscribing || isRecording) return
    e.currentTarget.setPointerCapture(e.pointerId)
    holdModeRef.current = true
    suppressClickRef.current = true
    onPressStart?.()
  }

  /** 触屏松手/被打断：停录并转写 */
  const handlePointerRelease = () => {
    if (!holdModeRef.current) return
    holdModeRef.current = false
    onPressEnd?.()
  }

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
        style={isRecording ? { backgroundColor: 'var(--status-error)' } : undefined}
        onMouseEnter={(e) => {
          if (isRecording) {
            e.currentTarget.style.backgroundColor = 'color-mix(in srgb, var(--status-error) 80%, black)'
          }
        }}
        onMouseLeave={(e) => {
          if (isRecording) {
            e.currentTarget.style.backgroundColor = 'var(--status-error)'
          }
        }}
        onPointerDown={handlePointerDown}
        onPointerUp={handlePointerRelease}
        onPointerCancel={handlePointerRelease}
        onClick={() => {
          // 触屏按住松手后的合成 click 已由按压路径处理过，吞掉防二次切换
          if (suppressClickRef.current) {
            suppressClickRef.current = false
            return
          }
          onClick?.()
        }}
        disabled={disabled || isTranscribing}
        title={getTooltip()}
        aria-label={getTooltip()}
      >
        {isRecording && <RecordingIndicator />}

        {isTranscribing ? (
          <Loader2 className="h-icon-md w-icon-md animate-spin" />
        ) : isRecording ? (
          <Mic className="relative z-10 h-icon-md w-icon-md" style={{ color: 'white' }} fill="white" />
        ) : error?.type === 'permission_denied' ? (
          <MicOff className="h-icon-md w-icon-md" />
        ) : (
          <Mic className="h-icon-md w-icon-md" />
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
