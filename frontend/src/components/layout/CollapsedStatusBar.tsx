/**
 * 侧边状态条组件
 *
 * 在执行图折叠时显示，展示进度和 Agent 图标
 */
import { ChevronLeft, Bot, DraftingCompass, Code, FlaskConical, CheckCircle } from 'lucide-react'
import { cn } from '@/lib/utils'
import type { ReactNode } from 'react'

/** Agent 信息接口 */
export interface AgentInfo {
  id: string
  name: string
  type: 'architect' | 'coder' | 'tester' | 'reviewer' | 'main'
  status: 'running' | 'waiting_input' | 'completed' | 'failed'
}

interface CollapsedStatusBarProps {
  /** 总进度百分比 (0-100) */
  totalProgress: number
  /** 活跃的 Agent 列表 */
  activeAgents: AgentInfo[]
  /** 点击 Agent 图标回调 */
  onAgentClick?: (agentId: string) => void
  /** 展开按钮回调 */
  onExpand: () => void
}

/**
 * 进度环组件
 *
 * 使用 SVG 圆环展示进度百分比
 */
const ProgressRing: React.FC<{ progress: number }> = ({ progress }) => {
  const radius = 14
  const circumference = 2 * Math.PI * radius
  const offset = circumference * (1 - progress / 100)

  return (
    <div className="relative mb-3 h-8 w-8">
      <svg className="h-8 w-8 -rotate-90 transform">
        {/* 背景圆环 */}
        <circle
          cx="16"
          cy="16"
          r={radius}
          fill="none"
          stroke="var(--border-default)"
          strokeWidth="2"
        />
        {/* 进度圆环 */}
        <circle
          cx="16"
          cy="16"
          r={radius}
          fill="none"
          stroke="var(--accent-running)"
          strokeWidth="2"
          strokeDasharray={circumference}
          strokeDashoffset={offset}
          strokeLinecap="round"
          className="transition-all duration-500"
        />
      </svg>
      {/* 中心文字 */}
      <span className="font-code text-text-primary absolute inset-0 flex items-center justify-center text-xs">
        {progress}%
      </span>
    </div>
  )
}

/**
 * Agent 图标组件
 *
 * 根据 Agent 类型和状态渲染对应图标和颜色
 */
const AgentIcon: React.FC<{ type: string; status: string }> = ({ type, status }) => {
  const icons: Record<string, ReactNode> = {
    architect: <DraftingCompass className="h-4 w-4" />,
    coder: <Code className="h-4 w-4" />,
    tester: <FlaskConical className="h-4 w-4" />,
    reviewer: <CheckCircle className="h-4 w-4" />,
    main: <Bot className="h-4 w-4" />,
  }

  const statusColor = {
    running: 'text-status-running',
    waiting_input: 'text-status-waiting',
    completed: 'text-status-success',
    failed: 'text-status-error',
  }[status]

  return <div className={statusColor}>{icons[type] || <Bot className="h-4 w-4" />}</div>
}

/**
 * 侧边状态条主组件
 *
 * 显示进度环、Agent 图标列表和展开按钮
 */
export const CollapsedStatusBar: React.FC<CollapsedStatusBarProps> = ({
  totalProgress,
  activeAgents,
  onAgentClick,
  onExpand,
}) => {
  return (
    <div className="glass-panel flex h-full w-10 flex-col items-center gap-2 py-4">
      {/* 进度环 */}
      <ProgressRing progress={totalProgress} />

      <div className="bg-border/50 my-1 h-px w-6" />

      {/* Agent 图标列表 */}
      <div className="flex flex-1 flex-col gap-2 overflow-y-auto">
        {activeAgents.map((agent) => (
          <button
            key={agent.id}
            onClick={() => onAgentClick?.(agent.id)}
            className={cn(
              'flex h-8 w-8 items-center justify-center rounded-lg',
              'transition-all duration-200',
              'hover:bg-surface/80 hover:scale-110',
              agent.status === 'running' && 'glow-running bg-status-running/10',
              agent.status === 'waiting_input' &&
                'glow-waiting bg-status-waiting/10 animate-scale-pulse',
              agent.status === 'completed' && 'bg-status-success/10',
              agent.status === 'failed' && 'bg-status-error/10',
            )}
            title={agent.name}
          >
            <AgentIcon type={agent.type} status={agent.status} />
          </button>
        ))}
      </div>

      {/* 底部展开按钮 */}
      <button
        onClick={onExpand}
        className="glass-panel hover:bg-surface/80 flex h-8 w-8 items-center justify-center rounded-lg transition-colors"
        title="展开执行图"
      >
        <ChevronLeft className="h-4 w-4" />
      </button>
    </div>
  )
}
