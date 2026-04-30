/**
 * InteractionCard 组件
 *
 * 纯展示组件：渲染人类交互请求的卡片。
 * 支持 choice（选项）和 conversation（对话）两种模式。
 * 零 store/service 依赖，完全由 props 驱动。
 */

import { useState } from 'react'
import { ArrowRight, Check, Loader2, MessageSquare } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { MarkdownRenderer } from './markdown/MarkdownRenderer'
import type { PendingInteraction } from '@/stores/interactionStore'

export interface InteractionCardProps {
  interaction: PendingInteraction
  onRespondChoice: (optionId: string) => void
  onRespondText: (text: string) => void
  onNavigateToTab: () => void
  isSubmitting: boolean
}

export function InteractionCard({
  interaction,
  onRespondChoice,
  onRespondText,
  onNavigateToTab,
  isSubmitting,
}: InteractionCardProps) {
  const [textInput, setTextInput] = useState('')
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
      className={`mx-4 my-3 rounded-xl border transition-colors ${
        isDone
          ? 'border-border/50 bg-muted/30'
          : 'border-blue-500/40 bg-blue-500/5 animate-pulse-subtle shadow-md shadow-blue-500/10'
      }`}
    >
      {/* 标题区 */}
      <div className="border-b border-inherit px-4 py-3">
        <div className="flex items-center gap-2">
          <MessageSquare className="h-4 w-4 text-blue-500" />
          <span className="text-sm font-semibold">{interaction.title || '交互请求'}</span>
          {isDone && (
            <span className="ml-auto flex items-center gap-1 text-xs text-green-600">
              <Check className="h-3 w-3" />
              {interaction.status === 'navigated' ? '已跳转' : '已完成'}
            </span>
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

        {/* Choice 模式：选项按钮 */}
        {interaction.mode === 'choice' && interaction.options && interaction.options.length > 0 && !isDone && (
          <div className="flex flex-wrap gap-2">
            {interaction.options.map((opt) => (
              <Button
                key={opt.id}
                variant="outline"
                size="sm"
                disabled={isSubmitting}
                onClick={() => onRespondChoice(opt.id)}
                className="text-sm"
              >
                {opt.label}
              </Button>
            ))}
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
                className="text-sm text-blue-500 hover:text-blue-600"
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
                className="bg-background border-border flex-1 resize-none rounded-lg border px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-blue-500"
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
              className="bg-background border-border flex-1 resize-none rounded-lg border px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-blue-500"
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
