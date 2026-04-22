/**
 * ThinkingDisplay 组件
 *
 * 显示思考过程的组件，支持步骤列表和流式内容
 */

import { useState } from 'react'
import {
  ChevronDown,
  ChevronRight,
  Loader2,
  CheckCircle2,
  Clock,
  XCircle,
} from 'lucide-react'
import type { FC } from 'react'
import { cn } from '@/lib/utils'
import type { ThinkingContent } from '@/types/models'
import { MarkdownRenderer } from './markdown/MarkdownRenderer'

/** 思考内容统一样式 */
const thinkingTextStyle = {
  fontSize: '0.8125rem',
  color: 'rgba(127, 127, 127, 0.7)',
}

/**
 * 获取步骤状态图标
 */
function getStepStatusIcon(
  status: 'pending' | 'running' | 'completed' | 'failed'
) {
  switch (status) {
    case 'pending':
      return <Clock className="w-3 h-3 text-yellow-600 dark:text-yellow-400" />
    case 'running':
      return (
        <Loader2 className="w-3 h-3 text-blue-600 dark:text-blue-400 animate-spin" />
      )
    case 'completed':
      return (
        <CheckCircle2 className="w-3 h-3 text-green-600 dark:text-green-400" />
      )
    case 'failed':
      return <XCircle className="w-3 h-3 text-red-600 dark:text-red-400" />
  }
}

/**
 * 获取步骤类型标签
 */
function getStepTypeLabel(type: string): string {
  const labels: Record<string, string> = {
    reasoning: '推理',
    analysis: '分析',
    planning: '规划',
    evaluation: '评估',
  }
  return labels[type] || type
}

/**
 * 思考步骤组件
 */
const ThinkingStepItem: FC<{
  step: import('@/types/models').ThinkingStep
  index: number | string
}> = ({ step, index }) => {
  const [expanded, setExpanded] = useState(true)

  return (
    <div
      className={cn(
        'border-l-2 pl-3 space-y-1.5',
        step.status === 'running' && 'border-blue-500 dark:border-blue-400',
        step.status === 'completed' && 'border-green-500 dark:border-green-400',
        step.status === 'failed' && 'border-red-500 dark:border-red-400',
        step.status === 'pending' && 'border-yellow-500 dark:border-yellow-400'
      )}
    >
      {/* 步骤头部 */}
      <div
        className="flex items-center gap-2 cursor-pointer"
        onClick={() => setExpanded(!expanded)}
      >
        {expanded ? (
          <ChevronDown className="w-3 h-3" />
        ) : (
          <ChevronRight className="w-3 h-3" />
        )}
        <span className="text-xs font-medium" style={thinkingTextStyle}>
          步骤 {typeof index === 'number' ? index + 1 : index}
        </span>
        <span className="text-xs px-1.5 py-0.5 rounded bg-muted/50" style={thinkingTextStyle}>
          {getStepTypeLabel(step.type)}
        </span>
        {getStepStatusIcon(step.status)}
      </div>

      {/* 步骤内容 */}
      {expanded && (
        <div className="pl-5">
          <div
            className="whitespace-pre-wrap"
            style={thinkingTextStyle}
          >
            {step.content}
          </div>

          {/* 子步骤 */}
          {step.subSteps && step.subSteps.length > 0 && (
            <div className="mt-2 space-y-2">
              {step.subSteps.map((subStep, subIndex) => (
                <ThinkingStepItem
                  key={subStep.id}
                  step={subStep}
                  index={`${typeof index === 'number' ? index + 1 : index}.${subIndex + 1}`}
                />
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  )
}

/**
 * ThinkingDisplay 主组件
 */
export const ThinkingDisplay: FC<{
  thinking: ThinkingContent
  defaultExpanded?: boolean
}> = ({ thinking, defaultExpanded = true }) => {
  const [expanded, setExpanded] = useState(defaultExpanded)

  return (
    <div className="my-2 rounded-lg border border-border/50 bg-background/60 overflow-hidden">
      {/* 头部 */}
      <div
        className="flex items-center gap-2 px-3 py-2 cursor-pointer hover:bg-muted/30 transition-colors"
        onClick={() => setExpanded(!expanded)}
      >
        {thinking.isThinking ? (
          <Loader2 className="w-4 h-4 text-blue-600 dark:text-blue-400 animate-spin" />
        ) : (
          <CheckCircle2 className="w-4 h-4 text-green-600 dark:text-green-400" />
        )}
        <span className="text-sm font-medium">思考过程</span>
        {thinking.steps && thinking.steps.length > 0 && (
          <span className="text-xs">
            {thinking.steps.length} 步
          </span>
        )}
        {thinking.durationMs && (
          <span className="text-xs">
            {(thinking.durationMs / 1000).toFixed(1)}s
          </span>
        )}
        <div className="flex-1" />
        {expanded ? (
          <ChevronDown className="w-4 h-4" />
        ) : (
          <ChevronRight className="w-4 h-4" />
        )}
      </div>

      {/* 内容区域 */}
      {expanded && (
        <div className="thinking-text-content px-3 py-2 border-t border-border/50 space-y-3" style={thinkingTextStyle}>
          {/* 思考步骤列表 */}
          {thinking.steps && thinking.steps.length > 0 && (
            <div className="space-y-2">
              {thinking.steps.map((step, index) => (
                <ThinkingStepItem key={step.id} step={step} index={index} />
              ))}
            </div>
          )}

          {/* 流式内容 */}
          {thinking.content ? (
            <div className="pt-2 border-t border-border/30">
              <div
                className="prose prose-sm dark:prose-invert max-w-none"
                style={{ fontSize: '0.8125rem', color: 'inherit' }}
              >
                <MarkdownRenderer content={thinking.content} />
              </div>
            </div>
          ) : thinking.isThinking ? (
            <div className="flex items-center gap-2 py-1 text-sm">
              <Loader2 className="w-3 h-3 animate-spin" />
              <span>正在思考中...</span>
            </div>
          ) : null}
        </div>
      )}
    </div>
  )
}

export default ThinkingDisplay
