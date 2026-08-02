/**
 * 插件配置内联编辑器
 *
 * 对接 0.2 内核：
 * - GET/PUT /api/v1/plugins/{id}/config/{file_id}
 * 可嵌入设置页右侧面板，也可作为独立内容渲染。
 */

import { useCallback, useEffect, useState } from 'react'
import { Loader2, Plus, Trash2 } from '@/assets/icons'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { toast } from '@/components/ui/sonner'
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogFooter,
} from '@/components/ui/dialog'
import {
  getPluginConfigFile,
  savePluginConfigFile,
  isPluginConfigConflict,
} from '@/services/api/pluginConfig'

export interface PluginConfigEditorProps {
  pluginId: string
  fileId: string
  title: string
  description?: string
  /** 嵌入模式：不渲染独立页面头，由外层布局控制 */
  embedded?: boolean
}

type SaveState = 'idle' | 'saving' | 'saved' | 'error'

/** 嵌套设值 */
function setNestedValue(
  obj: Record<string, unknown>,
  path: string[],
  value: unknown,
): Record<string, unknown> {
  if (path.length === 0) return obj
  const [head, ...rest] = path
  const next = { ...obj }
  if (rest.length === 0) {
    next[head] = value
    return next
  }
  const child = (next[head] as Record<string, unknown>) || {}
  next[head] = setNestedValue({ ...child }, rest, value)
  return next
}

function keyToLabel(key: string): string {
  return key
    .replace(/_/g, ' ')
    .replace(/([a-z])([A-Z])/g, '$1 $2')
    .replace(/\b\w/g, (c) => c.toUpperCase())
}

/**
 * 插件配置编辑器：读配置 → 表单 → 乐观锁写回。
 */
export function PluginConfigEditor({
  pluginId,
  fileId,
  title,
  description,
  embedded = false,
}: PluginConfigEditorProps) {
  const [config, setConfig] = useState<Record<string, unknown> | null>(null)
  const [etag, setEtag] = useState('')
  const [isLoading, setIsLoading] = useState(true)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [saveState, setSaveState] = useState<SaveState>('idle')

  useEffect(() => {
    let cancelled = false
    setIsLoading(true)
    setLoadError(null)
    setConfig(null)

    getPluginConfigFile(pluginId, fileId)
      .then((result) => {
        if (cancelled) return
        setConfig(result.data.data ?? {})
        setEtag(result.etag)
      })
      .catch((error: unknown) => {
        if (cancelled) return
        const msg = error instanceof Error ? error.message : '无法加载配置'
        setConfig({})
        setLoadError(msg)
        toast.error('配置加载失败', { description: msg })
      })
      .finally(() => {
        if (!cancelled) setIsLoading(false)
      })

    return () => {
      cancelled = true
    }
  }, [pluginId, fileId])

  const handleChange = useCallback((path: string[], value: unknown) => {
    setConfig((prev) => {
      if (!prev) return prev
      return setNestedValue(prev, path, value)
    })
  }, [])

  const handleDelete = useCallback((path: string[]) => {
    setConfig((prev) => {
      if (!prev) return prev
      // 从嵌套对象里删掉 path 指向的 key
      const root = structuredClone(prev)
      let target: Record<string, unknown> = root
      for (let i = 0; i < path.length - 1; i++) {
        target = target[path[i]] as Record<string, unknown>
      }
      delete target[path[path.length - 1]]
      return root
    })
  }, [])

  const handleAddField = useCallback((parentPath: string[], key: string, value: unknown) => {
    setConfig((prev) => {
      if (!prev) return prev
      const root = structuredClone(prev)
      let target: Record<string, unknown> = root
      for (const seg of parentPath) {
        target = target[seg] as Record<string, unknown>
      }
      target[key] = value
      return root
    })
  }, [])

  const handleSave = useCallback(async () => {
    if (!config) return
    setSaveState('saving')
    try {
      const result = await savePluginConfigFile(pluginId, fileId, config, etag || undefined)
      setEtag(result.etag)
      setSaveState('saved')
      toast.success('配置已保存')
      setTimeout(() => setSaveState('idle'), 2000)
    } catch (error: unknown) {
      if (isPluginConfigConflict(error)) {
        setSaveState('error')
        toast.error('配置冲突', {
          description: '配置已被他人修改，请刷新后重试',
        })
        if (error.currentEtag) setEtag(error.currentEtag)
        return
      }
      const msg = error instanceof Error ? error.message : '保存配置时发生错误'
      setSaveState('error')
      toast.error('配置保存失败', { description: msg })
    }
  }, [config, pluginId, fileId, etag])

  const body = (
    <>
      {isLoading && (
        <div className="text-muted-foreground flex items-center justify-center py-20 text-sm">
          <div className="border-primary mr-2 h-5 w-5 animate-spin rounded-full border-2 border-t-transparent" />
          加载配置...
        </div>
      )}

      {!isLoading && loadError && (
        <div className="mb-4 rounded-lg bg-status-warning/10 px-3 py-2 text-xs text-status-warning">
          {loadError}
        </div>
      )}

      {!isLoading && config && (
        <>
          <ConfigObject obj={config} parentPath={[]} onChange={handleChange} onDelete={handleDelete} onAddField={handleAddField} allowAddField />
          <div className="mt-6 flex items-center gap-3 border-t pt-4">
            <Button onClick={handleSave} disabled={saveState === 'saving'}>
              {saveState === 'saving' ? (
                <>
                  <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" />
                  保存中...
                </>
              ) : (
                '保存配置'
              )}
            </Button>
            {saveState === 'saved' && (
              <span className="text-xs text-status-success" role="status">
                已保存
              </span>
            )}
            {saveState === 'error' && (
              <span className="text-xs text-status-error" role="alert">
                保存失败
              </span>
            )}
          </div>
        </>
      )}
    </>
  )

  if (embedded) {
    return (
      <div className="flex h-full flex-col">
        <div className="mb-4 shrink-0">
          <h2 className="text-base font-semibold">{title}</h2>
          {description && <p className="text-muted-foreground mt-1 text-xs">{description}</p>}
        </div>
        <div className="flex-1 overflow-y-auto" role="form" aria-label={`${title}表单`}>
          {body}
        </div>
      </div>
    )
  }

  return (
    <div className="bg-background text-foreground flex h-full flex-col overflow-hidden">
      <div className="max-w-3xl flex-1 overflow-y-auto p-3 sm:p-6" role="form" aria-label={`${title}表单`}>
        <h2 className="mb-1 text-base font-semibold">{title}</h2>
        {description && <p className="text-muted-foreground mb-4 text-xs">{description}</p>}
        {body}
      </div>
    </div>
  )
}

export function ConfigObject({
  obj,
  parentPath,
  onChange,
  onDelete,
  onAddField,
  allowAddField = false,
}: {
  obj: Record<string, unknown>
  parentPath: string[]
  onChange: (path: string[], value: unknown) => void
  onDelete: (path: string[]) => void
  onAddField: (parentPath: string[], key: string, value: unknown) => void
  /** 是否允许添加自定义字段（仅顶层配置允许；嵌套条目字段结构固定，不允许随意加） */
  allowAddField?: boolean
}) {
  const [showAdd, setShowAdd] = useState(false)
  const [newKey, setNewKey] = useState('')
  const entries = Object.entries(obj)

  const handleAdd = () => {
    const key = newKey.trim()
    if (!key) return
    if (key in obj) {
      toast.error('字段已存在', { description: key })
      return
    }
    onAddField(parentPath, key, '')
    setNewKey('')
    setShowAdd(false)
    toast.success(`已添加字段: ${key}`)
  }

  return (
    <div className="space-y-3">
      {entries.length === 0 && (
        <div className="text-muted-foreground py-4 text-center text-sm">该配置暂无字段</div>
      )}
      {entries.map(([key, value]) => (
        <div key={key} className="group relative">
          <FieldRenderer
            label={keyToLabel(key)}
            path={[...parentPath, key]}
            value={value}
            onChange={onChange}
            onDelete={onDelete}
            onAddField={onAddField}
          />
          <button
            onClick={() => onDelete([...parentPath, key])}
            className="absolute -right-1 top-0 hidden rounded p-0.5 text-muted-foreground/50 hover:text-status-error group-hover:block"
            title={`删除 ${key}`}
          >
            <Trash2 className="h-3 w-3" />
          </button>
        </div>
      ))}
      {/* 添加自定义字段：仅顶层配置允许（allowAddField=true）；嵌套条目字段固定不加 */}
      {allowAddField && (showAdd ? (
        <div className="flex items-center gap-2 rounded-lg border border-dashed p-2">
          <Input
            value={newKey}
            onChange={(e) => setNewKey(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && handleAdd()}
            placeholder="字段名（如 max_tokens）"
            className="h-7 text-xs"
            autoFocus
          />
          <Button size="sm" onClick={handleAdd} className="h-7 px-2 text-xs">添加</Button>
          <Button size="sm" variant="ghost" onClick={() => setShowAdd(false)} className="h-7 px-2 text-xs">取消</Button>
        </div>
      ) : (
        <button
          onClick={() => setShowAdd(true)}
          className="hover:bg-white/5 flex w-full items-center justify-center gap-1.5 rounded-lg border border-dashed py-1.5 text-xs text-muted-foreground transition-colors"
        >
          <Plus className="h-3 w-3" />
          添加自定义字段
        </button>
      ))}
    </div>
  )
}

function FieldRenderer({
  label,
  path,
  value,
  onChange,
  onDelete,
  onAddField,
}: {
  label: string
  path: string[]
  value: unknown
  onChange: (path: string[], value: unknown) => void
  onDelete: (path: string[]) => void
  onAddField: (parentPath: string[], key: string, value: unknown) => void
}) {
  const id = path.join('-')

  if (typeof value === 'boolean') {
    return (
      <FieldRow label={label} htmlFor={id}>
        <label className="flex cursor-pointer items-center gap-2">
          <input
            id={id}
            type="checkbox"
            checked={value}
            onChange={(e) => onChange(path, e.target.checked)}
            className="h-4 w-4 rounded border-gray-300"
          />
          <span className="text-muted-foreground text-xs">{value ? '已启用' : '已禁用'}</span>
        </label>
      </FieldRow>
    )
  }

  if (typeof value === 'number') {
    return (
      <FieldRow label={label} htmlFor={id}>
        <Input
          id={id}
          type="number"
          value={Number.isFinite(value) ? value : 0}
          onChange={(e) => onChange(path, Number(e.target.value))}
          className="max-w-xs"
        />
      </FieldRow>
    )
  }

  if (typeof value === 'string') {
    const isSecret =
      /api[_-]?key|token|secret|password|credential/i.test(path[path.length - 1] || '') ||
      value.includes('***')
    return (
      <FieldRow label={label} htmlFor={id}>
        <Input
          id={id}
          type={isSecret ? 'password' : 'text'}
          value={value}
          onChange={(e) => onChange(path, e.target.value)}
          className="max-w-xl"
        />
      </FieldRow>
    )
  }

  if (Array.isArray(value)) {
    return (
      <FieldRow label={label} htmlFor={id}>
        <textarea
          id={id}
          className="border-input bg-background min-h-[80px] w-full max-w-xl rounded-md border px-3 py-2 text-sm"
          value={JSON.stringify(value, null, 2)}
          onChange={(e) => {
            try {
              const parsed = JSON.parse(e.target.value)
              if (Array.isArray(parsed)) onChange(path, parsed)
            } catch {
              // 输入中，忽略
            }
          }}
        />
      </FieldRow>
    )
  }

  // dict-of-dicts（如 models: { glm-5.2: {...}, deepseek-v4: {...} }）
  // 用列表+模板添加，不让用户随意加字段
  if (value && typeof value === 'object' && !Array.isArray(value)) {
    const obj = value as Record<string, unknown>
    const values = Object.values(obj)
    const isDictOfDicts = values.length > 0 && values.every((v) => v && typeof v === 'object' && !Array.isArray(v))
    if (isDictOfDicts) {
      return (
        <DictOfDictsRenderer
          label={label}
          path={path}
          obj={obj}
          onChange={onChange}
          onDelete={onDelete}
          onAddField={onAddField}
        />
      )
    }
  }

  if (value && typeof value === 'object') {
    return (
      <CollapsibleObject label={label} path={path} value={value} onChange={onChange} onDelete={onDelete} onAddField={onAddField} />
    )
  }

  return (
    <FieldRow label={label} htmlFor={id}>
      <Input
        id={id}
        value={value == null ? '' : String(value)}
        onChange={(e) => onChange(path, e.target.value)}
        className="max-w-xl"
      />
    </FieldRow>
  )
}

/**
 * 可折叠嵌套对象——点标题展开/收起子字段，默认展开。
 * 用于 default_params/multimodal 等嵌套结构，让用户自己控制展开层级。
 */
function CollapsibleObject({
  label,
  path,
  value,
  onChange,
  onDelete,
  onAddField,
}: {
  label: string
  path: string[]
  value: unknown
  onChange: (path: string[], value: unknown) => void
  onDelete: (path: string[]) => void
  onAddField: (parentPath: string[], key: string, value: unknown) => void
}) {
  const [open, setOpen] = useState(true)
  const childCount = Object.keys(value as Record<string, unknown>).length
  return (
    <div className="rounded-lg border" style={{ borderColor: 'var(--ds-border-subtle, rgba(148,163,184,0.12))' }}>
      <button
        onClick={() => setOpen(!open)}
        className="hover:bg-white/5 flex w-full items-center gap-1.5 px-2.5 py-1.5 text-left"
      >
        <span className="text-muted-foreground text-[10px]">{open ? '▼' : '▶'}</span>
        <span className="text-xs font-medium">{label}</span>
        <span className="text-muted-foreground/60 text-[10px]">({childCount})</span>
      </button>
      {open && (
        <div className="border-t p-2.5" style={{ borderColor: 'var(--ds-border-subtle, rgba(148,163,184,0.12))' }}>
          <ConfigObject
            obj={value as Record<string, unknown>}
            parentPath={path}
            onChange={onChange}
            onDelete={onDelete}
            onAddField={onAddField}
          />
        </div>
      )}
    </div>
  )
}

/**
 * dict-of-dicts 渲染器（如 models: { model_id: {字段...} }）
 *
 * 显示条目列表，每个条目可展开编辑/删除。
 * 「添加」按钮从现有条目克隆字段模板（清空值），不让用户随意加字段。
 */
function DictOfDictsRenderer({
  label,
  path,
  obj,
  onChange,
  onDelete,
  onAddField,
}: {
  label: string
  path: string[]
  obj: Record<string, unknown>
  onChange: (path: string[], value: unknown) => void
  onDelete: (path: string[]) => void
  onAddField: (parentPath: string[], key: string, value: unknown) => void
}) {
  const entries = Object.entries(obj)
  const [expandedKey, setExpandedKey] = useState<string | null>(
    entries.length > 0 ? entries[0][0] : null,
  )
  const [showAddDialog, setShowAddDialog] = useState(false)
  const [newKey, setNewKey] = useState('')
  const [newValues, setNewValues] = useState<Record<string, unknown>>({})

  // 从第一个条目克隆模板（字段结构保留，值清空）
  const templateKeys = entries.length > 0
    ? Object.keys(entries[0][1] as Record<string, unknown>)
    : []

  const openAddDialog = () => {
    setNewKey('')
    setNewValues(Object.fromEntries(
      templateKeys.map((k) => {
        const sample = (entries[0][1] as Record<string, unknown>)[k]
        if (typeof sample === 'object' && sample !== null && !Array.isArray(sample)) return [k, {}]
        if (typeof sample === 'number') return [k, 0]
        if (typeof sample === 'boolean') return [k, false]
        return [k, '']
      }),
    ))
    setShowAddDialog(true)
  }

  const handleAdd = () => {
    const key = newKey.trim()
    if (!key) {
      toast.error('请输入条目标识')
      return
    }
    if (key in obj) {
      toast.error('条目已存在', { description: key })
      return
    }
    onAddField(path, key, { ...newValues })
    setShowAddDialog(false)
    setExpandedKey(key)
    toast.success(`已添加: ${key}`)
  }

  return (
    <FieldRow label={label} htmlFor={path.join('-')}>
      <div className="space-y-1.5">
        {entries.map(([key, val]) => (
          <div key={key} className="rounded-lg border" style={{ borderColor: 'var(--ds-border-subtle, rgba(148,163,184,0.12))' }}>
            <div className="flex items-center gap-2 px-2.5 py-1.5">
              <button
                onClick={() => setExpandedKey(expandedKey === key ? null : key)}
                className="text-muted-foreground hover:text-foreground flex-1 text-left text-xs font-medium"
              >
                {expandedKey === key ? '▼' : '▶'} {key}
              </button>
              <button
                onClick={() => onDelete([...path, key])}
                className="text-muted-foreground/50 hover:text-status-error rounded p-0.5"
                title={`删除 ${key}`}
              >
                <Trash2 className="h-3 w-3" />
              </button>
            </div>
            {expandedKey === key && (
              <div className="border-t p-2.5" style={{ borderColor: 'var(--ds-border-subtle, rgba(148,163,184,0.12))' }}>
                <ConfigObject
                  obj={val as Record<string, unknown>}
                  parentPath={[...path, key]}
                  onChange={onChange}
                  onDelete={onDelete}
                  onAddField={onAddField}
                />
              </div>
            )}
          </div>
        ))}

        <button
          onClick={openAddDialog}
          className="hover:bg-white/5 flex w-full items-center justify-center gap-1.5 rounded-lg border border-dashed py-1.5 text-xs text-muted-foreground transition-colors"
        >
          <Plus className="h-3 w-3" />
          添加{label}条目
        </button>

        {/* 统一模态框：添加条目（基于模板字段） */}
        <Dialog open={showAddDialog} onOpenChange={setShowAddDialog}>
          <DialogContent>
            <DialogHeader>
              <DialogTitle>添加{label}条目</DialogTitle>
            </DialogHeader>
            <div className="space-y-3 overflow-y-auto p-6" style={{ maxHeight: '60vh' }}>
              <div>
                <label className="text-muted-foreground mb-1 block text-xs">标识</label>
                <Input
                  value={newKey}
                  onChange={(e) => setNewKey(e.target.value)}
                  onKeyDown={(e) => e.key === 'Enter' && handleAdd()}
                  placeholder="条目标识（如 new_model）"
                  autoFocus
                />
              </div>
              {templateKeys.map((fieldKey) => {
                const sampleVal = (entries[0]?.[1] as Record<string, unknown>)?.[fieldKey]
                const isBool = typeof sampleVal === 'boolean'
                const isNum = typeof sampleVal === 'number'
                return (
                  <div key={fieldKey}>
                    <label className="text-muted-foreground mb-1 block text-xs">{keyToLabel(fieldKey)}</label>
                    {isBool ? (
                      <select
                        value={String(newValues[fieldKey] ?? false)}
                        onChange={(e) => setNewValues((v) => ({ ...v, [fieldKey]: e.target.value === 'true' }))}
                        className="h-8 w-full rounded border bg-transparent px-2 text-sm"
                      >
                        <option value="false">否</option>
                        <option value="true">是</option>
                      </select>
                    ) : (
                      <Input
                        type={isNum ? 'number' : 'text'}
                        value={newValues[fieldKey] == null ? '' : String(newValues[fieldKey])}
                        onChange={(e) => setNewValues((v) => ({
                          ...v,
                          [fieldKey]: isNum ? Number(e.target.value) : e.target.value,
                        }))}
                        placeholder={fieldKey}
                      />
                    )}
                  </div>
                )
              })}
            </div>
            <DialogFooter>
              <Button variant="ghost" onClick={() => setShowAddDialog(false)}>取消</Button>
              <Button onClick={handleAdd}>添加</Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      </div>
    </FieldRow>
  )
}

function FieldRow({
  label,
  htmlFor,
  children,
}: {
  label: string
  htmlFor: string
  children: React.ReactNode
}) {
  return (
    <div className="flex flex-col gap-1 sm:flex-row sm:items-start sm:gap-4">
      <label htmlFor={htmlFor} className="text-muted-foreground w-40 shrink-0 pt-2 text-xs">
        {label}
      </label>
      <div className="min-w-0 flex-1">{children}</div>
    </div>
  )
}
