/**
 * InteractionCard 组件
 *
 * 纯展示组件：渲染人类交互请求的卡片。
 * 支持 choice（选项）、conversation（对话）和 notification（通知）三种模式。
 * 零 store/service 依赖，完全由 props 驱动。
 *
 * Choice 模式：
 * - 有选项时显示快捷按钮（点击填入输入框）+ 输入框 + 发送按钮
 * - 无选项时只显示输入框 + 发送按钮
 * - 用户必须输入内容后发送，不直接通过/驳回
 */

import { ArrowRight, Check, Loader2, MessageSquare, X } from 'lucide-react'
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
              className="ml-auto rounded-sm p-0.5 text-muted-foreground opacity-100 md:opacity-0 md:group-hover:opacity-100 transition-opacity hover:text-foreground [.animate-pulse-subtle_&]:opacity-60"
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
          <div className="mb-3 max-h-[40vh] overflow-y-auto overscroll-contain rounded">
            <MarkdownRenderer content={interaction.initialMessage} />
          </div>
        )}

        {/* Notification 模式：纯展示，无交互按钮 */}
        {interaction.mode === 'notification' && !isDone && (
          <div className="space-y-2 max-h-[50vh] overflow-y-auto overscroll-contain">
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

        {/* Choice 模式：快捷按钮（填入输入框）+ 文本输入 + 发送 */}
        {interaction.mode === 'choice' && !isDone && (
          <div className="space-y-3">
            {interaction.options && interaction.options.length > 0 && (
              <div className="flex flex-wrap gap-2">
                {interaction.options.map((opt) => (
                  <Button
                    key={opt.id}
                    variant="outline"
                    size="sm"
                    disabled={isSubmitting}
                    onClick={() => {
                      if (opt.description) {
                        setDetailOption(opt)
                      } else {
                        onRespondChoice(interaction.requestId, opt.id, opt.label)
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
            <div className="flex gap-2">
              <textarea
                value={textInput}
                onChange={(e) => setTextInput(e.target.value)}
                onKeyDown={handleKeyDown}
                disabled={isSubmitting}
                placeholder="输入回复后发送..."
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

        {/* Conversation 模式：选项/快捷回复 + 跳转 + 输入 */}
        {interaction.mode === 'conversation' && !isDone && (
          <div className="space-y-3">
            {/* 选项按钮（如果有 options） */}
            {interaction.options && interaction.options.length > 0 && (
              <div className="flex flex-wrap gap-2">
                {interaction.options.map((opt) => (
                  <Button
                    key={opt.id}
                    variant="outline"
                    size="sm"
                    disabled={isSubmitting}
                    onClick={() => onRespondChoice(opt.id, opt.label)}
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

            {/* 快捷回复芯片（如果没有 options，用 suggestions） */}
            {(!interaction.options || interaction.options.length === 0) &&
              interaction.suggestions && interaction.suggestions.length > 0 && (
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

      </div>

      {/* 选项详情弹窗 */}
      <Dialog
        open={!!detailOption}
        onOpenChange={(open) => !open && setDetailOption(null)}
      >
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle>{detailOption?.label}</DialogTitle>
          </DialogHeader>
          <div className="max-h-[60vh] overflow-y-auto overscroll-contain">
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
                  onRespondChoice(detailOption.id, detailOption.label)
                  setDetailOption(null)
                }
              }}
            >
              {isSubmitting ? (
                <Loader2 className="h-4 w-4 animate-spin" />
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
