/** 展开/收起编辑器按钮：固定在输入框右上角，悬浮于文本之上 */

import { Maximize2, Minimize2 } from '@/assets/icons'
import { Button } from '@/components/ui/button'

interface ComposerExpandToggleProps {
  /** 展开态（ChatInput isExpanded） */
  isExpanded: boolean
  /** 切换展开态（ChatInput setIsExpanded 取反） */
  onToggle: () => void
}

export function ComposerExpandToggle({ isExpanded, onToggle }: ComposerExpandToggleProps) {
  return (
    <Button
      variant="ghost"
      size="icon"
      className="text-muted-foreground hover:text-foreground hover:bg-muted absolute top-2 right-2 z-10 h-8 w-8 rounded-lg"
      onClick={onToggle}
      aria-label={isExpanded ? '收起编辑器' : '展开编辑器'}
      aria-expanded={isExpanded}
      title={isExpanded ? '收起 (Esc)' : '展开为大编辑器'}
      data-testid="chat-input-expand-toggle"
    >
      {isExpanded ? (
        <Minimize2 className="h-icon-md w-icon-md" />
      ) : (
        <Maximize2 className="h-icon-md w-icon-md" />
      )}
    </Button>
  )
}
