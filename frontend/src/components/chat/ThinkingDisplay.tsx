/**
 * ThinkingDisplay 组件
 *
 * 显示思考过程的组件，支持步骤列表和流式内容
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import { ChevronDown, ChevronRight, Loader2, CheckCircle2, Clock, XCircle } from '@/assets/icons'
import { MarkdownRenderer } from '@/components/shared/markdown/MarkdownRenderer'
import { cn } from '@/lib/utils'
import { StreamSpinner } from './StreamActivityIndicator'
import { StreamLiveLine } from './StreamLiveLine'
import type { ThinkingContent, ThinkingStep } from '@/types/models'
import type { FC } from 'react'

/** 思考内容统一样式 - 使用CSS变量适配主题 */
const thinkingTextStyle = {
  fontSize: '0.8125rem',
  color: 'var(--thinking-text-color)',
}

/**
 * 获取步骤状态图标
 */
function getStepStatusIcon(status: 'pending' | 'running' | 'completed' | 'failed') {
  switch (status) {
    case 'pending':
      return <Clock className="h-icon-xs w-icon-xs text-status-warning" />
    case 'running':
      return <Loader2 className="h-icon-xs w-icon-xs animate-spin text-status-info" />
    case 'completed':
      return <CheckCircle2 className="h-icon-xs w-icon-xs text-status-success" />
    case 'failed':
      return <XCircle className="h-icon-xs w-icon-xs text-status-error" />
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
  step: ThinkingStep
  index: number | string
}> = ({ step, index }) => {
  const [expanded, setExpanded] = useState(true)

  return (
    <div
      className={cn(
        'space-y-1.5 border-l-2 pl-3',
        step.status === 'running' && 'border-status-info',
        step.status === 'completed' && 'border-status-success',
        step.status === 'failed' && 'border-status-error',
        step.status === 'pending' && 'border-status-warning',
      )}
    >
      {/* 步骤头部 */}
      <div
        className="flex cursor-pointer items-center gap-2"
        onClick={() => setExpanded(!expanded)}
      >
        {expanded ? <ChevronDown className="h-icon-xs w-icon-xs" /> : <ChevronRight className="h-icon-xs w-icon-xs" />}
        <span className="text-xs font-medium" style={thinkingTextStyle}>
          步骤 {typeof index === 'number' ? index + 1 : index}
        </span>
        <span className="bg-muted/50 rounded px-1.5 py-0.5 text-xs" style={thinkingTextStyle}>
          {getStepTypeLabel(step.type)}
        </span>
        {getStepStatusIcon(step.status)}
      </div>

      {/* 步骤内容 */}
      {expanded && (
        <div className="pl-5">
          <div className="whitespace-pre-wrap" style={thinkingTextStyle}>
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
  /** 流式总耗时锚点（触发消息发出时刻 ms）：思考中态头部实时总耗时用
   *  （2026-11-06 用户裁定图标/耗时统一）；缺省不显示耗时 */
  streamAnchorMs?: number | null
}> = ({ thinking, defaultExpanded = false }) => {
  const [expanded, setExpanded] = useState(defaultExpanded)

  // 流式思考内容贴底跟随：展开时定位到最新；用户上滑看历史时暂停跟随，滑回底部后恢复
  const scrollRef = useRef<HTMLDivElement>(null)
  const stickToBottom = useRef(true)
  const stepsCount = thinking.steps?.length ?? 0

  const handleScroll = useCallback(() => {
    const el = scrollRef.current
    if (!el) return
    // 距底部 28px 内视为"贴底"，恢复跟随
    stickToBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight < 28
  }, [])

  useEffect(() => {
    const el = scrollRef.current
    if (!el || !expanded) return
    if (stickToBottom.current) el.scrollTop = el.scrollHeight
  }, [expanded, thinking.content, thinking.isThinking, stepsCount])



  return (
    <div className="border-border/50 bg-background/60 overflow-hidden rounded-lg border" data-testid="message-think">
      {/* 头部 */}
      <div
        className="hover:bg-muted/30 flex cursor-pointer items-center gap-2 px-3 py-2 transition-colors"
        onClick={() => setExpanded(!expanded)}
      >
        {thinking.isThinking ? (
          <StreamSpinner />
        ) : (
          <CheckCircle2 className="h-icon-md w-icon-md text-status-success" />
        )}
        <span className="text-sm font-medium">思考过程</span>
        {thinking.steps && thinking.steps.length > 0 && (
          <span className="text-xs">{thinking.steps.length} 步</span>
        )}
        {/* 折叠态实时活动行（新增功能 2026-11-18）：折叠时也能看到正在输出的内容。
            2026-11-19 用户纠正二：思考内容**保持单行**（不铺成多行段落）；
            纠正三：「只显示一半」是宽度问题——去掉 max-w-[52%] 百分比上限、并移除
            抢占剩余空间的 flex-1 占位 div，由本行 flex-1 吃满可用宽度后再按容器
            宽度截断（右侧不再留大片空白）。 */}
        {!expanded && thinking.isThinking && thinking.content && (
          <StreamLiveLine
            text={thinking.content}
            running={thinking.isThinking}
            testId="thinking-live-line"
            className="min-w-0 flex-1"
          />
        )}
          {/* 2026-11-19 用户纠正（单一状态指示点）：思考正文右侧不得出现「执行中」
              文字——状态只由消息底部带转圈的「执行中 · N.Ns」承担；卡片运行态由
              左侧 spinner 图标表达，且不显示任何耗时数字。 */}
        {expanded ? (
          <ChevronDown className="h-icon-md w-icon-md ml-auto flex-shrink-0" />
        ) : (
          <ChevronRight className="h-icon-md w-icon-md ml-auto flex-shrink-0" />
        )}
      </div>

      {/* 内容区域 */}
      {expanded && (
        <div
          ref={scrollRef}
          onScroll={handleScroll}
          className="thinking-text-content border-border/50 space-y-3 overflow-y-auto border-t px-3 py-2"
          style={{ ...thinkingTextStyle, maxHeight: '33vh' }}
          data-testid="message-think-body"
        >
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
            <div className="border-border/30 border-t pt-2">
              <div
                className="prose prose-sm dark:prose-invert max-w-none"
                style={{ fontSize: '0.8125rem', color: 'inherit' }}
              >
                {thinking.isThinking ? (
                  <pre className="whitespace-pre-wrap font-sans text-inherit" style={{ fontSize: 'inherit' }}>
                    {thinking.content}
                  </pre>
                ) : (
                  <MarkdownRenderer content={thinking.content} />
                )}
              </div>
            </div>
            ) : null}
        </div>
      )}
    </div>
  )
}

export default ThinkingDisplay
