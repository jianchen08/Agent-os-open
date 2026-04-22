/**
 * Token 使用统计显示组件
 *
 * 显示当前 Token 使用情况，根据使用率自动显示不同颜色警告
 */

import { AlertTriangle } from 'lucide-react'

export interface TokenUsageBarProps {
  /** 当前使用量 */
  currentUsage: number
  /** 最大限额 */
  maxTokens: number
  /** 是否显示详细信息 */
  showDetails?: boolean
}

/**
 * 获取使用率状态颜色
 */
const getUsageColor = (usagePercent: number) => {
  if (usagePercent >= 95) {
    return {
      text: 'text-red-600 dark:text-red-400',
      bg: 'bg-red-50 dark:bg-red-950/30',
      border: 'border-red-200 dark:border-red-800',
    }
  }
  if (usagePercent >= 80) {
    return {
      text: 'text-orange-600 dark:text-orange-400',
      bg: 'bg-orange-50 dark:bg-orange-950/30',
      border: 'border-orange-200 dark:border-orange-800',
    }
  }
  return {
    text: 'text-muted-foreground',
    bg: 'bg-muted/30',
    border: 'border-border',
  }
}

/**
 * 格式化数字（添加千位分隔符）
 */
const formatNumber = (num: number): string => {
  return num.toLocaleString('en-US')
}

/**
 * Token 使用统计显示组件
 */
export const TokenUsageBar = ({
  currentUsage,
  maxTokens,
  showDetails = false,
}: TokenUsageBarProps) => {
  const usagePercent = (currentUsage / maxTokens) * 100
  const colors = getUsageColor(usagePercent)
  const showWarning = usagePercent >= 80

  return (
    <div
      className={`
        flex items-center gap-2 px-3 py-1.5
        text-xs rounded-lg border
        ${colors.bg} ${colors.border} ${colors.text}
        transition-colors duration-200
      `}
      data-testid="token-usage-bar"
      data-usage-percent={usagePercent.toFixed(1)}
    >
      {showWarning && (
        <AlertTriangle
          className="w-3.5 h-3.5 flex-shrink-0"
          aria-label="Token 使用量警告"
        />
      )}

      <span className="whitespace-nowrap">
        Tokens: {formatNumber(currentUsage)} / {formatNumber(maxTokens)}
      </span>

      {showDetails && (
        <span className="text-xs opacity-75 ml-1">
          ({usagePercent.toFixed(1)}%)
        </span>
      )}
    </div>
  )
}
