/**
 * 文档库分区（记忆页两分区之一）
 *
 * 标签云 + 统计卡 + 上传区 + 条目列表（平铺，条目以标签 chip 展示）。
 * query 化数据（useKnowledgeBaseQuery 缓存 SWR，重挂零请求）；
 * upload/delete 后 invalidateKnowledgeBaseCache 整体失效。
 */

import { useState, useRef } from 'react'
import {
  BookOpen,
  Upload,
  Trash2,
  Tag,
  FileText,
} from '@/assets/icons'
import { EmptyState } from '@/components/shared/EmptyState'
import { ErrorState } from '@/components/shared/ErrorState'
import { LoadingState } from '@/components/shared/LoadingState'
import { API_ENDPOINTS } from '@/constants/api'
import {
  invalidateKnowledgeBaseCache,
  useKnowledgeBaseQuery,
} from '@/hooks/queries/useKnowledgeBaseQuery'
import apiClient from '@/services/api/client'
import { formatFileSize } from '@/utils/format'

/**
 * 文档库分区组件
 */
export function KnowledgeBaseContent() {
  const [actionMessage, setActionMessage] = useState<{
    text: string
    ok: boolean
  } | null>(null)

  const [isUploading, setIsUploading] = useState(false)
  const [isDragging, setIsDragging] = useState(false)
  const fileInputRef = useRef<HTMLInputElement>(null)

  const [confirmDeleteId, setConfirmDeleteId] = useState<string | null>(null)
  const [deletingId, setDeletingId] = useState<string | null>(null)

  // 全部数据（query 化）：staleTime 窗口内重挂零请求；写操作后 invalidate
  const kbQuery = useKnowledgeBaseQuery()
  const items = kbQuery.data?.items ?? []
  const stats = kbQuery.data?.stats ?? null
  const tags = kbQuery.data?.tags ?? []
  // 无缓存数据时显示 loading（有缓存先渲染缓存不闪 loading）
  const isLoading = kbQuery.isPending && !kbQuery.data
  const error = kbQuery.isError
    ? kbQuery.error instanceof Error
      ? kbQuery.error.message
      : '加载数据失败'
    : null

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
      setActionMessage({
        text:
          files.length === 1
            ? `文件 "${files[0].name}" 上传成功`
            : `${files.length} 个文件上传成功`,
        ok: true,
      })
      invalidateKnowledgeBaseCache()
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : '上传失败'
      setActionMessage({ text: message, ok: false })
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
      setActionMessage({ text: `"${name}" 已删除`, ok: true })
      setConfirmDeleteId(null)
      invalidateKnowledgeBaseCache()
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : '删除失败'
      setActionMessage({ text: message, ok: false })
    } finally {
      setDeletingId(null)
    }
  }

  return (
    <>
      <div className="flex h-full min-h-0 overflow-hidden">
        {/* 左侧：标签云 */}
        <aside className="w-56 shrink-0 space-y-2 overflow-y-auto border-r p-4">
          {tags.length > 0 && (
            <div>
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
          {/* 条目总数 */}
          <div className="text-muted-foreground text-right text-xs">共 {items.length} 条</div>

          {/* 统计卡片 */}
          {stats && (
            <div className="grid grid-cols-2 gap-4">
              <div className="rounded-lg border p-4">
                <div className="text-muted-foreground mb-1 flex items-center gap-1.5 text-xs">
                  <FileText className="h-3.5 w-3.5" />
                  总条目
                </div>
                <div className="text-xl font-semibold">{stats.total}</div>
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
              role="status"
              data-ok={actionMessage.ok}
              className={`rounded-lg p-3 text-sm ${
                actionMessage.ok
                  ? 'bg-status-success/10 text-status-success'
                  : 'bg-destructive/10 text-destructive'
              }`}
            >
              {actionMessage.text}
            </div>
          )}

          {/* 错误状态 */}
          {error && <ErrorState message={error} />}

          {/* 加载状态 */}
          {isLoading && <LoadingState />}

          {/* 空状态 */}
          {!isLoading && !error && items.length === 0 && (
            <EmptyState
              icon={BookOpen}
              title="知识库暂无条目"
              description="上方拖拽文件或点击上传按钮添加知识库内容"
            />
          )}

          {/* 知识库条目列表 */}
          {!isLoading && !error && items.length > 0 && (
            <div className="space-y-3" aria-live="polite" aria-label="知识库列表">
              {items.map((item) => (
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
                            className="hover:bg-muted/50 rounded px-2 py-0.5 text-xs"
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
      </div>
    </>
  )
}
