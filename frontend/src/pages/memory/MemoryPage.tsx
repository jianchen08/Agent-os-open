/**
 * 记忆管理页面
 *
 * 展示情景记忆、语义记忆和搜索功能，顶部显示统计卡片
 */

import { useState, useEffect, useCallback } from 'react'
import {
  getEpisodes,
  searchMemory,
  getMemoryStats,
  getSemanticMemory,
} from '@/services/api/memory'
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
    } catch (err: any) {
      setError(err.message || '获取情景记忆失败')
    }
  }, [])

  /**
   * 加载语义记忆
   */
  const fetchSemantics = useCallback(async () => {
    try {
      const res = await getSemanticMemory()
      setSemantics(res.items || [])
    } catch (err: any) {
      setError(err.message || '获取语义记忆失败')
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
    } catch (err: any) {
      setError(err.message || '搜索失败')
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
    <div className="h-screen flex flex-col bg-background text-foreground overflow-hidden">
      <header className="h-12 border-b flex items-center px-4 shrink-0">
        <a href="/" className="text-sm text-muted-foreground hover:text-foreground">
          &larr; 返回
        </a>
        <h1 className="ml-4 text-base font-semibold">记忆管理</h1>
      </header>
      <main className="flex-1 overflow-y-auto p-6 space-y-6">
        {/* 统计卡片 */}
        {stats && (
          <div className="grid grid-cols-3 gap-4">
            <div className="p-4 border rounded-lg">
              <div className="text-xs text-muted-foreground mb-1">情景记忆</div>
              <div className="text-xl font-semibold">{stats.episode_count}</div>
            </div>
            <div className="p-4 border rounded-lg">
              <div className="text-xs text-muted-foreground mb-1">语义知识</div>
              <div className="text-xl font-semibold">{stats.knowledge_count}</div>
            </div>
            <div className="p-4 border rounded-lg">
              <div className="text-xs text-muted-foreground mb-1">总记忆数</div>
              <div className="text-xl font-semibold">{stats.total_count}</div>
            </div>
          </div>
        )}

        {/* Tab 切换 */}
        <div className="flex gap-1 border-b">
          {(['episodes', 'semantic', 'search'] as TabType[]).map(tab => (
            <button
              key={tab}
              onClick={() => setActiveTab(tab)}
              className={`px-4 py-2 text-sm transition-colors ${
                activeTab === tab
                  ? 'border-b-2 border-primary text-foreground font-medium'
                  : 'text-muted-foreground hover:text-foreground'
              }`}
            >
              {tab === 'episodes' ? '情景记忆' : tab === 'semantic' ? '语义记忆' : '搜索'}
            </button>
          ))}
        </div>

        {/* 错误提示 */}
        {error && (
          <div className="p-4 rounded-lg bg-destructive/10 text-destructive text-sm">{error}</div>
        )}

        {/* 加载状态 */}
        {isLoading && (
          <div className="flex items-center justify-center py-12">
            <div className="w-6 h-6 border-2 border-primary border-t-transparent rounded-full animate-spin" />
            <span className="ml-2 text-sm text-muted-foreground">加载中...</span>
          </div>
        )}

        {/* 情景记忆 */}
        {!isLoading && activeTab === 'episodes' && (
          <div className="space-y-3">
            {episodes.length === 0 ? (
              <div className="text-center py-8 text-muted-foreground">暂无数据</div>
            ) : (
              episodes.map(ep => (
                <div key={ep.id} className="border rounded-lg p-4">
                  <div className="flex items-start justify-between mb-2">
                    <h3 className="text-sm font-semibold flex-1 mr-2">{ep.intent_text}</h3>
                    {ep.final_score !== undefined && (
                      <span className="text-xs px-2 py-0.5 bg-primary/10 text-primary rounded-full">
                        {ep.final_score.toFixed(2)}
                      </span>
                    )}
                  </div>
                  {ep.execution_summary && (
                    <p className="text-xs text-muted-foreground line-clamp-2 mb-2">{ep.execution_summary}</p>
                  )}
                  <div className="flex items-center gap-2 text-xs text-muted-foreground">
                    <span>{new Date(ep.created_at).toLocaleString()}</span>
                    {ep.tags.length > 0 && (
                      <div className="flex gap-1">
                        {ep.tags.map(tag => (
                          <span key={tag} className="px-1.5 py-0.5 bg-accent/30 rounded">{tag}</span>
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
                  onClick={() => { setEpisodesPage(p => p - 1); fetchEpisodes(episodesPage - 1) }}
                  disabled={episodesPage <= 1}
                  className="px-3 py-1.5 text-sm border rounded-lg disabled:opacity-50 hover:bg-accent/50"
                >
                  上一页
                </button>
                <span className="text-sm text-muted-foreground">
                  {episodesPage} / {Math.ceil(episodesTotal / 10)}
                </span>
                <button
                  onClick={() => { setEpisodesPage(p => p + 1); fetchEpisodes(episodesPage + 1) }}
                  disabled={episodesPage >= Math.ceil(episodesTotal / 10)}
                  className="px-3 py-1.5 text-sm border rounded-lg disabled:opacity-50 hover:bg-accent/50"
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
              <div className="text-center py-8 text-muted-foreground">暂无数据</div>
            ) : (
              semantics.map(sm => (
                <div key={sm.id} className="border rounded-lg p-4">
                  <p className="text-sm mb-2">{sm.content}</p>
                  <div className="flex items-center gap-2 text-xs text-muted-foreground">
                    <span className="px-1.5 py-0.5 bg-accent/30 rounded">{sm.source_type}</span>
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
            <div className="flex gap-2">
              <input
                type="text"
                placeholder="搜索记忆..."
                value={searchQuery}
                onChange={e => setSearchQuery(e.target.value)}
                onKeyDown={e => e.key === 'Enter' && handleSearch()}
                className="flex-1 max-w-md px-3 py-1.5 text-sm border rounded-lg bg-background focus:outline-none focus:ring-1 focus:ring-primary"
              />
              <button
                onClick={handleSearch}
                disabled={isSearching}
                className="px-4 py-1.5 text-sm bg-primary text-primary-foreground rounded-lg hover:opacity-90 disabled:opacity-50"
              >
                {isSearching ? '搜索中...' : '搜索'}
              </button>
            </div>
            {searchResults.length > 0 && (
              <div className="text-xs text-muted-foreground mb-2">找到 {searchTotal} 条结果</div>
            )}
            {searchResults.length === 0 && searchQuery && !isSearching && (
              <div className="text-center py-8 text-muted-foreground">无搜索结果</div>
            )}
            {searchResults.map(item => (
              <div key={item.id} className="border rounded-lg p-4">
                <p className="text-sm mb-2">{item.content}</p>
                <div className="flex items-center gap-2 text-xs text-muted-foreground">
                  <span className="px-1.5 py-0.5 bg-accent/30 rounded">{item.memory_type}</span>
                  {item.score > 0 && (
                    <span className="px-1.5 py-0.5 bg-primary/10 text-primary rounded">
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
