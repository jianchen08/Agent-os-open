/** 输入区错误提示（上传失败/发送未受理/发送判死/语音转写失败）：落在操作点、
 *  可手动关闭，下一次受理发送自动清除。
 *
 *  P1-6 折叠（MuMu 实测 shots/07 整屏红字墙）：折叠态只留一行摘要短语
 *  （errorAlertSummary.summarizeErrorMessage）+「详情」展开；展开态给原始
 *  全文（break-words 可滚动）+「复制」按钮（既有 toast 面反馈）。 */

import { useState } from 'react'
import { toast } from 'sonner'
import { AlertCircle, X } from '@/assets/icons'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'
import { summarizeErrorMessage } from './errorAlertSummary'

interface ChatInputErrorAlertProps {
  /** 错误文案（ChatInput inputError state） */
  message: string
  /** 关闭提示（ChatInput 置空 inputError） */
  onClose: () => void
}

export function ChatInputErrorAlert({ message, onClose }: ChatInputErrorAlertProps) {
  /** 详情展开态（默认折叠：一行摘要 + 展开入口） */
  const [expanded, setExpanded] = useState(false)

  const handleCopy = () => {
    navigator.clipboard
      .writeText(message)
      .then(() => toast.success('已复制到剪贴板'))
      .catch(() => toast.error('复制失败'))
  }

  return (
    <div
      id="input-error"
      role="alert"
      className="text-destructive bg-destructive/10 mb-3 flex items-center gap-2 rounded-xl p-2 text-sm"
    >
      <AlertCircle className="h-icon-md w-icon-md flex-shrink-0" />
      {expanded ? (
        <span
          className="max-h-40 min-w-0 flex-1 overflow-y-auto break-words whitespace-pre-wrap"
          data-testid="input-error-full"
        >
          {message}
        </span>
      ) : (
        <span className="min-w-0 flex-1 truncate" data-testid="input-error-summary">
          {summarizeErrorMessage(message)}
        </span>
      )}
      {expanded && (
        <Button
          variant="ghost"
          size="sm"
          className="mt-1 h-11 shrink-0 self-start px-2 text-destructive sm:h-7"
          onClick={handleCopy}
          data-testid="input-error-copy"
          aria-label="复制错误全文"
        >
          复制
        </Button>
      )}
      <Button
        variant="ghost"
        size="sm"
        className={cn(
          'h-11 shrink-0 px-2 text-destructive sm:h-7',
          expanded && 'mt-1 self-start',
        )}
        onClick={() => setExpanded((v) => !v)}
        data-testid={expanded ? 'input-error-collapse' : 'input-error-expand'}
        aria-label={expanded ? '收起错误详情' : '展开错误详情'}
      >
        {expanded ? '收起' : '详情'}
      </Button>
      {!expanded && (
        <Button
          variant="ghost"
          size="sm"
          className="h-11 w-11 shrink-0 rounded-lg p-0 sm:h-6 sm:w-6"
          onClick={onClose}
          aria-label="关闭错误提示"
        >
          <X className="h-icon-md w-icon-md" />
        </Button>
      )}
    </div>
  )
}
