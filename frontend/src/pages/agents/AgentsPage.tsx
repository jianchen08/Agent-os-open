/**
 * Agent 列表页面
 *
 * 展示所有 Agent，卡片式布局，显示配置信息
 */

import { Bot, RefreshCw, Search } from 'lucide-react'
import { useState, useEffect, useCallback } from 'react'
import { getAgents } from '@/services/api/agents'
import type { AgentResponse } from '@/services/api/agents'

/**
 * Agent 列表页面组件
 */
export function AgentsPage() {
  const [agents, setAgents] = useState<AgentResponse[]>([])
  const [total, setTotal] = useState(0)
  const [isLoading, setIsLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [expandedId, setExpandedId] = useState<string | null>(null)
  const [search, setSearch] = useState('')

  /**
   * 加载 Agent 列表
   */
  const fetchAgents = useCallback(async () => {
    setIsLoading(true)
    setError(null)
    try {
      const res = await getAgents({ search: search || undefined, pageSize: 100 })
      setAgents(res.items)
      setTotal(res.total)
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : '获取 Agent 列表失败'
      setError(message)
    } finally {
      setIsLoading(false)
    }
  }, [search])

  useEffect(() => {
    fetchAgents()
  }, [fetchAgents])

  /** 获取状态标签样式 */
  const getStatusStyle = (status: string) => {
    switch (status) {
      case 'active':
        return 'bg-status-success/10 text-status-success'
      case 'inactive':
        return 'bg-status-pending/10 text-status-pending'
      case 'error':
        return 'bg-status-error/10 text-status-error'
      default:
        return 'bg-status-pending/10 text-status-pending'
    }
  }

  /** 获取类型标签样式 */
  const getTypeBadge = (agentType: string) => {
    switch (agentType) {
      case 'main':
        return 'bg-status-info/10 text-status-info'
      case 'sub':
        return 'bg-accent/10 text-accent'
      case 'atomic':
        return 'bg-status-running/10 text-status-running'
      default:
        return 'bg-status-pending/10 text-status-pending'
    }
  }

  return (
    <div className="bg-background text-foreground flex h-screen flex-col overflow-hidden">
      <header className="flex h-12 shrink-0 items-center border-b px-4">
        <a href="/" className="text-muted-foreground hover:text-foreground text-sm">
          &larr; 返回
        </a>
        <h1 className="ml-4 text-base font-semibold">智能体管理</h1>
        <div className="ml-auto flex items-center gap-2">
          <span className="text-muted-foreground text-xs">共 {total} 个智能体</span>
          <button
            onClick={fetchAgents}
            disabled={isLoading}
            className="hover:bg-accent/50 min-h-[44px] min-w-[44px] rounded-lg border px-2 py-1 text-xs disabled:opacity-50"
            aria-label="刷新智能体列表"
          >
            <RefreshCw className={`h-3.5 w-3.5 ${isLoading ? 'animate-spin' : ''}`} />
          </button>
        </div>
      </header>
      <main className="flex-1 space-y-4 overflow-y-auto p-3 sm:p-6">
        {/* 搜索 */}
        <input
          type="text"
          placeholder="搜索 Agent..."
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          aria-label="搜索智能体"
          className="bg-background focus:ring-primary w-full max-w-md rounded-lg border px-3 py-1.5 text-sm focus:ring-1 focus:outline-none"
        />

        {/* 加载状态 - 骨架屏 */}
        {isLoading && (
          <div className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-3">
            {Array.from({ length: 6 }).map((_, i) => (
              <div key={i} className="animate-pulse rounded-lg border p-4">
                <div className="mb-2 flex items-start justify-between">
                  <div className="bg-muted h-4 w-2/3 rounded" />
                  <div className="bg-muted h-5 w-12 rounded-full" />
                </div>
                <div className="bg-muted mb-3 h-3 w-full rounded" />
                <div className="bg-muted mb-1.5 h-3 w-4/5 rounded" />
                <div className="flex gap-1.5">
                  <div className="bg-muted h-5 w-10 rounded" />
                  <div className="bg-muted h-5 w-16 rounded" />
                </div>
              </div>
            ))}
          </div>
        )}

        {/* 错误状态 */}
        {error && (
          <div className="bg-destructive/10 text-destructive rounded-lg p-4 text-sm">{error}</div>
        )}

        {/* 空状态 */}
        {!isLoading && !error && agents.length === 0 && (
          <div className="flex flex-col items-center justify-center py-16">
            <Bot className="text-muted-foreground/40 mb-3 h-12 w-12" />
            <p className="text-muted-foreground text-sm">
              {search ? '没有找到匹配的智能体' : '暂无智能体'}
            </p>
            {!search && (
              <p className="text-muted-foreground/60 mt-1 text-xs">
                请在 config/agents/ 目录下添加 Agent 配置文件
              </p>
            )}
          </div>
        )}

        {/* Agent 卡片列表 */}
        {!isLoading && !error && agents.length > 0 && (
          <div
            className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-3"
            role="list"
            aria-live="polite"
            aria-label="智能体列表"
          >
            {agents.map((agent) => (
              <div
                key={agent.id}
                className="hover:bg-accent/30 cursor-pointer rounded-lg border p-4 transition-colors"
                onClick={() => setExpandedId(expandedId === agent.id ? null : agent.id)}
                role="listitem"
              >
                <div className="mb-2 flex items-start justify-between">
                  <h3 className="mr-2 flex-1 truncate text-sm font-semibold">{agent.name}</h3>
                  <span
                    className={`rounded-full px-2 py-0.5 text-xs ${getStatusStyle(agent.status)}`}
                  >
                    {agent.status}
                  </span>
                </div>
                <p className="text-muted-foreground mb-3 line-clamp-2 text-xs">
                  {agent.description || '暂无描述'}
                </p>
                <div className="flex flex-wrap gap-1.5 text-xs">
                  <span className={`rounded px-1.5 py-0.5 ${getTypeBadge(agent.agent_type)}`}>
                    {agent.agent_type}
                  </span>
                  {agent.level && (
                    <span className="bg-accent/30 rounded px-1.5 py-0.5">{agent.level}</span>
                  )}
                  <span className="bg-accent/30 max-w-[120px] truncate rounded px-1.5 py-0.5">
                    {agent.model}
                  </span>
                </div>

                {/* 展开详情 */}
                {expandedId === agent.id && (
                  <div className="mt-3 space-y-1.5 border-t pt-3 text-xs">
                    {agent.system_prompt && (
                      <div>
                        <span className="text-muted-foreground">系统提示词：</span>
                        <p className="bg-accent/20 text-muted-foreground mt-0.5 line-clamp-4 rounded p-2">
                          {agent.system_prompt}
                        </p>
                      </div>
                    )}
                    {agent.tool_names && agent.tool_names.length > 0 && (
                      <div>
                        <span className="text-muted-foreground">绑定工具：</span>
                        <div className="mt-0.5 flex flex-wrap gap-1">
                          {agent.tool_names.map((t) => (
                            <span
                              key={t}
                              className="bg-primary/10 text-primary rounded px-1.5 py-0.5"
                            >
                              {t}
                            </span>
                          ))}
                        </div>
                      </div>
                    )}
                    {agent.max_iterations && (
                      <div>
                        <span className="text-muted-foreground">最大迭代：</span>
                        {agent.max_iterations}
                      </div>
                    )}
                    {agent.timeout && (
                      <div>
                        <span className="text-muted-foreground">超时：</span>
                        {agent.timeout}s
                      </div>
                    )}
                    {agent.tags && agent.tags.length > 0 && (
                      <div className="flex flex-wrap gap-1">
                        {agent.tags.map((tag) => (
                          <span key={tag} className="bg-accent/30 rounded px-1.5 py-0.5">
                            {tag}
                          </span>
                        ))}
                      </div>
                    )}
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
      </main>
    </div>
  )
}
