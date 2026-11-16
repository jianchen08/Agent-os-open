/**
 * 录音状态条 —— 输入容器内顶部的一条录音指示（与附件预览区结构对称，不改变与
 * 上方消息列表的距离）：左 = 脉冲点 + 识别阶段文案，右 = 录音时长。
 *
 * 根节点显式涂输入面皮肤底（与 ChatInput 容器同值）：组件独立成文件后祖先背景
 * 不再可见于对比度静态扫描，缺这一层会让文字按页面底色判对（pixel-art 实测
 * 2.96 < 3.0）；同色叠涂零视觉差。
 */

export interface ComposerRecordingBannerProps {
  /** 录音中；false 时不渲染（零占位） */
  isRecording: boolean
  /** 识别通道：server-asr = 服务端转写，其余按本地流式展示 */
  mode: string
  /** 已录秒数 */
  seconds: number
}

/** 格式化录音时长为 mm:ss */
const formatDuration = (seconds: number): string =>
  `${String(Math.floor(seconds / 60)).padStart(2, '0')}:${String(seconds % 60).padStart(2, '0')}`

export function ComposerRecordingBanner({
  isRecording,
  mode,
  seconds,
}: ComposerRecordingBannerProps) {
  if (!isRecording) return null
  return (
    <div className="bg-[var(--chat-input-bg,var(--hover-overlay))] flex items-center justify-between gap-2 px-3 pt-3 pb-1">
      <div className="flex items-center gap-2">
        <span
          className="bg-status-error inline-block h-2 w-2 rounded-full"
          style={{ animation: 'voice-pulse-core 1.2s ease-in-out infinite' }}
        />
        <span className="text-muted-foreground text-xs font-medium">
          {mode === 'server-asr' ? '服务器识别中…' : '正在聆听…'}
        </span>
      </div>
      <span className="text-muted-foreground/70 font-mono text-xs tabular-nums">
        {formatDuration(seconds)}
      </span>
    </div>
  )
}
