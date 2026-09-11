/**
 * 拉取模型对话框：远端列表勾选 + 自定义输入，批量写入 llm.yaml。
 *
 * 自 LlmSettingsPage 抽出（冻结巨型文件只许缩小不许增长，批次F门禁）。
 */
import { useState, useEffect } from 'react'
import { Loader2, Search, X } from '@/assets/icons'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { getRemoteModels, type RemoteModel } from '@/services/api/config'

/** 从被 reject 的对象中提取后端错误消息（apiClient 拦截器构造普通 ApiError，非 Error 实例）。 */
const getApiMsg = (e: unknown, fallback = '操作失败'): string =>
  (e as { message?: string })?.message ?? fallback

/** 拉取模型对话框：远端列表勾选 + 自定义输入，批量写入 llm.yaml */
export function FetchModelsModal({
  open,
  providerId,
  onClose,
  onAdd,
}: {
  open: boolean
  providerId: string | null
  onClose: () => void
  onAdd: (
    providerId: string,
    modelNames: string[],
    limits: Map<string, RemoteModel>,
  ) => Promise<void>
}) {
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [models, setModels] = useState<RemoteModel[]>([])
  const [search, setSearch] = useState('')
  const [checked, setChecked] = useState<Set<string>>(new Set())
  const [customs, setCustoms] = useState<string[]>([])
  const [customInput, setCustomInput] = useState('')
  const [adding, setAdding] = useState(false)

  useEffect(() => {
    if (!open || !providerId) return
    setLoading(true)
    setError(null)
    setModels([])
    setSearch('')
    setChecked(new Set())
    setCustoms([])
    setCustomInput('')
    getRemoteModels(providerId)
      .then(({ models: remote }) => setModels(remote))
      .catch((e) => setError(getApiMsg(e, '拉取模型列表失败')))
      .finally(() => setLoading(false))
  }, [open, providerId])

  const filtered = models.filter((m) => m.id.toLowerCase().includes(search.toLowerCase()))
  const pendingCount = checked.size + customs.length

  const toggle = (id: string) => {
    setChecked((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  const addCustom = () => {
    const v = customInput.trim()
    if (!v) return
    if (!customs.includes(v) && !checked.has(v)) setCustoms((prev) => [...prev, v])
    setCustomInput('')
  }

  const handleAdd = async () => {
    if (!providerId || pendingCount === 0) return
    setAdding(true)
    try {
      const limits = new Map(models.map((m) => [m.id, m]))
      await onAdd(providerId, [...checked, ...customs], limits)
      onClose()
    } finally {
      setAdding(false)
    }
  }

  return (
    // 原 ui/Modal（maxWidth="lg"）迁移：width=32rem 等价 max-w-lg 面板宽度；
    // DialogContent 已内建「外点不关」（对齐 Modal closeOnBackdropClick 缺省 false）
    <Dialog
      open={open}
      onOpenChange={(o) => {
        if (!o) onClose()
      }}
    >
      <DialogContent width="32rem">
        <DialogHeader className="border-b pb-6">
          <DialogTitle>拉取模型 — {providerId ?? ''}</DialogTitle>
        </DialogHeader>
        <div className="p-6">
          <div className="space-y-3">
            {loading && (
              <div className="text-muted-foreground flex items-center justify-center py-8 text-sm">
                <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                正在从提供者 API 拉取模型列表...
              </div>
            )}
            {error && (
              <div className="bg-status-warning/10 text-status-warning rounded-lg px-3 py-2 text-xs">
                {error}
                <p className="text-muted-foreground mt-1">仍可在下方手动输入模型名添加。</p>
              </div>
            )}

            {!loading && !error && (
              <>
                <div className="relative">
                  <Search className="text-muted-foreground absolute top-1/2 left-2.5 h-3.5 w-3.5 -translate-y-1/2" />
                  <Input
                    value={search}
                    onChange={(e) => setSearch(e.target.value)}
                    placeholder={`搜索 ${models.length} 个模型...`}
                    className="h-8 pl-8 text-xs"
                  />
                </div>
                <div className="max-h-64 space-y-1 overflow-y-auto rounded-lg border p-2">
                  {filtered.length === 0 ? (
                    <div className="text-muted-foreground py-4 text-center text-xs">
                      没有匹配的模型，可在下方手动输入
                    </div>
                  ) : (
                    filtered.map((m) => (
                      <label
                        key={m.id}
                        className="hover:bg-muted/50 flex cursor-pointer items-center gap-2 rounded px-2 py-1"
                      >
                        <input
                          type="checkbox"
                          checked={checked.has(m.id)}
                          onChange={() => toggle(m.id)}
                          className="border-border h-3.5 w-3.5"
                        />
                        <span className="text-xs">{m.id}</span>
                        {m.owned_by && (
                          <span className="text-muted-foreground ml-auto text-[10px]">
                            {m.owned_by}
                          </span>
                        )}
                      </label>
                    ))
                  )}
                </div>
              </>
            )}

            {/* 自定义输入：列表里没有的模型手动加 */}
            <div className="space-y-1.5">
              <div className="flex items-center gap-2">
                <Input
                  value={customInput}
                  onChange={(e) => setCustomInput(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter') {
                      e.preventDefault()
                      addCustom()
                    }
                  }}
                  placeholder="自定义模型名（回车添加，适用于列表未包含的新模型）"
                  className="h-8 text-xs"
                />
                <Button
                  size="xs"
                  variant="outline"
                  onClick={addCustom}
                  disabled={!customInput.trim()}
                >
                  加入
                </Button>
              </div>
              {customs.length > 0 && (
                <div className="flex flex-wrap gap-1.5">
                  {customs.map((name) => (
                    <span
                      key={name}
                      className="bg-muted flex items-center gap-1 rounded px-2 py-0.5 font-mono text-xs"
                    >
                      {name}
                      <button
                        type="button"
                        onClick={() => setCustoms((prev) => prev.filter((n) => n !== name))}
                        className="text-muted-foreground hover:text-foreground"
                        aria-label={`移除 ${name}`}
                      >
                        <X className="h-3 w-3" />
                      </button>
                    </span>
                  ))}
                </div>
              )}
            </div>

            <div className="flex items-center justify-end gap-2 border-t pt-3">
              <span className="text-muted-foreground mr-auto text-xs">
                待添加 {pendingCount} 个；添加后可在「模型」页展开参数设置上下文/think
              </span>
              <Button size="sm" variant="outline" onClick={onClose}>
                取消
              </Button>
              <Button size="sm" onClick={handleAdd} disabled={pendingCount === 0 || adding}>
                {adding ? (
                  <>
                    <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" />
                    添加中...
                  </>
                ) : (
                  `添加所选 (${pendingCount})`
                )}
              </Button>
            </div>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  )
}
