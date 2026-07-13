/**
 * 记忆管理页面
 *
 * 展示情景记忆、语义记忆和搜索功能，顶部显示统计卡片
 */

import { Brain, Inbox, Search } from 'lucide-react'
import { useState, useEffect, useCallback } from 'react'
import { getEpisodes, searchMemory, getMemoryStats, getSemanticMemory } from '@/services/api/memory'
import type { Episode, SemanticKnowledge, MemoryStats, MemoryItem } from '@/services/api/memory'

/** Tab 类型 */
type TabType = 'episodes' | 'semantic' | 'search'

/**
 * 记忆管理页面组件
 */
export function MemoryPage() {
  const [activeTab, setActiveTab] = useState<TabType>('episodes')
  const [stats, setStats] = useState<MemoryStats | null>(null)
  const [isLoading, setIsLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  // 情景记忆
  const [episodes, setEpisodes] = useState<Episode[]>([])
  const [episodesTotal, setEpisodesTotal] = useState(0)
  const [episodesPage, setEpisodesPage] = useState(1)

  // 语义记忆
  const [semantics, setSemantics] = useState<SemanticKnowledge[]>([])

  // 搜索
  const [searchQuery, setSearchQuery] = useState('')
  const [searchResults, setSearchResults] = useState<MemoryItem[]>([])
  const [searchTotal, setSearchTotal] = useState(0)
  const [isSearching, setIsSearching] = useState(false)

  /**
   * 加载统计数据
   */
  const fetchStats = useCallback(async () => {
    try {
      const data = await getMemoryStats()
      setStats(data)
    } catch {
      // 统计加载失败不阻塞页面
    }
  }, [])

  /**
   * 加载情景记忆
   */
  const fetchEpisodes = useCallback(async (page: number) => {
    try {
      const res = await getEpisodes(page, 10)
      setEpisodes(res.items)
      setEpisodesTotal(res.total)
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : '获取情景记忆失败'
      setError(message)
    }
  }, [])

  /**
   * 加载语义记忆
   */
  const fetchSemantics = useCallback(async () => {
    try {
      const res = await getSemanticMemory()
      setSemantics(res.items || [])
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : '获取语义记忆失败'
      setError(message)
    }
  }, [])

  /**
   * 执行搜索
   */
  const handleSearch = async () => {
    if (!searchQuery.trim()) return
    setIsSearching(true)
    setError(null)
    try {
      const res = await searchMemory(searchQuery)
      setSearchResults(res.items)
      setSearchTotal(res.total)
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : '搜索失败'
      setError(message)
    } finally {
      setIsSearching(false)
    }
  }

  useEffect(() => {
    const init = async () => {
      setIsLoading(true)
      await Promise.allSettled([fetchStats(), fetchEpisodes(1)])
      setIsLoading(false)
    }
    init()
  }, [fetchStats, fetchEpisodes])

  /** Tab 切换时加载对应数据 */
  useEffect(() => {
    if (activeTab === 'semantic' && semantics.length === 0) {
      fetchSemantics()
    }
  }, [activeTab, semantics.length, fetchSemantics])

  return (
    <div className="bg-background text-foreground flex h-screen flex-col overflow-hidden">
      <header className="flex h-12 shrink-0 items-center border-b px-4">
        <a href="/" className="text-muted-foreground hover:text-foreground text-sm">
          &larr; 返回
        </a>
        <h1 className="ml-4 text-base font-semibold">记忆管理</h1>
      </header>
      <main className="flex-1 space-y-6 overflow-y-auto p-3 sm:p-6">
        {/* 统计卡片 */}
        {stats && (
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
            <div className="rounded-lg border p-4">
              <div className="text-muted-foreground mb-1 text-xs">情景记忆</div>
              <div className="text-xl font-semibold">{stats.episode_count}</div>
            </div>
            <div className="rounded-lg border p-4">
              <div className="text-muted-foreground mb-1 text-xs">语义知识</div>
              <div className="text-xl font-semibold">{stats.knowledge_count}</div>
            </div>
            <div className="rounded-lg border p-4">
              <div className="text-muted-foreground mb-1 text-xs">总记忆数</div>
              <div className="text-xl font-semibold">{stats.total_count}</div>
            </div>
          </div>
        )}

        {/* Tab 切换 */}
        <div className="flex gap-1 border-b">
          {(['episodes', 'semantic', 'search'] as TabType[]).map((tab) => (
            <button
              key={tab}
              onClick={() => setActiveTab(tab)}
              className={`min-h-[44px] px-4 py-2 text-sm transition-colors ${
                activeTab === tab
                  ? 'border-primary text-foreground border-b-2 font-medium'
                  : 'text-muted-foreground hover:text-foreground'
              }`}
            >
              {tab === 'episodes' ? '情景记忆' : tab === 'semantic' ? '语义记忆' : '搜索'}
            </button>
          ))}
        </div>

        {/* 错误提示 */}
        {error && (
          <div className="bg-destructive/10 text-destructive rounded-lg p-4 text-sm">{error}</div>
        )}

        {/* 加载状态 */}
        {isLoading && (
          <div className="flex items-center justify-center py-12">
            <div className="border-primary h-6 w-6 animate-spin rounded-full border-2 border-t-transparent" />
            <span className="text-muted-foreground ml-2 text-sm">加载中...</span>
          </div>
        )}

        {/* 情景记忆 */}
        {!isLoading && activeTab === 'episodes' && (
          <div className="space-y-3">
            {episodes.length === 0 ? (
              <div className="flex flex-col items-center justify-center py-12">
                <Brain className="text-muted-foreground/40 mb-3 h-10 w-10" />
                <p className="text-muted-foreground text-sm">暂无情景记忆</p>
                <p className="text-muted-foreground/60 mt-1 text-xs">
                  与 Agent 对话后，交互记录将自动保存为情景记忆
                </p>
              </div>
            ) : (
              episodes.map((ep) => (
                <div key={ep.id} className="rounded-lg border p-4">
                  <div className="mb-2 flex items-start justify-between">
                    <h3 className="mr-2 flex-1 text-sm font-semibold">{ep.intent_text}</h3>
                    {ep.final_score !== undefined && (
                      <span className="bg-primary/10 text-primary rounded-full px-2 py-0.5 text-xs">
                        {ep.final_score.toFixed(2)}
                      </span>
                    )}
                  </div>
                  {ep.execution_summary && (
                    <p className="text-muted-foreground mb-2 line-clamp-2 text-xs">
                      {ep.execution_summary}
                    </p>
                  )}
                  <div className="text-muted-foreground flex items-center gap-2 text-xs">
                    <span>{new Date(ep.created_at).toLocaleString()}</span>
                    {ep.tags.length > 0 && (
                      <div className="flex gap-1">
                        {ep.tags.map((tag) => (
                          <span key={tag} className="bg-accent/30 rounded px-1.5 py-0.5">
                            {tag}
                          </span>
                        ))}
                      </div>
                    )}
                  </div>
                </div>
              ))
            )}
            {episodesTotal > 10 && (
              <div className="flex items-center justify-center gap-2">
                <button
                  onClick={() => {
                    setEpisodesPage((p) => p - 1)
                    fetchEpisodes(episodesPage - 1)
                  }}
                  disabled={episodesPage <= 1}
                  className="hover:bg-accent/50 min-h-[44px] rounded-lg border px-3 py-1.5 text-sm disabled:opacity-50"
                >
                  上一页
                </button>
                <span className="text-muted-foreground text-sm">
                  {episodesPage} / {Math.ceil(episodesTotal / 10)}
                </span>
                <button
                  onClick={() => {
                    setEpisodesPage((p) => p + 1)
                    fetchEpisodes(episodesPage + 1)
                  }}
                  disabled={episodesPage >= Math.ceil(episodesTotal / 10)}
                  className="hover:bg-accent/50 min-h-[44px] rounded-lg border px-3 py-1.5 text-sm disabled:opacity-50"
                >
                  下一页
                </button>
              </div>
            )}
          </div>
        )}

        {/* 语义记忆 */}
        {!isLoading && activeTab === 'semantic' && (
          <div className="space-y-3">
            {semantics.length === 0 ? (
              <div className="flex flex-col items-center justify-center py-12">
                <Inbox className="text-muted-foreground/40 mb-3 h-10 w-10" />
                <p className="text-muted-foreground text-sm">暂无语义记忆</p>
                <p className="text-muted-foreground/60 mt-1 text-xs">
                  系统会自动从交互中提取语义知识并存储
                </p>
              </div>
            ) : (
              semantics.map((sm) => (
                <div key={sm.id} className="rounded-lg border p-4">
                  <p className="mb-2 text-sm">{sm.content}</p>
                  <div className="text-muted-foreground flex items-center gap-2 text-xs">
                    <span className="bg-accent/30 rounded px-1.5 py-0.5">{sm.source_type}</span>
                    <span>{new Date(sm.created_at).toLocaleString()}</span>
                  </div>
                </div>
              ))
            )}
          </div>
        )}

        {/* 搜索 */}
        {!isLoading && activeTab === 'search' && (
          <div className="space-y-4">
            <div className="flex flex-col gap-2 sm:flex-row">
              <input
                type="text"
                placeholder="搜索记忆..."
                value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
                onKeyDown={(e) => e.key === 'Enter' && handleSearch()}
                aria-label="搜索记忆"
                className="bg-background focus:ring-primary max-w-md flex-1 rounded-lg border px-3 py-1.5 text-sm focus:ring-1 focus:outline-none"
              />
              <button
                onClick={handleSearch}
                disabled={isSearching}
                className="bg-primary text-primary-foreground min-h-[44px] rounded-lg px-4 py-1.5 text-sm hover:opacity-90 disabled:opacity-50"
              >
                {isSearching ? '搜索中...' : '搜索'}
              </button>
            </div>
            {searchResults.length > 0 && (
              <div className="text-muted-foreground mb-2 text-xs">找到 {searchTotal} 条结果</div>
            )}
            {searchResults.length === 0 && searchQuery && !isSearching && (
              <div className="flex flex-col items-center justify-center py-12">
                <Search className="text-muted-foreground/40 mb-3 h-10 w-10" />
                <p className="text-muted-foreground text-sm">无搜索结果</p>
                <p className="text-muted-foreground/60 mt-1 text-xs">
                  尝试使用不同的关键词搜索
                </p>
              </div>
            )}
            {searchResults.map((item) => (
              <div key={item.id} className="rounded-lg border p-4">
                <p className="mb-2 text-sm">{item.content}</p>
                <div className="text-muted-foreground flex items-center gap-2 text-xs">
                  <span className="bg-accent/30 rounded px-1.5 py-0.5">{item.memory_type}</span>
                  {item.score > 0 && (
                    <span className="bg-primary/10 text-primary rounded px-1.5 py-0.5">
                      相关度: {item.score.toFixed(2)}
                    </span>
                  )}
                  <span>{new Date(item.created_at).toLocaleString()}</span>
                </div>
              </div>
            ))}
          </div>
        )}
      </main>
    </div>
  )
}
