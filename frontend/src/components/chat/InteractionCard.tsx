/**
 * InteractionCard 组件
 *
 * 纯展示组件：渲染人类交互请求的卡片。
 * 支持 choice（选项）和 conversation（对话）两种模式。
 * 零 store/service 依赖，完全由 props 驱动。
 */

import { ArrowRight, Check, Loader2, MessageSquare, X } from 'lucide-react'
import { useState } from 'react'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { MarkdownRenderer } from './markdown/MarkdownRenderer'
import type { InteractionOption, PendingInteraction } from '@/stores/interactionStore'

/** description 长度阈值：超过此值弹窗展示，否则直接执行选择 */
const DESCRIPTION_DIALOG_THRESHOLD = 20

export interface InteractionCardProps {
  interaction: PendingInteraction
  onRespondChoice: (optionId: string) => void
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
  /** 当前选中的待确认选项（弹窗打开时非 null） */
  const [selectedOption, setSelectedOption] = useState<InteractionOption | null>(null)
  const isDone = interaction.status !== 'pending'

  /**
   * 文件审阅去重：当 interaction 携带 fileContents 时，
   * 说明已有专属的 FileReviewTab（含审批按钮），避免在 InteractionCard 中重复渲染选项按钮。
   */
  const hasFileReviewTab = !!(
    interaction.fileContents && Object.keys(interaction.fileContents).length > 0
  )

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

  /**
   * 判断是否需要弹窗展示 description。
   * description 存在且长度 >= 阈值时弹窗，否则直接执行选择。
   */
  const shouldShowDialog = (opt: InteractionOption): boolean =>
    !!opt.description && opt.description.length >= DESCRIPTION_DIALOG_THRESHOLD

  /** 点击选项：有长 description 弹窗，否则直接选择 */
  const handleOptionClick = (opt: InteractionOption) => {
    if (shouldShowDialog(opt)) {
      setSelectedOption(opt)
    } else {
      onRespondChoice(opt.id)
    }
  }

  /** 确认选择 */
  const handleConfirmOption = () => {
    if (selectedOption) {
      onRespondChoice(selectedOption.id)
      setSelectedOption(null)
    }
  }

  /** 取消选择 */
  const handleCancelOption = () => {
    setSelectedOption(null)
  }

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
          <MessageSquare className="h-4 w-4 shrink-0 text-status-info" />
          <span className="text-sm font-semibold">{interaction.title || '交互请求'}</span>
          {isDone && (
            <span className="ml-auto flex items-center gap-1 text-xs text-status-success">
              <Check className="h-3 w-3" />
              {interaction.status === 'navigated' ? '已跳转' : '已完成'}
            </span>
          )}
          {!isDone && (
            <button
              onClick={onDismiss}
              className="ml-auto rounded-sm p-0.5 text-muted-foreground opacity-0 transition-opacity hover:text-foreground group-hover:opacity-100 [.animate-pulse-subtle_&]:opacity-60"
              title="关闭"
            >
              <X className="h-3.5 w-3.5" />
            </button>
          )}
        </div>
        {interaction.description && (
          <p className="text-muted-foreground mt-1 text-sm">{interaction.description}</p>
        )}
      </div>

      {/* 内容区 */}
      <div className="px-4 py-3">
        {/* 初始消息（对话模式） */}
        {interaction.initialMessage && (
          <div className="mb-3">
            <MarkdownRenderer content={interaction.initialMessage} />
          </div>
        )}

        {/* Notification 模式：纯展示，无交互按钮 */}
        {interaction.mode === 'notification' && !isDone && (
          <div className="space-y-2">
            {interaction.initialMessage && (
              <MarkdownRenderer content={interaction.initialMessage} />
            )}
            {interaction.description && !interaction.initialMessage && (
              <p className="text-muted-foreground text-sm">{interaction.description}</p>
            )}
            {interaction.progress != null && (
              <div className="h-2 w-full rounded-full bg-muted">
                <div
                  className="h-2 rounded-full bg-status-info transition-all"
                  style={{ width: `${Math.min(100, Math.max(0, interaction.progress))}%` }}
                />
              </div>
            )}
          </div>
        )}

        {/* Choice 模式：选项按钮 + 自定义输入 */}
        {interaction.mode === 'choice' && interaction.options && interaction.options.length > 0 && !isDone && !hasFileReviewTab && (
          <div className="space-y-3">
            <div className="flex flex-wrap gap-2">
              {interaction.options.map((opt) => (
                <Button
                  key={opt.id}
                  variant="outline"
                  size="sm"
                  disabled={isSubmitting}
                  onClick={() => handleOptionClick(opt)}
                  className="text-sm"
                >
                  {opt.label}
                </Button>
              ))}
            </div>

            {/* 选项描述确认弹窗 */}
            <Dialog open={selectedOption !== null} onOpenChange={(open: boolean) => { if (!open) handleCancelOption() }}>
              <DialogContent>
                <DialogHeader>
                  <DialogTitle>{selectedOption?.label}</DialogTitle>
                  <DialogDescription>请确认您的选择</DialogDescription>
                </DialogHeader>
                <div
                  data-testid="dialog-scroll-area"
                  className="max-h-[60vh] overflow-y-auto px-6 py-2"
                >
                  {selectedOption?.description && (
                    <MarkdownRenderer content={selectedOption.description} />
                  )}
                </div>
                <DialogFooter>
                  <Button variant="outline" onClick={handleCancelOption}>
                    取消
                  </Button>
                  <Button onClick={handleConfirmOption}>
                    确认选择
                  </Button>
                </DialogFooter>
              </DialogContent>
            </Dialog>
            <div className="flex gap-2">
              <textarea
                value={textInput}
                onChange={(e) => setTextInput(e.target.value)}
                onKeyDown={handleKeyDown}
                disabled={isSubmitting}
                placeholder="或输入自定义回复..."
                rows={1}
                className="border-border bg-background flex-1 resize-none rounded-lg border px-3 py-2 text-sm outline-none transition-shadow focus:ring-1 focus:ring-status-info"
              />
              <Button
                size="sm"
                disabled={isSubmitting || !textInput.trim()}
                onClick={handleTextSubmit}
              >
                {isSubmitting ? (
                  <Loader2 className="h-4 w-4 animate-spin" />
                ) : (
                  '发送'
                )}
              </Button>
            </div>
          </div>
        )}

        {/* Conversation 模式：快捷回复 + 跳转 + 输入 */}
        {interaction.mode === 'conversation' && !isDone && (
          <div className="space-y-3">
            {/* 快捷回复芯片 */}
            {interaction.suggestions && interaction.suggestions.length > 0 && (
              <div className="flex flex-wrap gap-2">
                {interaction.suggestions.map((suggestion, i) => (
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

            {/* 跳转到对话标签页 */}
            <div>
              <Button
                variant="ghost"
                size="sm"
                disabled={isSubmitting}
                onClick={onNavigateToTab}
                className="text-sm text-status-info hover:text-status-info/80"
              >
                <ArrowRight className="mr-1 h-3.5 w-3.5" />
                进入对话
              </Button>
            </div>

            {/* 自定义文本输入 */}
            <div className="flex gap-2">
              <textarea
                value={textInput}
                onChange={(e) => setTextInput(e.target.value)}
                onKeyDown={handleKeyDown}
                disabled={isSubmitting}
                placeholder="输入回复..."
                rows={1}
                className="border-border bg-background flex-1 resize-none rounded-lg border px-3 py-2 text-sm outline-none transition-shadow focus:ring-1 focus:ring-status-info"
              />
              <Button
                size="sm"
                disabled={isSubmitting || !textInput.trim()}
                onClick={handleTextSubmit}
              >
                {isSubmitting ? (
                  <Loader2 className="h-4 w-4 animate-spin" />
                ) : (
                  '发送'
                )}
              </Button>
            </div>
          </div>
        )}

        {/* Choice 模式无选项时也提供文本输入 */}
        {interaction.mode === 'choice' && (!interaction.options || interaction.options.length === 0) && !isDone && (
          <div className="flex gap-2">
            <textarea
              value={textInput}
              onChange={(e) => setTextInput(e.target.value)}
              onKeyDown={handleKeyDown}
              disabled={isSubmitting}
              placeholder="输入回复..."
              rows={1}
              className="border-border bg-background flex-1 resize-none rounded-lg border px-3 py-2 text-sm outline-none transition-shadow focus:ring-1 focus:ring-status-info"
            />
            <Button
              size="sm"
              disabled={isSubmitting || !textInput.trim()}
              onClick={handleTextSubmit}
            >
              {isSubmitting ? (
                <Loader2 className="h-4 w-4 animate-spin" />
              ) : (
                '发送'
              )}
            </Button>
          </div>
        )}
      </div>
    </div>
  )
}
