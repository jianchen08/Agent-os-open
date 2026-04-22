/**
 * Agent 列表页面
 *
 * 展示所有 Agent，卡片式布局，显示配置信息
 */

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
    } catch (err: any) {
      setError(err.message || '获取 Agent 列表失败')
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
        return 'bg-green-500/10 text-green-500'
      case 'inactive':
        return 'bg-gray-500/10 text-gray-500'
      case 'error':
        return 'bg-red-500/10 text-red-500'
      default:
        return 'bg-gray-500/10 text-gray-500'
    }
  }

  /** 获取类型标签样式 */
  const getTypeBadge = (agentType: string) => {
    switch (agentType) {
      case 'main':
        return 'bg-blue-500/10 text-blue-500'
      case 'sub':
        return 'bg-purple-500/10 text-purple-500'
      case 'atomic':
        return 'bg-orange-500/10 text-orange-500'
      default:
        return 'bg-gray-500/10 text-gray-500'
    }
  }

  return (
    <div className="min-h-screen bg-background text-foreground">
      <header className="h-12 border-b flex items-center px-4 shrink-0">
        <a href="/" className="text-sm text-muted-foreground hover:text-foreground">
          &larr; 返回
        </a>
        <h1 className="ml-4 text-base font-semibold">智能体管理</h1>
        <span className="ml-auto text-xs text-muted-foreground">共 {total} 个智能体</span>
      </header>
      <main className="p-6 space-y-4">
        {/* 搜索 */}
        <input
          type="text"
          placeholder="搜索 Agent..."
          value={search}
          onChange={e => setSearch(e.target.value)}
          className="w-full max-w-md px-3 py-1.5 text-sm border rounded-lg bg-background focus:outline-none focus:ring-1 focus:ring-primary"
        />

        {/* 加载状态 */}
        {isLoading && (
          <div className="flex items-center justify-center py-12">
            <div className="w-6 h-6 border-2 border-primary border-t-transparent rounded-full animate-spin" />
            <span className="ml-2 text-sm text-muted-foreground">加载中...</span>
          </div>
        )}

        {/* 错误状态 */}
        {error && (
          <div className="p-4 rounded-lg bg-destructive/10 text-destructive text-sm">{error}</div>
        )}

        {/* 空状态 */}
        {!isLoading && !error && agents.length === 0 && (
          <div className="text-center py-12 text-muted-foreground">暂无数据</div>
        )}

        {/* Agent 卡片列表 */}
        {!isLoading && !error && agents.length > 0 && (
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
            {agents.map(agent => (
              <div
                key={agent.id}
                className="border rounded-lg p-4 cursor-pointer hover:bg-accent/30 transition-colors"
                onClick={() => setExpandedId(expandedId === agent.id ? null : agent.id)}
              >
                <div className="flex items-start justify-between mb-2">
                  <h3 className="text-sm font-semibold truncate flex-1 mr-2">{agent.name}</h3>
                  <span className={`text-xs px-2 py-0.5 rounded-full ${getStatusStyle(agent.status)}`}>
                    {agent.status}
                  </span>
                </div>
                <p className="text-xs text-muted-foreground line-clamp-2 mb-3">
                  {agent.description || '暂无描述'}
                </p>
                <div className="flex flex-wrap gap-1.5 text-xs">
                  <span className={`px-1.5 py-0.5 rounded ${getTypeBadge(agent.agent_type)}`}>
                    {agent.agent_type}
                  </span>
                  {agent.level && (
                    <span className="px-1.5 py-0.5 bg-accent/30 rounded">{agent.level}</span>
                  )}
                  <span className="px-1.5 py-0.5 bg-accent/30 rounded truncate max-w-[120px]">
                    {agent.model}
                  </span>
                </div>

                {/* 展开详情 */}
                {expandedId === agent.id && (
                  <div className="mt-3 pt-3 border-t text-xs space-y-1.5">
                    {agent.system_prompt && (
                      <div>
                        <span className="text-muted-foreground">系统提示词：</span>
                        <p className="mt-0.5 p-2 bg-accent/20 rounded text-muted-foreground line-clamp-4">
                          {agent.system_prompt}
                        </p>
                      </div>
                    )}
                    {agent.tool_names && agent.tool_names.length > 0 && (
                      <div>
                        <span className="text-muted-foreground">绑定工具：</span>
                        <div className="flex flex-wrap gap-1 mt-0.5">
                          {agent.tool_names.map(t => (
                            <span key={t} className="px-1.5 py-0.5 bg-primary/10 text-primary rounded">{t}</span>
                          ))}
                        </div>
                      </div>
                    )}
                    {agent.max_iterations && (
                      <div><span className="text-muted-foreground">最大迭代：</span>{agent.max_iterations}</div>
                    )}
                    {agent.timeout && (
                      <div><span className="text-muted-foreground">超时：</span>{agent.timeout}s</div>
                    )}
                    {agent.tags && agent.tags.length > 0 && (
                      <div className="flex flex-wrap gap-1">
                        {agent.tags.map(tag => (
                          <span key={tag} className="px-1.5 py-0.5 bg-accent/30 rounded">{tag}</span>
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
