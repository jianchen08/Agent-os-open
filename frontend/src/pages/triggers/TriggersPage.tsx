/**
 * 触发器管理页面
 *
 * 展示触发器列表，支持创建、编辑、删除、启用/禁用和手动触发
 */

import {
  Zap,
  Plus,
  Trash2,
  Play,
  ToggleLeft,
  ToggleRight,
  X,
  BarChart3,
} from 'lucide-react'
import { useState, useEffect, useCallback } from 'react'
import apiClient from '@/services/api/client'
import { API_ENDPOINTS } from '@/constants/api'

/** 触发器信息 */
interface TriggerItem {
  id: string
  name: string
  type: string
  enabled: boolean
  config: Record<string, unknown>
  created_at?: string
  updated_at?: string
}

/** 触发器统计 */
interface TriggerStats {
  total: number
  enabled: number
  disabled: number
  [key: string]: unknown
}

/** 创建/编辑表单数据 */
interface TriggerFormData {
  name: string
  type: string
  config: string
}

/**
 * 获取类型标签样式
 *
 * Args:
 *   type: 触发器类型
 *
 * Returns:
 *   Tailwind CSS 类名字符串
 */
function getTypeBadgeStyle(type: string): string {
  const styles: Record<string, string> = {
    cron: 'bg-blue-500/10 text-blue-500',
    event: 'bg-purple-500/10 text-purple-500',
    webhook: 'bg-orange-500/10 text-orange-500',
    manual: 'bg-green-500/10 text-green-500',
  }
  return styles[type] || 'bg-accent/30 text-muted-foreground'
}

const EMPTY_FORM: TriggerFormData = { name: '', type: 'cron', config: '{}' }

/**
 * 触发器管理页面组件
 */
export function TriggersPage() {
  const [triggers, setTriggers] = useState<TriggerItem[]>([])
  const [stats, setStats] = useState<TriggerStats | null>(null)
  const [isLoading, setIsLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [actionMessage, setActionMessage] = useState<string | null>(null)

  // 模态框状态
  const [showModal, setShowModal] = useState(false)
  const [editingTrigger, setEditingTrigger] = useState<TriggerItem | null>(null)
  const [formData, setFormData] = useState<TriggerFormData>({ ...EMPTY_FORM })
  const [isSubmitting, setIsSubmitting] = useState(false)

  // 操作中状态
  const [triggeringId, setTriggeringId] = useState<string | null>(null)
  const [deletingId, setDeletingId] = useState<string | null>(null)

  // 删除确认
  const [confirmDeleteId, setConfirmDeleteId] = useState<string | null>(null)

  /**
   * 加载触发器列表和统计
   */
  const fetchData = useCallback(async () => {
    setIsLoading(true)
    setError(null)
    try {
      const [triggersRes, statsRes] = await Promise.allSettled([
        apiClient.get<TriggerItem[]>(API_ENDPOINTS.TRIGGERS.LIST),
        apiClient.get<TriggerStats>(API_ENDPOINTS.TRIGGERS.STATS),
      ])
      if (triggersRes.status === 'fulfilled') {
        setTriggers(triggersRes.value.data)
      } else {
        setError('获取触发器列表失败')
      }
      if (statsRes.status === 'fulfilled') {
        setStats(statsRes.value.data)
      }
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
   * 打开创建模态框
   */
  const handleOpenCreate = () => {
    setEditingTrigger(null)
    setFormData({ ...EMPTY_FORM })
    setShowModal(true)
  }

  /**
   * 打开编辑模态框
   *
   * Args:
   *   trigger: 要编辑的触发器
   */
  const handleOpenEdit = (trigger: TriggerItem) => {
    setEditingTrigger(trigger)
    setFormData({
      name: trigger.name,
      type: trigger.type,
      config: JSON.stringify(trigger.config, null, 2),
    })
    setShowModal(true)
  }

  /**
   * 提交创建或编辑
   */
  const handleSubmit = async () => {
    if (!formData.name.trim() || !formData.type.trim()) return

    setIsSubmitting(true)
    setActionMessage(null)
    try {
      let configObj: Record<string, unknown> = {}
      try {
        configObj = JSON.parse(formData.config)
      } catch {
        setActionMessage('配置 JSON 格式无效')
        setIsSubmitting(false)
        return
      }

      if (editingTrigger) {
        await apiClient.put(API_ENDPOINTS.TRIGGERS.UPDATE(editingTrigger.id), {
          name: formData.name,
          type: formData.type,
          config: configObj,
        })
        setActionMessage(`触发器 "${formData.name}" 更新成功`)
      } else {
        await apiClient.post(API_ENDPOINTS.TRIGGERS.CREATE, {
          name: formData.name,
          type: formData.type,
          config: configObj,
        })
        setActionMessage(`触发器 "${formData.name}" 创建成功`)
      }
      setShowModal(false)
      await fetchData()
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : '操作失败'
      setActionMessage(message)
    } finally {
      setIsSubmitting(false)
    }
  }

  /**
   * 切换启用/禁用
   *
   * Args:
   *   trigger: 目标触发器
   */
  const handleToggleEnabled = async (trigger: TriggerItem) => {
    setActionMessage(null)
    try {
      const endpoint = trigger.enabled
        ? API_ENDPOINTS.TRIGGERS.DISABLE(trigger.id)
        : API_ENDPOINTS.TRIGGERS.ENABLE(trigger.id)
      await apiClient.post(endpoint)
      setActionMessage(
        `触发器 "${trigger.name}" 已${trigger.enabled ? '禁用' : '启用'}`,
      )
      await fetchData()
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : '操作失败'
      setActionMessage(message)
    }
  }

  /**
   * 手动触发
   *
   * Args:
   *   triggerId: 触发器 ID
   *   triggerName: 触发器名称（用于提示信息）
   */
  const handleTrigger = async (triggerId: string, triggerName: string) => {
    setTriggeringId(triggerId)
    setActionMessage(null)
    try {
      await apiClient.post(API_ENDPOINTS.TRIGGERS.TRIGGER(triggerId))
      setActionMessage(`触发器 "${triggerName}" 已执行`)
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : '触发失败'
      setActionMessage(message)
    } finally {
      setTriggeringId(null)
    }
  }

  /**
   * 删除触发器
   *
   * Args:
   *   triggerId: 触发器 ID
   *   triggerName: 触发器名称
   */
  const handleDelete = async (triggerId: string, triggerName: string) => {
    setDeletingId(triggerId)
    setActionMessage(null)
    try {
      await apiClient.delete(API_ENDPOINTS.TRIGGERS.DELETE(triggerId))
      setActionMessage(`触发器 "${triggerName}" 已删除`)
      setConfirmDeleteId(null)
      await fetchData()
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : '删除失败'
      setActionMessage(message)
    } finally {
      setDeletingId(null)
    }
  }

  return (
    <div className="bg-background text-foreground flex h-screen flex-col overflow-hidden">
      <header className="flex h-12 shrink-0 items-center border-b px-4">
        <a href="/" className="text-muted-foreground hover:text-foreground text-sm">
          &larr; 返回
        </a>
        <h1 className="ml-4 text-base font-semibold">触发器管理</h1>
        <span className="text-muted-foreground ml-auto text-xs">
          共 {triggers.length} 个触发器
        </span>
      </header>
      <main className="flex-1 space-y-4 overflow-y-auto p-6">
        {/* 统计卡片 */}
        {stats && (
          <div className="grid grid-cols-3 gap-4">
            <div className="rounded-lg border p-4">
              <div className="text-muted-foreground mb-1 flex items-center gap-1.5 text-xs">
                <BarChart3 className="h-3.5 w-3.5" />
                总触发器
              </div>
              <div className="text-xl font-semibold">{stats.total}</div>
            </div>
            <div className="rounded-lg border p-4">
              <div className="text-muted-foreground mb-1 text-xs">已启用</div>
              <div className="text-status-success text-xl font-semibold">{stats.enabled}</div>
            </div>
            <div className="rounded-lg border p-4">
              <div className="text-muted-foreground mb-1 text-xs">已禁用</div>
              <div className="text-status-warning text-xl font-semibold">{stats.disabled}</div>
            </div>
          </div>
        )}

        {/* 操作按钮 */}
        <div className="flex items-center gap-3">
          <button
            onClick={handleOpenCreate}
            className="bg-primary text-primary-foreground flex items-center gap-1.5 rounded-lg px-4 py-1.5 text-sm hover:opacity-90"
          >
            <Plus className="h-3.5 w-3.5" />
            创建触发器
          </button>
          <button
            onClick={fetchData}
            disabled={isLoading}
            className="hover:bg-accent/50 rounded-lg border px-3 py-1.5 text-sm disabled:opacity-50"
          >
            刷新
          </button>
        </div>

        {/* 操作结果提示 */}
        {actionMessage && (
          <div
            className={`rounded-lg p-3 text-sm ${
              actionMessage.includes('失败') || actionMessage.includes('无效')
                ? 'bg-destructive/10 text-destructive'
                : 'bg-status-success/10 text-status-success'
            }`}
          >
            {actionMessage}
          </div>
        )}

        {/* 错误状态 */}
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

        {/* 空状态 */}
        {!isLoading && !error && triggers.length === 0 && (
          <div className="flex flex-col items-center justify-center py-16">
            <Zap className="text-muted-foreground/40 mb-3 h-12 w-12" />
            <p className="text-muted-foreground text-sm">暂无触发器</p>
            <p className="text-muted-foreground/60 mt-1 text-xs">
              点击上方"创建触发器"按钮添加第一个触发器
            </p>
          </div>
        )}

        {/* 触发器列表 */}
        {!isLoading && !error && triggers.length > 0 && (
          <div className="space-y-3" aria-live="polite" aria-label="触发器列表">
            {triggers.map((trigger) => (
              <div key={trigger.id} className="rounded-lg border p-4">
                <div className="mb-2 flex items-center justify-between">
                  <div className="flex items-center gap-2">
                    <h3 className="text-sm font-semibold">{trigger.name}</h3>
                    <span
                      className={`rounded-full px-2 py-0.5 text-xs ${getTypeBadgeStyle(trigger.type)}`}
                    >
                      {trigger.type}
                    </span>
                  </div>
                  <div className="flex items-center gap-2">
                    {/* 启用/禁用切换 */}
                    <button
                      onClick={() => handleToggleEnabled(trigger)}
                      className="flex items-center"
                      title={trigger.enabled ? '点击禁用' : '点击启用'}
                    >
                      {trigger.enabled ? (
                        <ToggleRight className="text-status-success h-5 w-5" />
                      ) : (
                        <ToggleLeft className="text-muted-foreground h-5 w-5" />
                      )}
                    </button>

                    {/* 手动触发 */}
                    <button
                      onClick={() => handleTrigger(trigger.id, trigger.name)}
                      disabled={triggeringId === trigger.id}
                      className="hover:bg-accent/50 rounded p-1 disabled:opacity-50"
                      title="手动触发"
                    >
                      <Play
                        className={`h-4 w-4 ${triggeringId === trigger.id ? 'animate-pulse' : ''}`}
                      />
                    </button>

                    {/* 编辑 */}
                    <button
                      onClick={() => handleOpenEdit(trigger)}
                      className="hover:bg-accent/50 rounded p-1"
                      title="编辑"
                    >
                      <Zap className="h-4 w-4" />
                    </button>

                    {/* 删除 */}
                    {confirmDeleteId === trigger.id ? (
                      <div className="flex items-center gap-1">
                        <button
                          onClick={() => handleDelete(trigger.id, trigger.name)}
                          disabled={deletingId === trigger.id}
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
                        onClick={() => setConfirmDeleteId(trigger.id)}
                        className="hover:bg-accent/50 text-muted-foreground rounded p-1 hover:text-destructive"
                        title="删除"
                      >
                        <Trash2 className="h-4 w-4" />
                      </button>
                    )}
                  </div>
                </div>

                <div className="text-muted-foreground flex items-center gap-3 text-xs">
                  <span>ID: {trigger.id}</span>
                  <span className={trigger.enabled ? 'text-status-success' : 'text-status-warning'}>
                    {trigger.enabled ? '已启用' : '已禁用'}
                  </span>
                  {trigger.updated_at && (
                    <span>更新于 {new Date(trigger.updated_at).toLocaleString()}</span>
                  )}
                </div>

                {/* 配置预览 */}
                {trigger.config && Object.keys(trigger.config).length > 0 && (
                  <pre className="bg-accent/20 mt-2 overflow-x-auto rounded p-2 text-xs">
                    {JSON.stringify(trigger.config, null, 2)}
                  </pre>
                )}
              </div>
            ))}
          </div>
        )}
      </main>

      {/* 创建/编辑模态框 */}
      {showModal && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50">
          <div className="bg-background w-full max-w-md rounded-lg border p-6 shadow-lg">
            <div className="mb-4 flex items-center justify-between">
              <h2 className="text-base font-semibold">
                {editingTrigger ? '编辑触发器' : '创建触发器'}
              </h2>
              <button
                onClick={() => setShowModal(false)}
                className="text-muted-foreground hover:text-foreground"
              >
                <X className="h-4 w-4" />
              </button>
            </div>

            <div className="space-y-4">
              <div>
                <label className="text-muted-foreground mb-1 block text-xs">名称</label>
                <input
                  type="text"
                  value={formData.name}
                  onChange={(e) => setFormData({ ...formData, name: e.target.value })}
                  placeholder="输入触发器名称"
                  className="bg-background focus:ring-primary w-full rounded-lg border px-3 py-2 text-sm focus:ring-1 focus:outline-none"
                />
              </div>

              <div>
                <label className="text-muted-foreground mb-1 block text-xs">类型</label>
                <select
                  value={formData.type}
                  onChange={(e) => setFormData({ ...formData, type: e.target.value })}
                  className="bg-background w-full rounded-lg border px-3 py-2 text-sm"
                >
                  <option value="cron">定时 (cron)</option>
                  <option value="event">事件 (event)</option>
                  <option value="webhook">Webhook</option>
                  <option value="manual">手动 (manual)</option>
                </select>
              </div>

              <div>
                <label className="text-muted-foreground mb-1 block text-xs">配置 (JSON)</label>
                <textarea
                  value={formData.config}
                  onChange={(e) => setFormData({ ...formData, config: e.target.value })}
                  placeholder='{"key": "value"}'
                  rows={6}
                  className="bg-background focus:ring-primary w-full rounded-lg border px-3 py-2 font-mono text-xs focus:ring-1 focus:outline-none"
                />
              </div>

              <div className="flex justify-end gap-2">
                <button
                  onClick={() => setShowModal(false)}
                  className="hover:bg-accent/50 rounded-lg border px-4 py-2 text-sm"
                >
                  取消
                </button>
                <button
                  onClick={handleSubmit}
                  disabled={isSubmitting || !formData.name.trim()}
                  className="bg-primary text-primary-foreground rounded-lg px-4 py-2 text-sm hover:opacity-90 disabled:opacity-50"
                >
                  {isSubmitting
                    ? '提交中...'
                    : editingTrigger
                      ? '保存'
                      : '创建'}
                </button>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
