/**
 * InteractionCard 组件
 *
 * 通用交互卡片（widget 化 T9）：布局由交互模式声明驱动
 * （utils/interactionModes——human_interaction_tool 插件 ui.interaction_modes
 * 声明覆盖，内置三模式默认件兜底，未知模式通用兜底+数据形状增强），
 * 本组件不再按 mode 硬编码渲染分支。
 * 零 store/service 依赖，完全由 props 驱动。
 */

import { useEffect, useMemo, useState } from 'react'
import { ArrowRight, Check, Loader2, MessageSquare, X } from '@/assets/icons'
import { MarkdownRenderer } from '@/components/shared/markdown/MarkdownRenderer'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { resolveInteractionLayout } from '@/utils/interactionModes'
import type { InteractionOption, PendingInteraction } from '@/stores/interactionStore'

export interface InteractionCardProps {
  interaction: PendingInteraction
  onRespondChoice: (optionId: string, optionLabel?: string) => void
  onRespondText: (text: string) => void
  onNavigateToTab: () => void
  onDismiss: () => void
  isSubmitting: boolean
}

/** 剩余时间格式：<1h 为 m:ss；≥1h 为 h:mm:ss（BUG-40 24h 等待上限可读展示） */
function formatRemaining(totalSeconds: number): string {
  const h = Math.floor(totalSeconds / 3600)
  const m = Math.floor((totalSeconds % 3600) / 60)
  const s = totalSeconds % 60
  if (h > 0) return `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`
  return `${m}:${String(s).padStart(2, '0')}`
}

/**
 * 审批等待倒计时（BUG-14 有界等待可见化）。
 * timeoutSeconds + createdAt 齐备且请求仍 pending 时启用，每秒刷新；
 * 缺任一字段（非审批交互/旧数据）返回 null——不显示倒计时。
 */
function useApprovalCountdown(interaction: PendingInteraction): number | null {
  const { timeoutSeconds, createdAt, timestamp, status } = interaction
  const active = status === 'pending' && !!timeoutSeconds && timeoutSeconds > 0

  const deadlineMs = useMemo(() => {
    const base = Date.parse(createdAt || timestamp || '')
    if (!Number.isFinite(base)) return null
    return base + (timeoutSeconds ?? 0) * 1000
  }, [createdAt, timestamp, timeoutSeconds])

  const [now, setNow] = useState(() => Date.now())

  useEffect(() => {
    if (!active || deadlineMs == null) return
    setNow(Date.now())
    const timer = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(timer)
  }, [active, deadlineMs])

  if (!active || deadlineMs == null) return null
  return Math.max(0, Math.ceil((deadlineMs - now) / 1000))
}

export function InteractionCard({
  interaction,
  onRespondChoice,
  onRespondText,
  onNavigateToTab,
  onDismiss,
  isSubmitting,
}: InteractionCardProps) {
  const [textInput, setTextInput] = useState('')
  const [detailOption, setDetailOption] = useState<InteractionOption | null>(null)
  const isDone = interaction.status !== 'pending'
  const { features, textInputPlaceholder } = resolveInteractionLayout(interaction)
  const secondsLeft = useApprovalCountdown(interaction)

  const handleTextSubmit = () => {
    const trimmed = textInput.trim()
    if (!trimmed) return
    onRespondText(trimmed)
    setTextInput('')
  }

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      handleTextSubmit()
    }
  }

  const hasOptions = features.has('options') && !!interaction.options?.length
  const hasSuggestions =
    features.has('suggestions') && !hasOptions && !!interaction.suggestions?.length

  return (
    <div
      className={`group mx-4 my-3 rounded-xl border transition-colors ${
        isDone
          ? 'border-border/50 bg-muted/30'
          : 'border-[var(--badge-info-text)]/40 bg-[var(--badge-info-bg)] animate-pulse-subtle shadow-md shadow-[var(--badge-info-bg)]'
      }`}
    >
      {/* 标题区 */}
      <div className="border-b border-border/30 px-4 py-3">
        <div className="flex items-center gap-2">
          <MessageSquare className="h-icon-md w-icon-md shrink-0 text-status-info" />
          <span className="text-sm font-semibold">{interaction.title || '交互请求'}</span>
          {secondsLeft != null && (
            <span
              data-testid="approval-countdown"
              className={`ml-auto shrink-0 rounded px-1.5 py-0.5 text-xs tabular-nums ${
                secondsLeft <= 0
                  ? 'bg-[var(--badge-danger-bg)] text-[var(--badge-danger-text)]'
                  : 'bg-muted text-muted-foreground'
              }`}
              title="审批等待超时后将自动按拒绝裁决"
            >
              {secondsLeft <= 0 ? '已超时，将自动拒绝' : `剩余 ${formatRemaining(secondsLeft)}`}
            </span>
          )}
          {isDone && (
            <span className="ml-auto flex items-center gap-1 text-xs text-status-success">
              <Check className="h-icon-xs w-icon-xs" />
              {interaction.status === 'navigated' ? '已跳转' : '已完成'}
            </span>
          )}
          {!isDone && (
            <button
              onClick={onDismiss}
              className={`rounded-sm p-0.5 text-muted-foreground opacity-100 md:opacity-0 md:group-hover:opacity-100 transition-opacity hover:text-foreground [.animate-pulse-subtle_&]:opacity-60 ${
                secondsLeft != null ? '' : 'ml-auto'
              }`}
              title="关闭"
            >
              <X className="h-icon-sm w-icon-sm" />
            </button>
          )}
        </div>
        {interaction.description && (
          <p className="text-muted-foreground mt-1 text-sm">{interaction.description}</p>
        )}
      </div>

      {/* 内容区 */}
      <div className="px-4 py-3">
        {/* 消息区（message 特性）：initialMessage 优先 markdown，缺省展示描述文本 */}
        {features.has('message') && !isDone && (
          <div className="mb-3 max-h-[50vh] space-y-2 overflow-y-auto overscroll-contain rounded">
            {interaction.initialMessage && <MarkdownRenderer content={interaction.initialMessage} />}
            {!interaction.initialMessage && interaction.description && (
              <p className="text-muted-foreground text-sm">{interaction.description}</p>
            )}
          </div>
        )}
        {/* 对话初始消息（非 message 特性但有 initialMessage 载荷，如 conversation） */}
        {!features.has('message') && interaction.initialMessage && (
          <div className="mb-3 max-h-[40vh] overflow-y-auto overscroll-contain rounded">
            <MarkdownRenderer content={interaction.initialMessage} />
          </div>
        )}

        {/* 进度条（progress 特性） */}
        {features.has('progress') && interaction.progress != null && !isDone && (
          <div className="h-2 w-full rounded-full bg-muted">
            <div
              className="h-2 rounded-full bg-status-info transition-all"
              style={{ width: `${Math.min(100, Math.max(0, interaction.progress))}%` }}
            />
          </div>
        )}

        {/* 选项按钮组（options 特性）：点选即回调；长描述（options_detail）走详情弹窗。
            BUG-40：单个选项文本可很长——按钮解除固定高/不换行约束让文本完整换行可读，
            选项区限高纵向滚动防总高撑破卡片 */}
        {hasOptions && !isDone && (
          <div
            data-testid="options-list"
            className="flex max-h-[40vh] flex-wrap gap-2 overflow-y-auto overscroll-contain"
          >
            {interaction.options!.map((opt, i) => {
              // 后端协议要求 options 携带稳定 id；LLM 传参差异可能缺失。
              // 人工确认属核心流程，回传展示文案会因 label 重复选错项——
              // 缺 id 的选项一律禁用并提示（fail-closed），不做回退猜测。
              const hasId = typeof opt.id === 'string' && opt.id.length > 0
              return (
                <Button
                  key={hasId ? opt.id : `missing-id-${i}`}
                  variant="outline"
                  size="sm"
                  disabled={isSubmitting || !hasId}
                  title={!hasId ? '该选项缺少 id（后端契约违规），已禁用' : undefined}
                  onClick={() => {
                    // AC-1.2-3: 短 description（<20字符）直接执行选择；长描述（>=20字符）弹窗展示详情
                    if (features.has('options_detail') && opt.description && opt.description.length >= 20) {
                      setDetailOption(opt)
                    } else {
                      onRespondChoice(opt.id)
                    }
                  }}
                  className="h-auto min-h-8 whitespace-normal break-words py-1.5 text-left text-sm"
                >
                  <span className="flex flex-col items-start gap-0.5">
                    <span>{opt.label}</span>
                    {opt.description && (
                      <span className="text-xs text-muted-foreground text-left">{opt.description}</span>
                    )}
                  </span>
                </Button>
              )
            })}
          </div>
        )}

        {/* 快捷回复芯片（suggestions 特性，options 缺席时） */}
        {hasSuggestions && !isDone && (
          <div className="flex flex-wrap gap-2">
            {interaction.suggestions!.map((suggestion, i) => (
              <Button
                key={i}
                variant="outline"
                size="sm"
                disabled={isSubmitting}
                onClick={() => onRespondText(suggestion)}
                className="text-sm"
              >
                {suggestion}
              </Button>
            ))}
          </div>
        )}

        {/* 跳转到对话标签页（navigate 特性） */}
        {features.has('navigate') && !isDone && (
          <div>
            <Button
              variant="ghost"
              size="sm"
              disabled={isSubmitting}
              onClick={onNavigateToTab}
              className="text-sm text-status-info hover:text-status-info/80"
            >
              <ArrowRight className="mr-1 h-icon-sm w-icon-sm" />
              进入对话
            </Button>
          </div>
        )}

        {/* 自由文本输入（text_input 特性） */}
        {features.has('text_input') && !isDone && (
          <div className="flex gap-2">
            <textarea
              value={textInput}
              onChange={(e) => setTextInput(e.target.value)}
              onKeyDown={handleKeyDown}
              disabled={isSubmitting}
              placeholder={textInputPlaceholder}
              rows={1}
              className="border-border bg-background flex-1 resize-none rounded-lg border px-3 py-2 text-sm outline-none transition-shadow focus:ring-1 focus:ring-status-info"
            />
            <Button
              size="sm"
              disabled={isSubmitting || !textInput.trim()}
              onClick={handleTextSubmit}
            >
              {isSubmitting ? (
                <Loader2 className="h-icon-md w-icon-md animate-spin" />
              ) : (
                '发送'
              )}
            </Button>
          </div>
        )}

      </div>

      {/* 选项详情弹窗（options_detail 特性配套） */}
      <Dialog
        open={!!detailOption}
        onOpenChange={(open) => !open && setDetailOption(null)}
      >
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle>{detailOption?.label}</DialogTitle>
          </DialogHeader>
          <div data-testid="dialog-scroll-area" className="max-h-[60vh] overflow-y-auto overscroll-contain">
            {detailOption?.description && (
              <MarkdownRenderer content={detailOption.description} />
            )}
          </div>
          <DialogFooter>
            <Button
              variant="outline"
              size="sm"
              onClick={() => setDetailOption(null)}
              disabled={isSubmitting}
            >
              取消
            </Button>
            <Button
              size="sm"
              disabled={isSubmitting || !detailOption?.id}
              title={!detailOption?.id ? '该选项缺少 id（后端契约违规），已禁用' : undefined}
              onClick={() => {
                if (detailOption) {
                  onRespondChoice(detailOption.id)
                  setDetailOption(null)
                }
              }}
            >
              {isSubmitting ? (
                <Loader2 className="h-icon-md w-icon-md animate-spin" />
              ) : (
                '确认选择'
              )}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}
