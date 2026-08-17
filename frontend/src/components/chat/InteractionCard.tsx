/**
 * InteractionCard 组件
 *
 * 通用交互卡片（widget 化 T9）：布局由交互模式声明驱动
 * （utils/interactionModes——human_interaction_tool 插件 ui.interaction_modes
 * 声明覆盖，内置三模式默认件兜底，未知模式通用兜底+数据形状增强），
 * 本组件不再按 mode 硬编码渲染分支。
 * 零 store/service 依赖，完全由 props 驱动。
 */

import { ArrowRight, Check, Loader2, MessageSquare, X } from '@/assets/icons'
import { useState } from 'react'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { MarkdownRenderer } from './markdown/MarkdownRenderer'
import type { InteractionOption, PendingInteraction } from '@/stores/interactionStore'
import { resolveInteractionLayout } from '@/utils/interactionModes'

export interface InteractionCardProps {
  interaction: PendingInteraction
  onRespondChoice: (optionId: string, optionLabel?: string) => void
  onRespondText: (text: string) => void
  onNavigateToTab: () => void
  onDismiss: () => void
  isSubmitting: boolean
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
          {isDone && (
            <span className="ml-auto flex items-center gap-1 text-xs text-status-success">
              <Check className="h-icon-xs w-icon-xs" />
              {interaction.status === 'navigated' ? '已跳转' : '已完成'}
            </span>
          )}
          {!isDone && (
            <button
              onClick={onDismiss}
              className="ml-auto rounded-sm p-0.5 text-muted-foreground opacity-100 md:opacity-0 md:group-hover:opacity-100 transition-opacity hover:text-foreground [.animate-pulse-subtle_&]:opacity-60"
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

        {/* 选项按钮组（options 特性）：点选即回调；长描述（options_detail）走详情弹窗 */}
        {hasOptions && !isDone && (
          <div className="flex flex-wrap gap-2">
            {interaction.options!.map((opt, i) => (
              <Button
                key={opt.id ?? opt.label ?? i}
                variant="outline"
                size="sm"
                disabled={isSubmitting}
                onClick={() => {
                  // AC-1.2-3: 短 description（<20字符）直接执行选择；长描述（>=20字符）弹窗展示详情
                  if (features.has('options_detail') && opt.description && opt.description.length >= 20) {
                    setDetailOption(opt)
                  } else {
                    // 后端 options 可能缺 id（LLM 传参差异）——label 兜底；父层已绑定 requestId
                    onRespondChoice(opt.id ?? opt.label ?? String(i))
                  }
                }}
                className="text-sm"
              >
                <span className="flex flex-col items-start gap-0.5">
                  <span>{opt.label}</span>
                  {opt.description && (
                    <span className="text-xs text-muted-foreground line-clamp-1 text-left">
                      {opt.description}
                    </span>
                  )}
                </span>
              </Button>
            ))}
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
              disabled={isSubmitting}
              onClick={() => {
                if (detailOption) {
                  onRespondChoice(detailOption.id ?? detailOption.label ?? '')
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
