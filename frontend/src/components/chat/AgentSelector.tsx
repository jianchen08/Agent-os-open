/**
 * Agent 选择器组件
 *
 * REQ-22: 主管道会话添加「更换主 Agent」功能
 * 在聊天界面头部提供 Agent 切换入口，调用后端 API 切换当前会话的 Agent。
 *
 * 暴露接口：
 * - AgentSelector: React 组件
 * - AgentSelectorProps: 组件属性类型
 */

import { ChevronDown } from 'lucide-react'
import { useState, useRef, useEffect, useCallback } from 'react'
import { useAgentStore } from '@/stores/agentStore'
import { updateSessionAgent } from '@/services/api/session'
import type { Agent } from '@/types/models'

/** AgentSelector 组件属性 */
export interface AgentSelectorProps {
  /** 当前会话 ID */
  sessionId: string
  /** 当前绑定的 Agent ID */
  currentAgentId?: string | null
  /** Agent 切换成功后的回调 */
  onAgentChanged?: (agentId: string, agent: Agent) => void
}

/**
 * Agent 选择器组件
 *
 * 下拉菜单式 Agent 选择器，只显示 L1 主 Agent。
 */
export function AgentSelector({
  sessionId,
  currentAgentId,
  onAgentChanged,
}: AgentSelectorProps) {
  const [isOpen, setIsOpen] = useState(false)
  const [isSwitching, setIsSwitching] = useState(false)
  const dropdownRef = useRef<HTMLDivElement>(null)

  const agents = useAgentStore((s) => s.agents)
  const fetchAgents = useAgentStore((s) => s.fetchAgents)

  // 加载 Agent 列表
  useEffect(() => {
    if (agents.length === 0) {
      fetchAgents().catch(() => {})
    }
  }, [agents.length, fetchAgents])

  // 点击外部关闭下拉
  useEffect(() => {
    if (!isOpen) return
    const handleClickOutside = (e: MouseEvent) => {
      if (dropdownRef.current && !dropdownRef.current.contains(e.target as Node)) {
        setIsOpen(false)
      }
    }
    document.addEventListener('mousedown', handleClickOutside)
    return () => document.removeEventListener('mousedown', handleClickOutside)
  }, [isOpen])

  const currentAgent = agents.find(
    (a) => a.id === currentAgentId || a.configId === currentAgentId,
  )

  const handleSelect = useCallback(
    async (agent: Agent) => {
      if (isSwitching) return
      if (agent.id === currentAgentId || agent.configId === currentAgentId) {
        setIsOpen(false)
        return
      }

      setIsSwitching(true)
      try {
        await updateSessionAgent(sessionId, agent.configId || agent.id)
        onAgentChanged?.(agent.configId || agent.id, agent)
      } catch (error) {
        console.error('[AgentSelector] 切换 Agent 失败:', error)
      } finally {
        setIsSwitching(false)
        setIsOpen(false)
      }
    },
    [sessionId, currentAgentId, isSwitching, onAgentChanged],
  )

  if (agents.length === 0) {
    return null
  }

  return (
    <div ref={dropdownRef} className="relative">
      <button
        onClick={() => setIsOpen(!isOpen)}
        disabled={isSwitching}
        className="hover:bg-accent/50 flex items-center gap-1.5 rounded-md border px-2 py-1 text-xs transition-colors disabled:opacity-50"
        data-testid="agent-selector-toggle"
      >
        <span className="max-w-[120px] truncate">
          {isSwitching ? '切换中...' : currentAgent?.name || '灵汐'}
        </span>
        <ChevronDown className="h-3 w-3 shrink-0 opacity-50" />
      </button>

      {isOpen && (
        <div
          className="bg-popover text-popover-foreground absolute right-0 top-full z-50 mt-1 min-w-[180px] rounded-lg border shadow-lg"
          data-testid="agent-selector-dropdown"
        >
          <div className="border-b px-3 py-1.5 text-xs font-medium text-muted-foreground">
            选择主 Agent
          </div>
          <div className="max-h-[240px] overflow-y-auto py-1">
            {agents.map((agent) => {
              const isActive =
                agent.id === currentAgentId || agent.configId === currentAgentId
              return (
                <button
                  key={agent.id}
                  onClick={() => handleSelect(agent)}
                  disabled={isSwitching}
                  className={`flex w-full items-center gap-2 px-3 py-2 text-left text-xs transition-colors hover:bg-accent/50 disabled:opacity-50 ${
                    isActive ? 'bg-accent/30' : ''
                  }`}
                  data-testid={`agent-option-${agent.id}`}
                >
                  <span className="flex-1 truncate font-medium">
                    {agent.name}
                  </span>
                  {agent.model && (
                    <span className="text-muted-foreground shrink-0 text-[10px]">
                      {agent.model}
                    </span>
                  )}
                  {isActive && (
                    <span className="text-primary text-[10px]">●</span>
                  )}
                </button>
              )
            })}
          </div>
        </div>
      )}
    </div>
  )
}
