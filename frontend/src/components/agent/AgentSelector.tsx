/**
 * Agent 选择器组件
 *
 * 用于选择当前会话使用的 Agent，支持完整模式和紧凑模式
 */

import { Check, ChevronDown, Plus, Sparkles } from 'lucide-react'
import { useState } from 'react'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { cn } from '@/lib/utils'
import { useAgentStore } from '@/stores/agentStore'
import { AgentIcon } from './AgentIcon'

/**
 * Agent 选择器组件属性
 */
export interface AgentSelectorProps {
  /** 当前选中的 Agent ID */
  currentAgentId: string | null
  /** Agent 切换回调 */
  onAgentChange: (agentId: string | null) => void
  /** 紧凑模式 */
  compact?: boolean
  /** 自定义样式类名 */
  className?: string
}

/**
 * Agent 选择器组件
 */
export function AgentSelector({
  currentAgentId,
  onAgentChange,
  compact = false,
  className,
}: AgentSelectorProps) {
  const agents = useAgentStore((s) => s.agents)
  const isLoading = useAgentStore((s) => s.isLoading)
  const [isOpen, setIsOpen] = useState(false)

  const currentAgent = agents.find((a) => a.id === currentAgentId)

  /** 处理 Agent 选择 */
  const handleSelectAgent = (agentId: string | null) => {
    onAgentChange(agentId)
    setIsOpen(false)
  }

  /** 紧凑模式 */
  if (compact) {
    return (
      <div className={cn('flex items-center', className)} title={currentAgent?.name || '默认助手'}>
        <AgentIcon type={currentAgent?.type} size="sm" />
      </div>
    )
  }

  return (
    <DropdownMenu open={isOpen} onOpenChange={setIsOpen}>
      <DropdownMenuTrigger asChild>
        <button
          className={cn(
            'flex items-center gap-2 rounded-lg px-3 py-2',
            'hover:bg-accent transition-colors',
            'text-left text-sm font-medium',
            'min-w-[200px]',
            className,
          )}
        >
          {currentAgent ? (
            <>
              <AgentIcon type={currentAgent.type} size="sm" />
              <span className="flex-1 truncate">{currentAgent.name}</span>
            </>
          ) : (
            <>
              <Sparkles className="h-4 w-4 text-status-warning" />
              <span className="flex-1">默认助手</span>
            </>
          )}
          <ChevronDown className="ml-auto h-4 w-4 opacity-50" />
        </button>
      </DropdownMenuTrigger>

      <DropdownMenuContent align="start" className="w-80">
        <DropdownMenuItem onClick={() => handleSelectAgent(null)} className="cursor-pointer">
          <div className="flex flex-1 items-center gap-3">
            <Sparkles className="h-5 w-5 flex-shrink-0 text-status-warning" />
            <div className="min-w-0 flex-1">
              <div className="font-medium">默认助手</div>
              <div className="text-muted-foreground text-xs">通用对话，适合大多数场景</div>
            </div>
            {currentAgentId === null && <Check className="text-primary h-5 w-5 flex-shrink-0" />}
          </div>
        </DropdownMenuItem>

        <DropdownMenuSeparator />

        {agents.map((agent) => (
          <DropdownMenuItem
            key={agent.id}
            onClick={() => handleSelectAgent(agent.id)}
            className="cursor-pointer"
            disabled={agent.status !== 'active'}
          >
            <div className="flex min-w-0 flex-1 items-center gap-3">
              <AgentIcon type={agent.type} size="sm" />
              <div className="min-w-0 flex-1">
                <div className="truncate font-medium">{agent.name}</div>
                <div className="text-muted-foreground truncate text-xs">{agent.description}</div>
              </div>
              {currentAgentId === agent.id && (
                <Check className="text-primary h-5 w-5 flex-shrink-0" />
              )}
            </div>
          </DropdownMenuItem>
        ))}

        {isLoading && (
          <div className="text-muted-foreground px-2 py-1.5 text-center text-sm">加载中...</div>
        )}

        {!isLoading && agents.length === 0 && (
          <div className="text-muted-foreground px-2 py-1.5 text-center text-sm">
            暂无可用 Agent
          </div>
        )}

        <DropdownMenuSeparator />

        <DropdownMenuItem
          onClick={() => {
            window.location.href = '/agents/new'
          }}
          className="text-primary cursor-pointer"
        >
          <div className="flex items-center gap-2">
            <Plus className="h-4 w-4" />
            <span>创建新 Agent</span>
          </div>
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
