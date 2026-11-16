/** 消息内联编辑器（Ctrl+Enter 保存 / Esc 取消；自动聚焦并贴合内容高度）。
 * 从 MessageItem 抽出：巨型文件只许缩小（frontend-any-baseline 大文件冻结）。 */
import { useEffect, useRef, useState } from 'react'
import { Check, Loader2 } from '@/assets/icons'
import { Button } from '@/components/ui/button'

interface MessageEditorProps {
  content: string
  onSave: (newContent: string) => void
  onCancel: () => void
  disabled?: boolean
}

export const MessageEditor = ({ content, onSave, onCancel, disabled = false }: MessageEditorProps) => {
  const [value, setValue] = useState(content)
  const textareaRef = useRef<HTMLTextAreaElement>(null)

  useEffect(() => {
    if (textareaRef.current) {
      textareaRef.current.focus()
      textareaRef.current.setSelectionRange(
        textareaRef.current.value.length,
        textareaRef.current.value.length,
      )
      textareaRef.current.style.height = 'auto'
      textareaRef.current.style.height = `${Math.max(100, textareaRef.current.scrollHeight)}px`
    }
  }, [])

  const handleChange = (e: React.ChangeEvent<HTMLTextAreaElement>) => {
    setValue(e.target.value)
    e.target.style.height = 'auto'
    e.target.style.height = `${Math.max(100, e.target.scrollHeight)}px`
  }

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault()
    if (value.trim() && value !== content) {
      onSave(value)
    } else if (value === content) {
      onCancel()
    }
  }

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
      e.preventDefault()
      handleSubmit(e)
    } else if (e.key === 'Escape') {
      onCancel()
    }
  }

  return (
    <form onSubmit={handleSubmit} className="flex w-full flex-col gap-2">
      <textarea
        ref={textareaRef}
        value={value}
        onChange={handleChange}
        onKeyDown={handleKeyDown}
        disabled={disabled}
        /* 输入面恒 ≥16px（text-base，M8 不变量）：更小字号触发 iOS Safari 聚焦自动放大 */
        className="border-input bg-background text-foreground focus:ring-ring min-h-[100px] w-full resize-none rounded-md border p-3 text-base focus:ring-2 focus:outline-none"
        placeholder="编辑消息内容..."
      />
      <div className="flex items-center justify-between">
        <span className="text-muted-foreground text-xs">Ctrl+Enter 保存，Esc 取消</span>
        <div className="flex gap-2">
          <Button type="button" variant="ghost" size="sm" onClick={onCancel} disabled={disabled}>
            取消
          </Button>
          <Button type="submit" size="sm" disabled={disabled || !value.trim()}>
            {disabled ? (
              <>
                <Loader2 className="mr-1 h-icon-md w-icon-md animate-spin" />
                保存中...
              </>
            ) : (
              <>
                <Check className="mr-1 h-icon-md w-icon-md" />
                保存
              </>
            )}
          </Button>
        </div>
      </div>
    </form>
  )
}
