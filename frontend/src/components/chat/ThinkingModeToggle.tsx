/**
 * 思考强度选择器组件
 *
 * 提供四档思考强度选择：关闭 / 低 / 中 / 高。
 * 强度随消息传给后端（thinking_strength），由 llm_core 路由到具体模型参数
 * （temperature / max_tokens / reasoning_effort，见 STRENGTH_TO_PARAMS）。
 */

import { AlertCircle, Brain, Check, ChevronDown, Loader2 } from '@/assets/icons'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { cn } from '@/lib/utils'
import {
  STRENGTH_TO_ENABLE,
  THINKING_STRENGTH_OPTIONS,
  type ThinkingStrength,
} from '@/types/thinkingMode'

export interface ThinkingModeToggleProps {
  /** 当前模型名称 */
  currentModel: string
  /** 当前思考强度 */
  strength: ThinkingStrength
  /** 切换强度回调（调用方负责：本地记忆 + 后端覆盖） */
  onStrengthChange: (strength: ThinkingStrength) => void
  /** 是否禁用 */
  disabled?: boolean
  /** 自定义样式类名 */
  className?: string
}

/** 当前档显示文案 */
function strengthLabel(strength: ThinkingStrength): string {
  if (strength === 'off') return '普通模式'
  return `思考·${THINKING_STRENGTH_OPTIONS.find((o) => o.value === strength)?.label ?? '中'}`
}

/**
 * 思考强度选择器（四档：关闭/低/中/高）
 */
export const ThinkingModeToggle = ({
  currentModel,
  strength,
  onStrengthChange,
  disabled = false,
  className = '',
}: ThinkingModeToggleProps) => {
  const isInvalidModel = !currentModel || currentModel === 'unknown'
  const isOff = strength === 'off'
  const effectiveDisabled = disabled || isInvalidModel

  /** 提示文本 */
  const getTitle = () => {
    if (isInvalidModel) {
      return '当前模型无效，请先选择一个有效的模型'
    }
    const label = strengthLabel(strength)
    return isOff
      ? `点击选择思考强度（当前：${label}）`
      : `思考强度：${label}（${STRENGTH_TO_ENABLE[strength] ? '已启用' : '已关闭'}）`
  }

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <button
          type="button"
          id="thinking-mode-toggle"
          name="thinking-mode-toggle"
          className={cn(
            'flex h-8 items-center gap-1.5 rounded-lg px-2.5 text-xs font-medium transition-all duration-200',
            'hover:shadow-sm',
            isOff
              ? 'text-muted-foreground hover:bg-muted hover:text-foreground border border-muted/40 bg-transparent'
              : 'bg-primary text-primary-foreground hover:bg-primary/90',
            effectiveDisabled && 'cursor-not-allowed opacity-50',
            className,
          )}
          disabled={effectiveDisabled}
          title={getTitle()}
          aria-label={getTitle()}
          data-testid="thinking-strength-trigger"
        >
          {isInvalidModel ? (
            <AlertCircle className="h-icon-md w-icon-md" />
          ) : (
            <Brain className={cn('h-icon-md w-icon-md', isOff ? 'opacity-70' : '')} />
          )}
          <span>{isInvalidModel ? '模型无效' : strengthLabel(strength)}</span>
          <ChevronDown className="h-icon-xs w-icon-xs opacity-70" />
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start" sideOffset={6} className="w-44">
        <DropdownMenuLabel className="text-[11px]">思考强度</DropdownMenuLabel>
        <DropdownMenuSeparator />
        {THINKING_STRENGTH_OPTIONS.map((option) => (
          <DropdownMenuItem
            key={option.value}
            onClick={() => onStrengthChange(option.value)}
            className={cn(
              'flex items-center justify-between gap-2',
              option.value === strength && 'text-primary',
            )}
          >
            <span className="flex min-w-0 flex-col">
              <span className="text-[13px] font-medium">{option.label}</span>
              <span className="text-muted-foreground text-[11px]">{option.description}</span>
            </span>
            {option.value === strength && <Check className="h-icon-sm w-icon-sm shrink-0" />}
          </DropdownMenuItem>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
