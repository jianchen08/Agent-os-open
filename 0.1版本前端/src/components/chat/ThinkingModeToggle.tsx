/**
 * 思考模式切换按钮组件
 *
 * 提供思考模式的开启/关闭切换功能
 * 支持两种思考模式类型：参数切换型和模型切换型
 */

import { AlertCircle, Brain, Loader2 } from 'lucide-react'
import { useState } from 'react'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'
import type { ThinkingModeState } from '@/types/thinkingMode'

export interface ThinkingModeToggleProps {
  /** 当前模型名称 */
  currentModel: string
  /** 思考模式状态 */
  thinkingMode: ThinkingModeState
  /** 切换思考模式回调 */
  onToggle: (enabled: boolean) => Promise<void>
  /** 是否禁用 */
  disabled?: boolean
  /** 自定义样式类名 */
  className?: string
}

/**
 * 思考模式切换按钮组件
 */
export const ThinkingModeToggle = ({
  currentModel,
  thinkingMode,
  onToggle,
  disabled = false,
  className = '',
}: ThinkingModeToggleProps) => {
  const [isToggling, setIsToggling] = useState(false)

  /** 处理切换操作 */
  const handleToggle = async () => {
    if (disabled || isToggling || thinkingMode.switching) {
      return
    }

    setIsToggling(true)
    try {
      await onToggle(!thinkingMode.enabled)
    } catch (error) {
      console.error('思考模式切换失败:', error)
    } finally {
      setIsToggling(false)
    }
  }

  const isProcessing = isToggling || thinkingMode.switching

  /** 获取按钮状态样式 */
  const getButtonVariant = () => {
    if (thinkingMode.error) return 'destructive'
    if (thinkingMode.enabled) return 'default'
    return 'outline'
  }

  /** 获取图标颜色 */
  const getIconColor = () => {
    if (thinkingMode.error) return 'text-destructive-foreground'
    if (thinkingMode.enabled) return 'text-primary-foreground'
    return 'text-muted-foreground'
  }

  const isInvalidModel = !currentModel || currentModel === 'unknown'

  /** 获取提示文本 */
  const getTitle = () => {
    if (isInvalidModel) {
      return '当前模型无效，请先选择一个有效的模型'
    }
    if (thinkingMode.error) {
      return `错误: ${thinkingMode.error}`
    }
    if (thinkingMode.enabled) {
      return `思考模式已启用 (${currentModel})`
    }
    return `点击启用思考模式 (${currentModel})`
  }

  return (
    <Button
      type="button"
      id="thinking-mode-toggle"
      name="thinking-mode-toggle"
      variant={getButtonVariant()}
      size="sm"
      className={cn(
        'h-8 gap-2 px-3 transition-all duration-200',
        'hover:shadow-sm',
        thinkingMode.enabled && 'bg-primary hover:bg-primary/90',
        isInvalidModel && 'cursor-not-allowed opacity-50',
        className,
      )}
      onClick={handleToggle}
      disabled={disabled || isProcessing || isInvalidModel}
      title={getTitle()}
      aria-label={getTitle()}
    >
      {isProcessing ? (
        <Loader2 className="h-4 w-4 animate-spin" />
      ) : thinkingMode.error ? (
        <AlertCircle className="h-4 w-4" />
      ) : (
        <Brain className={cn('h-4 w-4 transition-colors duration-200', getIconColor())} />
      )}

      <span className="text-sm font-medium">
        {isInvalidModel
          ? '模型无效'
          : isProcessing
            ? '切换中...'
            : thinkingMode.error
              ? '错误'
              : thinkingMode.enabled
                ? '思考模式'
                : '普通模式'}
      </span>
    </Button>
  )
}
