/**
 * 工具列表页面
 *
 * 展示所有工具，支持搜索过滤、分页和展开详情
 */

import { useState, useEffect, useCallback } from 'react'
import { getTools } from '@/services/api/tools'
import type { ToolResponse, GetToolsParams } from '@/services/api/tools'

/**
 * 工具列表页面组件
 */
export function ToolsPage() {
  const [tools, setTools] = useState<ToolResponse[]>([])
  const [total, setTotal] = useState(0)
  const [isLoading, setIsLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [expandedId, setExpandedId] = useState<string | null>(null)

  const [search, setSearch] = useState('')
  const [page, setPage] = useState(1)
  const [pageSize] = useState(12)
  const [filterCategory, setFilterCategory] = useState('')
  const [filterSource, setFilterSource] = useState('')

  /**
   * 加载工具列表
   */
  const fetchTools = useCallback(async () => {
    setIsLoading(true)
    setError(null)
    try {
      const params: GetToolsParams = {
        page,
        pageSize,
        search: search || undefined,
        category: filterCategory || undefined,
        source: filterSource || undefined,
      }
      const res = await getTools(params)
      setTools(res.items)
      setTotal(res.total)
    } catch (err: any) {
      setError(err.message || '获取工具列表失败')
    } finally {
      setIsLoading(false)
    }
  }, [page, pageSize, search, filterCategory, filterSource])

  useEffect(() => {
    fetchTools()
  }, [fetchTools])

  const totalPages = Math.ceil(total / pageSize)

  /** 获取状态标签样式 */
  const getStatusStyle = (status: string) => {
    switch (status) {
      case 'active':
        return 'bg-green-500/10 text-green-500'
      case 'disabled':
        return 'bg-yellow-500/10 text-yellow-500'
      case 'deprecated':
        return 'bg-red-500/10 text-red-500'
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
        <h1 className="ml-4 text-base font-semibold">工具管理</h1>
        <span className="ml-auto text-xs text-muted-foreground">共 {total} 个工具</span>
      </header>
      <main className="p-6 space-y-4">
        {/* 搜索和过滤 */}
        <div className="flex flex-wrap gap-3">
          <input
            type="text"
            placeholder="搜索工具..."
            value={search}
            onChange={e => { setSearch(e.target.value); setPage(1) }}
            className="px-3 py-1.5 text-sm border rounded-lg bg-background focus:outline-none focus:ring-1 focus:ring-primary"
          />
          <select
            value={filterCategory}
            onChange={e => { setFilterCategory(e.target.value); setPage(1) }}
            className="px-3 py-1.5 text-sm border rounded-lg bg-background"
          >
            <option value="">全部分类</option>
            <option value="file">文件</option>
            <option value="search">搜索</option>
            <option value="web">网页</option>
            <option value="memory">记忆</option>
            <option value="task">任务</option>
            <option value="system">系统</option>
            <option value="execution">执行</option>
            <option value="analysis">分析</option>
          </select>
          <select
            value={filterSource}
            onChange={e => { setFilterSource(e.target.value); setPage(1) }}
            className="px-3 py-1.5 text-sm border rounded-lg bg-background"
          >
            <option value="">全部来源</option>
            <option value="builtin">内置</option>
            <option value="mcp">MCP</option>
            <option value="custom">自定义</option>
            <option value="code">代码</option>
          </select>
        </div>

        {/* 加载状态 */}
        {isLoading && (
          <div className="flex items-center justify-center py-12">
            <div className="w-6 h-6 border-2 border-primary border-t-transparent rounded-full animate-spin" />
            <span className="ml-2 text-sm text-muted-foreground">加载中...</span>
          </div>
        )}

        {/* 错误状态 */}
        {error && (
          <div className="p-4 rounded-lg bg-destructive/10 text-destructive text-sm">
            {error}
          </div>
        )}

        {/* 工具列表 */}
        {!isLoading && !error && tools.length === 0 && (
          <div className="text-center py-12 text-muted-foreground">暂无数据</div>
        )}

        {!isLoading && !error && tools.length > 0 && (
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
            {tools.map(tool => (
              <div
                key={tool.name}
                className="border rounded-lg p-4 cursor-pointer hover:bg-accent/30 transition-colors"
                onClick={() => setExpandedId(expandedId === tool.name ? null : tool.name)}
              >
                <div className="flex items-start justify-between mb-2">
                  <h3 className="text-sm font-semibold truncate flex-1 mr-2">{tool.name}</h3>
                  <span className={`text-xs px-2 py-0.5 rounded-full ${getStatusStyle(tool.status)}`}>
                    {tool.status}
                  </span>
                </div>
                <p className="text-xs text-muted-foreground line-clamp-2 mb-2">{tool.description}</p>
                <div className="flex gap-2 text-xs text-muted-foreground">
                  {tool.category && <span className="px-1.5 py-0.5 bg-accent/30 rounded">{tool.category}</span>}
                  <span className="px-1.5 py-0.5 bg-accent/30 rounded">{tool.source}</span>
                </div>

                {/* 展开详情 */}
                {expandedId === tool.name && (
                  <div className="mt-3 pt-3 border-t text-xs space-y-2">
                    {tool.when_to_use && tool.when_to_use.length > 0 && (
                      <div>
                        <span className="text-muted-foreground">适用场景：</span>
                        <ul className="list-disc list-inside mt-1">
                          {tool.when_to_use.map((w, i) => <li key={i}>{w}</li>)}
                        </ul>
                      </div>
                    )}
                    {tool.tags && tool.tags.length > 0 && (
                      <div className="flex flex-wrap gap-1">
                        {tool.tags.map(tag => (
                          <span key={tag} className="px-1.5 py-0.5 bg-primary/10 text-primary rounded text-xs">{tag}</span>
                        ))}
                      </div>
                    )}
                    {tool.version && <div><span className="text-muted-foreground">版本：</span>{tool.version}</div>}
                    {tool.requires_approval !== undefined && (
                      <div><span className="text-muted-foreground">需要审批：</span>{tool.requires_approval ? '是' : '否'}</div>
                    )}
                  </div>
                )}
              </div>
            ))}
          </div>
        )}

        {/* 分页 */}
        {totalPages > 1 && (
          <div className="flex items-center justify-center gap-2 pt-4">
            <button
              onClick={() => setPage(p => Math.max(1, p - 1))}
              disabled={page <= 1}
              className="px-3 py-1.5 text-sm border rounded-lg disabled:opacity-50 hover:bg-accent/50"
            >
              上一页
            </button>
            <span className="text-sm text-muted-foreground">
              {page} / {totalPages}
            </span>
            <button
              onClick={() => setPage(p => Math.min(totalPages, p + 1))}
              disabled={page >= totalPages}
              className="px-3 py-1.5 text-sm border rounded-lg disabled:opacity-50 hover:bg-accent/50"
            >
              下一页
            </button>
          </div>
        )}
      </main>
    </div>
  )
}
