/**
 * 侧边状态条组件
 *
 * 在执行图折叠时显示，展示进度和 Agent 图标
 */
import type { ReactNode } from 'react'
import {
  ChevronLeft,
  Bot,
  DraftingCompass,
  Code,
  FlaskConical,
  CheckCircle,
} from 'lucide-react'
import { cn } from '@/lib/utils'

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
    <div className="relative w-8 h-8 mb-3">
      <svg className="transform -rotate-90 w-8 h-8">
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
      <span className="absolute inset-0 flex items-center justify-center text-xs font-code text-text-primary">
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
const AgentIcon: React.FC<{ type: string; status: string }> = ({
  type,
  status,
}) => {
  const icons: Record<string, ReactNode> = {
    architect: <DraftingCompass className="w-4 h-4" />,
    coder: <Code className="w-4 h-4" />,
    tester: <FlaskConical className="w-4 h-4" />,
    reviewer: <CheckCircle className="w-4 h-4" />,
    main: <Bot className="w-4 h-4" />,
  }

  const statusColor = {
    running: 'text-status-running',
    waiting_input: 'text-status-waiting',
    completed: 'text-status-success',
    failed: 'text-status-error',
  }[status]

  return (
    <div className={statusColor}>
      {icons[type] || <Bot className="w-4 h-4" />}
    </div>
  )
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
    <div className="w-10 h-full glass-panel flex flex-col items-center py-4 gap-2">
      {/* 进度环 */}
      <ProgressRing progress={totalProgress} />

      <div className="w-6 h-px bg-border/50 my-1" />

      {/* Agent 图标列表 */}
      <div className="flex-1 flex flex-col gap-2 overflow-y-auto">
        {activeAgents.map(agent => (
          <button
            key={agent.id}
            onClick={() => onAgentClick?.(agent.id)}
            className={cn(
              'w-8 h-8 rounded-lg flex items-center justify-center',
              'transition-all duration-200',
              'hover:scale-110 hover:bg-surface/80',
              agent.status === 'running' && 'glow-running bg-status-running/10',
              agent.status === 'waiting_input' &&
                'glow-waiting bg-status-waiting/10 animate-scale-pulse',
              agent.status === 'completed' && 'bg-status-success/10',
              agent.status === 'failed' && 'bg-status-error/10'
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
        className="w-8 h-8 rounded-lg glass-panel flex items-center justify-center hover:bg-surface/80 transition-colors"
        title="展开执行图"
      >
        <ChevronLeft className="w-4 h-4" />
      </button>
    </div>
  )
}
