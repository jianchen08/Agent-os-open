/**
 * 模型选择面板（与提供商选择同屏，无弹窗）
 *
 * 两条等价的添加路径在同一界面内并排提供：
 * 1. 「拉取模型列表」——经 getRemoteModels 取该提供商可用模型，搜索 + 勾选（可多选）；
 * 2. 手填模型名——列表未包含的自定义模型（回车加入，可累计多条）。
 * 两条路径产出都汇入一次「添加模型」提交，添加载荷与原先弹窗形态一致
 * （远端模型带上 litellm 注册表下发的事实上限，手填模型走兜底上限）。
 */
import { useEffect, useState } from 'react'
import { Loader2, RefreshCw, Search, X } from '@/assets/icons'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { getRemoteModels, type RemoteModel } from '@/services/api/config'

/** 从被 reject 的对象中提取后端错误消息（apiClient 拦截器构造普通 ApiError，非 Error 实例）。 */
const getApiMsg = (e: unknown, fallback = '操作失败'): string =>
  (e as { message?: string })?.message ?? fallback

export function ModelPickerPanel({
  providerId,
  providerLabel,
  canPull,
  adding,
  onAdd,
}: {
  providerId: string
  /** 提供商显示名（文案用，缺省回退 providerId） */
  providerLabel?: string
  /** 该提供商是否具备拉取条件（已配置 Key 或无需 Key） */
  canPull: boolean
  /** 添加提交进行中 */
  adding: boolean
  onAdd: (
    providerId: string,
    modelNames: string[],
    limits: Map<string, RemoteModel>,
  ) => Promise<void>
}) {
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [models, setModels] = useState<RemoteModel[]>([])
  const [pulled, setPulled] = useState(false)
  const [search, setSearch] = useState('')
  const [checked, setChecked] = useState<Set<string>>(new Set())
  const [customs, setCustoms] = useState<string[]>([])
  const [customInput, setCustomInput] = useState('')

  // 切换提供商：清空上一家的拉取结果与待添加清单（模型归属随提供商走）
  useEffect(() => {
    setLoading(false)
    setError(null)
    setModels([])
    setPulled(false)
    setSearch('')
    setChecked(new Set())
    setCustoms([])
    setCustomInput('')
  }, [providerId])

  const pull = () => {
    setLoading(true)
    setError(null)
    setChecked(new Set())
    getRemoteModels(providerId)
      .then(({ models: remote }) => {
        setModels(remote)
        setPulled(true)
      })
      .catch((e) => setError(getApiMsg(e, '拉取模型列表失败')))
      .finally(() => setLoading(false))
  }

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
    if (pendingCount === 0) return
    const limits = new Map(models.map((m) => [m.id, m]))
    await onAdd(providerId, [...checked, ...customs], limits)
    // 提交后清空待添加清单（失败重试由 toast 提示，勾选结果不残留成重复提交源）
    setChecked(new Set())
    setCustoms([])
  }

  const label = providerLabel ?? providerId

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <Button size="xs" variant="outline" onClick={pull} disabled={!canPull || loading}>
          {loading ? (
            <Loader2 className="mr-1 h-3 w-3 animate-spin" />
          ) : (
            <RefreshCw className="mr-1 h-3 w-3" />
          )}
          {loading ? '拉取中...' : `从 ${label} 拉取模型列表`}
        </Button>
        {!canPull && (
          <span className="text-muted-foreground text-xs">
            该提供商尚未配置 Key，可先在上方填入，或直接手填模型名添加
          </span>
        )}
      </div>

      {error && (
        <div className="bg-status-warning/10 text-status-warning rounded-lg px-3 py-2 text-xs">
          {error}
          <p className="text-muted-foreground mt-1">仍可在下方手动输入模型名添加。</p>
        </div>
      )}

      {pulled && !loading && !error && (
        <>
          <div className="relative">
            <Search className="text-muted-foreground absolute top-1/2 left-2.5 h-3.5 w-3.5 -translate-y-1/2" />
            <Input
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder={`搜索 ${models.length} 个模型...`}
              className="h-8 pl-8 text-xs"
              aria-label="搜索模型"
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
                    <span className="text-muted-foreground ml-auto text-[10px]">{m.owned_by}</span>
                  )}
                </label>
              ))
            )}
          </div>
        </>
      )}

      {/* 手填模型名：列表未包含的自定义模型 */}
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
            placeholder="自定义模型名（回车加入，适用于列表未包含的模型）"
            className="h-8 text-xs"
            aria-label="自定义模型名"
          />
          <Button size="xs" variant="outline" onClick={addCustom} disabled={!customInput.trim()}>
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

      <div className="flex flex-wrap items-center gap-2 border-t pt-3">
        <Button size="sm" onClick={handleAdd} disabled={pendingCount === 0 || adding}>
          {adding ? (
            <>
              <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" />
              添加中...
            </>
          ) : (
            `添加模型${pendingCount > 0 ? ` (${pendingCount})` : ''}`
          )}
        </Button>
        <span className="text-muted-foreground text-xs">
          待添加 {pendingCount} 个；添加后可在下方「已注册模型」展开参数设置上下文/think
        </span>
      </div>
    </div>
  )
}
