/**
 * Agent 类型图标组件
 *
 * 根据不同的 Agent 类型显示对应的图标和颜色
 */

import { cn } from '@/lib/utils'
import type { ClassValue } from 'clsx'

/**
 * Agent 图标组件属性
 */
export interface AgentIconProps {
  /** Agent 类型 */
  type?: string
  /** 尺寸 */
  size?: 'sm' | 'md' | 'lg'
  /** 自定义图标 */
  icon?: React.ReactNode
  /** 自定义样式类名 */
  className?: string
}

/**
 * Agent 图标组件
 */
export function AgentIcon({ type = 'system', size = 'md', icon, className }: AgentIconProps) {
  if (icon) {
    return <span className={cn('flex items-center justify-center', className)}>{icon}</span>
  }

  const config = getAgentIconConfig(type)

  const sizeClasses = {
    sm: 'w-5 h-5 text-sm',
    md: 'w-8 h-8 text-base',
    lg: 'w-12 h-12 text-xl',
  }

  return (
    <div
      className={cn(
        'flex items-center justify-center rounded-lg font-bold text-white',
        sizeClasses[size],
        config.gradient,
        className,
      )}
      title={config.label}
    >
      {config.icon}
    </div>
  )
}

/**
 * 根据 Agent 类型获取图标配置
 */
function getAgentIconConfig(type?: string) {
  const configs: Record<string, { icon: string; label: string; gradient: string }> = {
    system: {
      icon: '\u2728',
      label: '系统助手',
      gradient: 'bg-gradient-to-br from-yellow-400 to-orange-500',
    },
    code: {
      icon: '\uD83D\uDC0D',
      label: '代码专家',
      gradient: 'bg-gradient-to-br from-blue-500 to-cyan-600',
    },
    doc: {
      icon: '\uD83D\uDCDD',
      label: '文档助手',
      gradient: 'bg-gradient-to-br from-green-500 to-emerald-600',
    },
    test: {
      icon: '\uD83E\uDDEA',
      label: '测试工程师',
      gradient: 'bg-gradient-to-br from-purple-500 to-violet-600',
    },
    debug: {
      icon: '\uD83D\uDD0D',
      label: '调试专家',
      gradient: 'bg-gradient-to-br from-red-500 to-pink-600',
    },
    review: {
      icon: '\uD83D\uDC41\uFE0F',
      label: '代码审查',
      gradient: 'bg-gradient-to-br from-indigo-500 to-blue-600',
    },
    default: {
      icon: '\uD83E\uDD16',
      label: 'Agent',
      gradient: 'bg-gradient-to-br from-gray-500 to-gray-700',
    },
  }

  return configs[type || 'default'] || configs.default
}

/**
 * 紧凑模式的小图标
 */
export interface AgentSmallIconProps {
  type?: string
  icon?: React.ReactNode
  className?: string
}

export function AgentSmallIcon({ type, icon, className = '' }: AgentSmallIconProps) {
  if (icon) {
    return <span className={className || ''}>{icon}</span>
  }

  const config = getAgentIconConfig(type || 'default')

  return (
    <span className={cn('inline-block', className)} title={config.label}>
      {config.icon}
    </span>
  )
}
