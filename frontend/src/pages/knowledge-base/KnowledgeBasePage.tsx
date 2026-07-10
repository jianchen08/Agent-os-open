/**
 * 知识库管理页面
 *
 * 展示知识库列表，支持文件上传、分类管理、标签云和统计信息
 */

import {
  BookOpen,
  Upload,
  Trash2,
  FolderPlus,
  X,
  Tag,
  Folder,
  FileText,
  Cloud,
} from 'lucide-react'
import { useState, useEffect, useCallback, useRef } from 'react'
import apiClient from '@/services/api/client'
import { API_ENDPOINTS } from '@/constants/api'

/** 知识库条目 */
interface KnowledgeItem {
  id: string
  name: string
  size: number
  categories: string[]
  tags: string[]
  created_at?: string
  updated_at?: string
  [key: string]: unknown
}

/** 知识库统计 */
interface KnowledgeStats {
  total: number
  categories_count: number
  tags_count: number
  [key: string]: unknown
}

/** 分类信息 */
interface CategoryItem {
  name: string
  count?: number
  [key: string]: unknown
}

/**
 * 格式化文件大小
 *
 * Args:
 *   bytes: 文件字节数
 *
 * Returns:
 *   格式化后的文件大小字符串
 */
function formatFileSize(bytes: number): string {
  if (bytes === 0) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB']
  const i = Math.floor(Math.log(bytes) / Math.log(1024))
  return `${(bytes / Math.pow(1024, i)).toFixed(1)} ${units[i]}`
}

/**
 * 知识库管理页面组件
 */
export function KnowledgeBasePage() {
  const [items, setItems] = useState<KnowledgeItem[]>([])
  const [stats, setStats] = useState<KnowledgeStats | null>(null)
  const [categories, setCategories] = useState<CategoryItem[]>([])
  const [tags, setTags] = useState<string[]>([])
  const [isLoading, setIsLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [actionMessage, setActionMessage] = useState<string | null>(null)

  // 分类筛选
  const [selectedCategory, setSelectedCategory] = useState<string | null>(null)

  // 上传状态
  const [isUploading, setIsUploading] = useState(false)
  const [isDragging, setIsDragging] = useState(false)
  const fileInputRef = useRef<HTMLInputElement>(null)

  // 创建分类模态框
  const [showCategoryModal, setShowCategoryModal] = useState(false)
  const [newCategoryName, setNewCategoryName] = useState('')
  const [isCreatingCategory, setIsCreatingCategory] = useState(false)

  // 删除确认
  const [confirmDeleteId, setConfirmDeleteId] = useState<string | null>(null)
  const [deletingId, setDeletingId] = useState<string | null>(null)

  // 删除分类确认
  const [confirmDeleteCategory, setConfirmDeleteCategory] = useState<string | null>(null)

  /**
   * 加载所有数据
   */
  const fetchData = useCallback(async () => {
    setIsLoading(true)
    setError(null)
    try {
      const [itemsRes, statsRes, catRes, tagsRes] = await Promise.allSettled([
        apiClient.get<KnowledgeItem[]>(API_ENDPOINTS.KNOWLEDGE_BASE.LIST),
        apiClient.get<KnowledgeStats>(API_ENDPOINTS.KNOWLEDGE_BASE.STATS),
        apiClient.get<CategoryItem[]>(API_ENDPOINTS.KNOWLEDGE_BASE.CATEGORIES),
        apiClient.get<string[]>(API_ENDPOINTS.KNOWLEDGE_BASE.TAGS),
      ])
      if (itemsRes.status === 'fulfilled') setItems(Array.isArray(itemsRes.value.data) ? itemsRes.value.data : [])
      if (statsRes.status === 'fulfilled') setStats(statsRes.value.data)
      if (catRes.status === 'fulfilled') setCategories(Array.isArray(catRes.value.data) ? catRes.value.data : [])
      if (tagsRes.status === 'fulfilled') setTags(Array.isArray(tagsRes.value.data) ? tagsRes.value.data : [])
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : '加载数据失败'
      setError(message)
    } finally {
      setIsLoading(false)
    }
  }, [])

  useEffect(() => {
    fetchData()
  }, [fetchData])

  /**
   * 处理文件上传
   *
   * Args:
   *   files: 要上传的文件列表
   */
  const handleUpload = async (files: FileList | File[]) => {
    if (files.length === 0) return
    setIsUploading(true)
    setActionMessage(null)
    try {
      for (const file of files) {
        const formData = new FormData()
        formData.append('file', file)
        await apiClient.post(API_ENDPOINTS.KNOWLEDGE_BASE.UPLOAD, formData, {
          headers: { 'Content-Type': 'multipart/form-data' },
        })
      }
      setActionMessage(
        files.length === 1
          ? `文件 "${files[0].name}" 上传成功`
          : `${files.length} 个文件上传成功`,
      )
      await fetchData()
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : '上传失败'
      setActionMessage(message)
    } finally {
      setIsUploading(false)
    }
  }

  /**
   * 拖拽事件处理
   */
  const handleDragOver = (e: React.DragEvent) => {
    e.preventDefault()
    setIsDragging(true)
  }

  const handleDragLeave = (e: React.DragEvent) => {
    e.preventDefault()
    setIsDragging(false)
  }

  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault()
    setIsDragging(false)
    if (e.dataTransfer.files.length > 0) {
      handleUpload(e.dataTransfer.files)
    }
  }

  /**
   * 删除知识库条目
   *
   * Args:
   *   id: 条目 ID
   *   name: 条目名称
   */
  const handleDelete = async (id: string, name: string) => {
    setDeletingId(id)
    setActionMessage(null)
    try {
      await apiClient.delete(API_ENDPOINTS.KNOWLEDGE_BASE.DELETE(id))
      setActionMessage(`"${name}" 已删除`)
      setConfirmDeleteId(null)
      await fetchData()
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : '删除失败'
      setActionMessage(message)
    } finally {
      setDeletingId(null)
    }
  }

  /**
   * 创建分类
   */
  const handleCreateCategory = async () => {
    if (!newCategoryName.trim()) return
    setIsCreatingCategory(true)
    setActionMessage(null)
    try {
      await apiClient.post(API_ENDPOINTS.KNOWLEDGE_BASE.CREATE_CATEGORY, {
        name: newCategoryName.trim(),
      })
      setActionMessage(`分类 "${newCategoryName.trim()}" 创建成功`)
      setNewCategoryName('')
      setShowCategoryModal(false)
      await fetchData()
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : '创建分类失败'
      setActionMessage(message)
    } finally {
      setIsCreatingCategory(false)
    }
  }

  /**
   * 删除分类
   *
   * Args:
   *   name: 分类名称
   */
  const handleDeleteCategory = async (name: string) => {
    setActionMessage(null)
    try {
      await apiClient.delete(API_ENDPOINTS.KNOWLEDGE_BASE.DELETE_CATEGORY(name))
      setActionMessage(`分类 "${name}" 已删除`)
      setConfirmDeleteCategory(null)
      if (selectedCategory === name) {
        setSelectedCategory(null)
      }
      await fetchData()
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : '删除分类失败'
      setActionMessage(message)
    }
  }

  /** 根据分类筛选后的条目 */
  const filteredItems = selectedCategory
    ? items.filter((item) => item.categories?.includes(selectedCategory))
    : items

  return (
    <div className="bg-background text-foreground flex h-screen flex-col overflow-hidden">
      <header className="flex h-12 shrink-0 items-center border-b px-4">
        <a href="/" className="text-muted-foreground hover:text-foreground text-sm">
          &larr; 返回
        </a>
        <h1 className="ml-4 text-base font-semibold">知识库</h1>
        <span className="text-muted-foreground ml-auto text-xs">共 {items.length} 条</span>
      </header>
      <main className="flex flex-1 overflow-hidden">
        {/* 左侧：分类列表 */}
        <aside className="w-56 shrink-0 space-y-2 overflow-y-auto border-r p-4">
          <div className="flex items-center justify-between">
            <span className="text-muted-foreground text-xs font-medium">分类</span>
            <button
              onClick={() => setShowCategoryModal(true)}
              className="text-muted-foreground hover:text-foreground"
              title="新建分类"
            >
              <FolderPlus className="h-3.5 w-3.5" />
            </button>
          </div>

          <button
            onClick={() => setSelectedCategory(null)}
            className={`flex w-full items-center gap-2 rounded px-2 py-1.5 text-left text-xs transition-colors ${
              selectedCategory === null
                ? 'bg-primary/10 text-primary'
                : 'hover:bg-accent/30'
            }`}
          >
            <Folder className="h-3.5 w-3.5" />
            全部
          </button>

          {categories.map((cat) => (
            <div key={cat.name} className="group flex items-center">
              <button
                onClick={() => setSelectedCategory(cat.name)}
                className={`flex flex-1 items-center gap-2 rounded px-2 py-1.5 text-left text-xs transition-colors ${
                  selectedCategory === cat.name
                    ? 'bg-primary/10 text-primary'
                    : 'hover:bg-accent/30'
                }`}
              >
                <Folder className="h-3.5 w-3.5 shrink-0" />
                <span className="flex-1 truncate">{cat.name}</span>
                {cat.count !== undefined && (
                  <span className="text-muted-foreground text-xs">{cat.count}</span>
                )}
              </button>
              {confirmDeleteCategory === cat.name ? (
                <div className="flex items-center gap-0.5">
                  <button
                    onClick={() => handleDeleteCategory(cat.name)}
                    className="text-destructive text-xs"
                  >
                    ✓
                  </button>
                  <button
                    onClick={() => setConfirmDeleteCategory(null)}
                    className="text-muted-foreground text-xs"
                  >
                    ✕
                  </button>
                </div>
              ) : (
                <button
                  onClick={() => setConfirmDeleteCategory(cat.name)}
                  className="text-muted-foreground hover:text-destructive block md:hidden md:group-hover:block p-0.5"
                  title="删除分类"
                >
                  <X className="h-3 w-3" />
                </button>
              )}
            </div>
          ))}

          {/* 标签云 */}
          {tags.length > 0 && (
            <div className="mt-4">
              <span className="text-muted-foreground flex items-center gap-1 text-xs font-medium">
                <Tag className="h-3 w-3" />
                标签
              </span>
              <div className="mt-2 flex flex-wrap gap-1">
                {tags.map((tag) => (
                  <span
                    key={tag}
                    className="bg-primary/10 text-primary rounded px-1.5 py-0.5 text-xs"
                  >
                    {tag}
                  </span>
                ))}
              </div>
            </div>
          )}
        </aside>

        {/* 右侧：主内容区 */}
        <div className="flex-1 space-y-4 overflow-y-auto p-6">
          {/* 统计卡片 */}
          {stats && (
            <div className="grid grid-cols-3 gap-4">
              <div className="rounded-lg border p-4">
                <div className="text-muted-foreground mb-1 flex items-center gap-1.5 text-xs">
                  <FileText className="h-3.5 w-3.5" />
                  总条目
                </div>
                <div className="text-xl font-semibold">{stats.total}</div>
              </div>
              <div className="rounded-lg border p-4">
                <div className="text-muted-foreground mb-1 flex items-center gap-1.5 text-xs">
                  <Folder className="h-3.5 w-3.5" />
                  分类数
                </div>
                <div className="text-xl font-semibold">{stats.categories_count}</div>
              </div>
              <div className="rounded-lg border p-4">
                <div className="text-muted-foreground mb-1 flex items-center gap-1.5 text-xs">
                  <Tag className="h-3.5 w-3.5" />
                  标签数
                </div>
                <div className="text-xl font-semibold">{stats.tags_count}</div>
              </div>
            </div>
          )}

          {/* 文件上传区域 */}
          <div
            onDragOver={handleDragOver}
            onDragLeave={handleDragLeave}
            onDrop={handleDrop}
            onClick={() => fileInputRef.current?.click()}
            className={`cursor-pointer rounded-lg border-2 border-dashed p-6 text-center transition-colors ${
              isDragging
                ? 'border-primary bg-primary/5'
                : 'hover:border-primary/50 border-muted'
            }`}
          >
            <input
              ref={fileInputRef}
              type="file"
              multiple
              className="hidden"
              onChange={(e) => e.target.files && handleUpload(e.target.files)}
            />
            <Upload
              className={`mx-auto mb-2 h-8 w-8 ${
                isDragging ? 'text-primary' : 'text-muted-foreground/40'
              }`}
            />
            <p className="text-sm">
              {isUploading ? (
                <span className="text-primary">上传中...</span>
              ) : (
                <>
                  <span className="text-primary">点击上传</span>
                  <span className="text-muted-foreground"> 或拖拽文件到此区域</span>
                </>
              )}
            </p>
            <p className="text-muted-foreground mt-1 text-xs">
              支持多文件上传
            </p>
          </div>

          {/* 操作结果提示 */}
          {actionMessage && (
            <div
              className={`rounded-lg p-3 text-sm ${
                actionMessage.includes('失败')
                  ? 'bg-destructive/10 text-destructive'
                  : 'bg-status-success/10 text-status-success'
              }`}
            >
              {actionMessage}
            </div>
          )}

          {/* 错误状态 */}
          {error && (
            <div className="bg-destructive/10 text-destructive rounded-lg p-4 text-sm">
              {error}
            </div>
          )}

          {/* 加载状态 */}
          {isLoading && (
            <div className="flex items-center justify-center py-12">
              <div className="border-primary h-6 w-6 animate-spin rounded-full border-2 border-t-transparent" />
              <span className="text-muted-foreground ml-2 text-sm">加载中...</span>
            </div>
          )}

          {/* 空状态 */}
          {!isLoading && !error && filteredItems.length === 0 && (
            <div className="flex flex-col items-center justify-center py-16">
              <BookOpen className="text-muted-foreground/40 mb-3 h-12 w-12" />
              <p className="text-muted-foreground text-sm">
                {selectedCategory ? `"${selectedCategory}" 分类下暂无条目` : '知识库暂无条目'}
              </p>
              <p className="text-muted-foreground/60 mt-1 text-xs">
                上方拖拽文件或点击上传按钮添加知识库内容
              </p>
            </div>
          )}

          {/* 知识库条目列表 */}
          {!isLoading && !error && filteredItems.length > 0 && (
            <div className="space-y-3" aria-live="polite" aria-label="知识库列表">
              {filteredItems.map((item) => (
                <div key={item.id} className="rounded-lg border p-4">
                  <div className="mb-2 flex items-start justify-between">
                    <div className="flex items-center gap-2">
                      <FileText className="text-muted-foreground h-4 w-4 shrink-0" />
                      <h3 className="text-sm font-semibold">{item.name}</h3>
                    </div>
                    <div className="flex items-center gap-2">
                      <span className="text-muted-foreground text-xs">
                        {formatFileSize(item.size)}
                      </span>
                      {confirmDeleteId === item.id ? (
                        <div className="flex items-center gap-1">
                          <button
                            onClick={() => handleDelete(item.id, item.name)}
                            disabled={deletingId === item.id}
                            className="bg-destructive text-destructive-foreground rounded px-2 py-0.5 text-xs disabled:opacity-50"
                          >
                            确认
                          </button>
                          <button
                            onClick={() => setConfirmDeleteId(null)}
                            className="hover:bg-accent/50 rounded px-2 py-0.5 text-xs"
                          >
                            取消
                          </button>
                        </div>
                      ) : (
                        <button
                          onClick={() => setConfirmDeleteId(item.id)}
                          className="text-muted-foreground hover:text-destructive rounded p-1"
                          title="删除"
                        >
                          <Trash2 className="h-4 w-4" />
                        </button>
                      )}
                    </div>
                  </div>

                  <div className="flex flex-wrap items-center gap-2 text-xs">
                    {item.categories && item.categories.length > 0 && (
                      <div className="flex gap-1">
                        {item.categories.map((cat) => (
                          <span
                            key={cat}
                            className="bg-accent/30 text-muted-foreground rounded px-1.5 py-0.5"
                          >
                            {cat}
                          </span>
                        ))}
                      </div>
                    )}
                    {item.tags && item.tags.length > 0 && (
                      <div className="flex gap-1">
                        {item.tags.map((tag) => (
                          <span
                            key={tag}
                            className="bg-primary/10 text-primary rounded px-1.5 py-0.5"
                          >
                            {tag}
                          </span>
                        ))}
                      </div>
                    )}
                    {item.created_at && (
                      <span className="text-muted-foreground ml-auto">
                        {new Date(item.created_at).toLocaleString()}
                      </span>
                    )}
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      </main>

      {/* 创建分类模态框 */}
      {showCategoryModal && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50">
          <div className="bg-background w-full max-w-sm rounded-lg border p-6 shadow-lg">
            <div className="mb-4 flex items-center justify-between">
              <h2 className="text-base font-semibold">新建分类</h2>
              <button
                onClick={() => setShowCategoryModal(false)}
                className="text-muted-foreground hover:text-foreground"
              >
                <X className="h-4 w-4" />
              </button>
            </div>
            <div className="space-y-4">
              <div>
                <label className="text-muted-foreground mb-1 block text-xs">分类名称</label>
                <input
                  type="text"
                  value={newCategoryName}
                  onChange={(e) => setNewCategoryName(e.target.value)}
                  placeholder="输入分类名称"
                  onKeyDown={(e) => e.key === 'Enter' && handleCreateCategory()}
                  className="bg-background focus:ring-primary w-full rounded-lg border px-3 py-2 text-sm focus:ring-1 focus:outline-none"
                />
              </div>
              <div className="flex justify-end gap-2">
                <button
                  onClick={() => setShowCategoryModal(false)}
                  className="hover:bg-accent/50 rounded-lg border px-4 py-2 text-sm"
                >
                  取消
                </button>
                <button
                  onClick={handleCreateCategory}
                  disabled={isCreatingCategory || !newCategoryName.trim()}
                  className="bg-primary text-primary-foreground rounded-lg px-4 py-2 text-sm hover:opacity-90 disabled:opacity-50"
                >
                  {isCreatingCategory ? '创建中...' : '创建'}
                </button>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
