/**
 * 录音状态条 —— 输入容器内顶部的一条录音/转写指示（与附件预览区结构对称，不改变
 * 与上方消息列表的距离）：左 = 状态点 + 阶段文案，右 = 录音时长 + 取消按钮。
 *
 * 根节点显式涂输入面皮肤底（与 ChatInput 容器同值）：组件独立成文件后祖先背景
 * 不再可见于对比度静态扫描，缺这一层会让文字按页面底色判对（pixel-art 实测
 * 2.96 < 3.0）；同色叠涂零视觉差。
 */

export interface ComposerRecordingBannerProps {
  /** 录音中 */
  isRecording: boolean
  /** 转写中（录音已停止，等待识别结果） */
  isTranscribing?: boolean
  /** 长按上滑进入「松开取消」态 */
  isCancelling?: boolean
  /** 已录秒数 */
  seconds: number
  /** 取消回调（录音/转写中丢弃本次语音）；缺省不渲染取消按钮 */
  onCancel?: () => void
}

/** 格式化录音时长为 mm:ss */
const formatDuration = (seconds: number): string =>
  `${String(Math.floor(seconds / 60)).padStart(2, '0')}:${String(seconds % 60).padStart(2, '0')}`

/** 状态 → 文案：取消态优先（长按上滑后松手即丢弃） */
function statusLabel(isCancelling: boolean, isTranscribing: boolean): string {
  if (isCancelling) return '松开取消'
  if (isTranscribing) return '识别中…'
  return '正在聆听'
}

export function ComposerRecordingBanner({
  isRecording,
  isTranscribing = false,
  isCancelling = false,
  seconds,
  onCancel,
}: ComposerRecordingBannerProps) {
  if (!isRecording && !isTranscribing) return null

  /** 转写中且未取消 = 已停录待结果：状态点转静态（红点脉冲只表示正在采集） */
  const showPulse = !isTranscribing || isCancelling

  return (
    <div
      className="bg-[var(--chat-input-bg,var(--hover-overlay))] flex items-center justify-between gap-2 px-3 pt-3 pb-1"
      data-testid="composer-recording-banner"
    >
      <div className="flex min-w-0 items-center gap-2">
        <span
          className={
            showPulse
              ? 'bg-status-error inline-block h-2 w-2 rounded-full'
              : 'bg-muted-foreground inline-block h-2 w-2 rounded-full'
          }
          style={showPulse ? { animation: 'voice-pulse-core 1.2s ease-in-out infinite' } : undefined}
        />
        <span className="text-muted-foreground text-xs font-medium">
          {statusLabel(isCancelling, isTranscribing)}
        </span>
      </div>
      <div className="flex shrink-0 items-center gap-2">
        <span className="text-muted-foreground/70 font-mono text-xs tabular-nums">
          {formatDuration(seconds)}
        </span>
        {onCancel && (
          <button
            type="button"
            onClick={onCancel}
            className="text-muted-foreground hover:text-foreground hover:bg-[var(--hover-overlay)] rounded px-1.5 py-0.5 text-xs font-medium transition-colors"
            aria-label="取消语音输入"
          >
            取消
          </button>
        )}
      </div>
    </div>
  )
}
