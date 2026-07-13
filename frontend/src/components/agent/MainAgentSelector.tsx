/**
 * 主 Agent 选择器组件
 *
 * 只显示主 Agent（type 为 "main"）的选择器
 */

import { Check, ChevronDown } from 'lucide-react'
import { useMemo, useState } from 'react'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { cn } from '@/lib/utils'
import { useAgentStore } from '@/stores/agentStore'
import { AgentIcon } from './AgentIcon'

/**
 * 主 Agent 选择器组件属性
 */
export interface MainAgentSelectorProps {
  /** 当前选中的 Agent ID */
  currentAgentId: string | null
  /** Agent 切换回调 */
  onAgentChange: (agentId: string | null) => void
  /** 自定义样式类名 */
  className?: string
}

/**
 * 主 Agent 选择器组件
 */
export function MainAgentSelector({
  currentAgentId,
  onAgentChange,
  className,
}: MainAgentSelectorProps) {
  const agents = useAgentStore((s) => s.agents)
  const isLoading = useAgentStore((s) => s.isLoading)
  const [isOpen, setIsOpen] = useState(false)

  /** 只筛选主 Agent */
  const mainAgents = useMemo(() => {
    return agents.filter((agent) => agent.type === 'main')
  }, [agents])

  const currentAgent = mainAgents.find((a) => a.id === currentAgentId)

  /** 处理 Agent 选择 */
  const handleSelectAgent = (agentId: string | null) => {
    onAgentChange(agentId)
    setIsOpen(false)
  }

  return (
    <DropdownMenu open={isOpen} onOpenChange={setIsOpen}>
      <DropdownMenuTrigger asChild>
        <button
          className={cn(
            'flex items-center gap-2 rounded-lg px-3 py-2',
            'hover:bg-accent transition-colors',
            'text-left text-sm font-medium',
            'w-full',
            className,
          )}
        >
          {currentAgent ? (
            <>
              <AgentIcon type={currentAgent.type} size="sm" />
              <span className="flex-1 truncate">{currentAgent.name}</span>
            </>
          ) : mainAgents.length > 0 ? (
            <>
              <AgentIcon type={mainAgents[0].type} size="sm" />
              <span className="flex-1 truncate">{mainAgents[0].name}</span>
            </>
          ) : (
            <>
              <AgentIcon type="main" size="sm" />
              <span className="flex-1">默认助手</span>
            </>
          )}
          <ChevronDown className="ml-auto h-4 w-4 opacity-50" />
        </button>
      </DropdownMenuTrigger>

      <DropdownMenuContent align="start" className="w-64">
        {mainAgents.map((agent) => (
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
                {agent.description && (
                  <div className="text-muted-foreground truncate text-xs">{agent.description}</div>
                )}
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

        {!isLoading && mainAgents.length === 0 && (
          <div className="text-muted-foreground px-2 py-1.5 text-center text-sm">暂无可用助手</div>
        )}
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
