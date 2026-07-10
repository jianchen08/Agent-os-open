/**
 * 通用配置页面组件
 *
 * 根据 YAML 配置结构自动生成表单。通过 configPath 和标题区分不同配置页面。
 * 支持嵌套对象、数组、布尔、数字、字符串等类型的自动渲染。
 *
 * 公共接口：
 * - GenericConfigPage(props) — 通用配置页面
 */

import { useState, useEffect, useCallback, createContext, useContext } from 'react'
import { Link } from 'react-router-dom'
import { Loader2 } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { toast } from '@/components/ui/sonner'
import { getGenericConfig, saveGenericConfig } from '@/services/api/config'

/** GenericConfigPage 组件属性 */
export interface GenericConfigPageProps {
  /** 后端配置路径（白名单 key，如 "system/memory_storage"） */
  configPath: string
  /** 页面标题 */
  title: string
  /** 页面描述 */
  description: string
  /** 字段中文标签映射（点号分隔路径，如 "database.pool_size"） */
  labelMap?: Record<string, string>
}

/** 保存状态 */
type SaveState = 'idle' | 'saving' | 'saved' | 'error'

/** 表单上下文类型 */
interface FormContextValue {
  onChange: (path: string[], value: unknown) => void
  /** 根据 path 获取中文标签 */
  getLabel: (path: string[]) => string
}

const FormContext = createContext<FormContextValue>({
  onChange: () => {},
  getLabel: (path) => keyToLabel(path[path.length - 1]),
})

/**
 * 通用配置页面组件
 *
 * 从后端读取 YAML 配置，根据值类型自动渲染表单字段，修改后保存回后端。
 */
export function GenericConfigPage({ configPath, title, description, labelMap }: GenericConfigPageProps) {
  const [config, setConfig] = useState<Record<string, unknown> | null>(null)
  const [isLoading, setIsLoading] = useState(true)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [saveState, setSaveState] = useState<SaveState>('idle')

  const getLabel = useCallback((path: string[]): string => {
    const dottedPath = path.join('.')
    if (labelMap && dottedPath in labelMap) return labelMap[dottedPath]
    return keyToLabel(path[path.length - 1])
  }, [labelMap])

  useEffect(() => {
    let cancelled = false
    setIsLoading(true)
    setLoadError(null)
    getGenericConfig(configPath)
      .then((data) => {
        if (!cancelled) setConfig(data)
      })
      .catch((error: unknown) => {
        if (!cancelled) {
          const msg = error instanceof Error ? error.message : '无法加载配置'
          setConfig({})
          setLoadError('无法加载配置')
          toast.error('配置加载失败', { description: msg })
        }
      })
      .finally(() => {
        if (!cancelled) setIsLoading(false)
      })
    return () => { cancelled = true }
  }, [configPath])

  const handleChange = useCallback((path: string[], value: unknown) => {
    setConfig((prev) => {
      if (!prev) return prev
      return setNestedValue(prev, path, value)
    })
  }, [])

  const handleSave = useCallback(async () => {
    if (!config) return
    setSaveState('saving')
    try {
      const saved = await saveGenericConfig(configPath, config)
      setConfig(saved)
      setSaveState('saved')
      setTimeout(() => setSaveState('idle'), 2000)
    } catch (error: unknown) {
      const msg = error instanceof Error ? error.message : '保存配置时发生错误'
      setSaveState('error')
      toast.error('配置保存失败', { description: msg })
    }
  }, [config, configPath])

  if (isLoading) {
    return (
      <SettingsPageShell title={title} description={description}>
        <div className="text-muted-foreground flex items-center justify-center py-20 text-sm">
          <div className="border-primary mr-2 h-5 w-5 animate-spin rounded-full border-2 border-t-transparent" />
          加载配置...
        </div>
      </SettingsPageShell>
    )
  }

  if (!config) {
    return (
      <SettingsPageShell title={title} description={description}>
        <div className="text-muted-foreground py-20 text-center text-sm">无法加载配置</div>
      </SettingsPageShell>
    )
  }

  return (
    <SettingsPageShell title={title} description={description}>
      {loadError && (
        <div className="mb-4 rounded-lg bg-status-warning/10 px-3 py-2 text-xs text-status-warning">
          {loadError}
        </div>
      )}

      <FormContext.Provider value={{ onChange: handleChange, getLabel }}>
        <RenderObject obj={config} parentPath={[]} />
      </FormContext.Provider>

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
        {saveState === 'saved' && <span className="text-xs text-status-success" role="status">已保存</span>}
        {saveState === 'error' && <span className="text-xs text-status-error" role="alert">保存失败</span>}
      </div>
    </SettingsPageShell>
  )
}

/* ============================================ */
/* 内部渲染函数                                  */
/* ============================================ */

/**
 * 递归渲染一个对象的所有字段
 */
function RenderObject({ obj, parentPath }: { obj: Record<string, unknown>; parentPath: string[] }): React.ReactNode {
  const { getLabel } = useContext(FormContext)
  const entries = Object.entries(obj)
  if (entries.length === 0) return null

  return (
    <div className="space-y-3">
      {entries.map(([key, value]) => (
        <FieldRenderer key={key} label={getLabel([...parentPath, key])} path={[...parentPath, key]} value={value} />
      ))}
    </div>
  )
}

/**
 * 单个字段渲染器，根据值的类型选择合适的控件
 */
function FieldRenderer({ label, path, value }: { label: string; path: string[]; value: unknown }) {
  const { onChange } = useContext(FormContext)

  // 布尔值 → 开关
  if (typeof value === 'boolean') {
    return (
      <FieldRow label={label} htmlFor={path.join('-')}>
        <label className="flex cursor-pointer items-center gap-2">
          <input
            id={path.join('-')}
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

  // 数字 → 数字输入
  if (typeof value === 'number') {
    return (
      <FieldRow label={label} htmlFor={path.join('-')}>
        <Input
          id={path.join('-')}
          type="number"
          value={value}
          onChange={(e) => onChange(path, parseNumber(e.target.value))}
        />
      </FieldRow>
    )
  }

  // 字符串 → 文本输入
  if (typeof value === 'string') {
    return (
      <FieldRow label={label} htmlFor={path.join('-')}>
        <Input
          id={path.join('-')}
          value={value}
          onChange={(e) => onChange(path, e.target.value)}
        />
      </FieldRow>
    )
  }

  // null → 空值输入
  if (value === null || value === undefined) {
    return (
      <FieldRow label={label} htmlFor={path.join('-')}>
        <Input
          id={path.join('-')}
          value=""
          placeholder="null"
          onChange={(e) => onChange(path, e.target.value || null)}
        />
      </FieldRow>
    )
  }

  // 数组 → 简易列表编辑
  if (Array.isArray(value)) {
    return (
      <div className="mb-4">
        <div className="text-muted-foreground mb-1.5 text-xs font-medium">{label}</div>
        <ArrayEditor path={path} items={value} onChange={onChange} />
      </div>
    )
  }

  // 对象 → 嵌套 Section
  if (typeof value === 'object') {
    return (
      <section className="mb-5">
        <h3 className="text-foreground mb-2 border-b pb-1.5 text-sm font-medium">{label}</h3>
        <RenderObject obj={value as Record<string, unknown>} parentPath={path} />
      </section>
    )
  }

  return null
}

/**
 * 数组编辑器
 */
function ArrayEditor({ path, items, onChange }: {
  path: string[]
  items: unknown[]
  onChange: (path: string[], value: unknown) => void
}) {
  const handleItemChange = (index: number, newVal: unknown) => {
    const updated = [...items]
    updated[index] = newVal
    onChange(path, updated)
  }

  const handleRemove = (index: number) => {
    const updated = items.filter((_, i) => i !== index)
    onChange(path, updated)
  }

  const handleAdd = () => {
    const firstItem = items[0]
    let defaultValue: unknown = ''
    if (typeof firstItem === 'number') defaultValue = 0
    else if (typeof firstItem === 'boolean') defaultValue = false
    else if (typeof firstItem === 'object' && firstItem !== null && !Array.isArray(firstItem)) {
      const template: Record<string, unknown> = {}
      for (const [k, v] of Object.entries(firstItem as Record<string, unknown>)) {
        template[k] = typeof v === 'number' ? 0 : typeof v === 'boolean' ? false : ''
      }
      defaultValue = template
    }
    onChange(path, [...items, defaultValue])
  }

  return (
    <div className="space-y-1.5">
      {items.map((item, i) => (
        <div key={i} className="flex items-start gap-2">
          <ArrayItemRenderer item={item} index={i} parentPath={path} onChange={handleItemChange} />
          <button
            onClick={() => handleRemove(i)}
            className="text-muted-foreground hover:text-status-error mt-1.5 shrink-0 text-xs"
            aria-label="删除"
          >
            ✕
          </button>
        </div>
      ))}
      <button onClick={handleAdd} className="text-primary hover:text-primary/80 text-xs">
        + 添加项
      </button>
    </div>
  )
}

/**
 * 渲染数组中的单个项
 */
function ArrayItemRenderer({ item, index, parentPath, onChange }: {
  item: unknown
  index: number
  parentPath: string[]
  onChange: (index: number, value: unknown) => void
}) {
  const itemPath = (key: string) => `${parentPath.join('-')}-${index}-${key}`

  if (typeof item === 'string') {
    return (
      <Input
        value={item}
        onChange={(e) => onChange(index, e.target.value)}
        className="flex-1"
      />
    )
  }

  if (typeof item === 'number') {
    return (
      <Input
        type="number"
        value={item}
        onChange={(e) => onChange(index, parseNumber(e.target.value))}
        className="flex-1"
      />
    )
  }

  if (typeof item === 'boolean') {
    return (
      <label className="flex flex-1 cursor-pointer items-center gap-2 pt-1.5">
        <input
          type="checkbox"
          checked={item}
          onChange={(e) => onChange(index, e.target.checked)}
          className="h-4 w-4 rounded border-gray-300"
        />
        <span className="text-muted-foreground text-xs">{item ? '已启用' : '已禁用'}</span>
      </label>
    )
  }

  if (typeof item === 'object' && item !== null && !Array.isArray(item)) {
    const obj = item as Record<string, unknown>
    return (
      <div className="bg-card flex-1 rounded border p-2">
        {Object.entries(obj).map(([k, v]) => (
          <div key={k} className="mb-1 last:mb-0">
            <FieldRow label={keyToLabel(k)} htmlFor={itemPath(k)}>
              {typeof v === 'boolean' ? (
                <input
                  id={itemPath(k)}
                  type="checkbox"
                  checked={v}
                  onChange={(e) => onChange(index, { ...obj, [k]: e.target.checked })}
                  className="h-4 w-4 rounded border-gray-300"
                />
              ) : typeof v === 'number' ? (
                <Input
                  id={itemPath(k)}
                  type="number"
                  value={v}
                  onChange={(e) => onChange(index, { ...obj, [k]: parseNumber(e.target.value) })}
                />
              ) : (
                <Input
                  id={itemPath(k)}
                  value={String(v ?? '')}
                  onChange={(e) => onChange(index, { ...obj, [k]: e.target.value })}
                />
              )}
            </FieldRow>
          </div>
        ))}
      </div>
    )
  }

  return <span className="text-muted-foreground flex-1 text-xs">{String(item)}</span>
}

/* ============================================ */
/* 共享子组件和工具函数                            */
/* ============================================ */

/** 页面外壳 */
function SettingsPageShell({ title, description, children }: {
  title: string
  description: string
  children: React.ReactNode
}) {
  return (
    <div className="bg-background text-foreground flex h-screen flex-col overflow-hidden">
      <header className="flex h-12 shrink-0 items-center border-b px-4">
        <Link to="/settings" className="text-muted-foreground hover:text-foreground text-sm">
          &larr; 设置
        </Link>
        <h1 className="ml-4 text-base font-semibold">{title}</h1>
        <span className="text-muted-foreground ml-2 text-xs">{description}</span>
      </header>
      <main className="max-w-3xl flex-1 overflow-y-auto p-3 sm:p-6" role="form" aria-label={`${title}表单`}>
        {children}
      </main>
    </div>
  )
}

/** 表单行 */
function FieldRow({ label, htmlFor, children }: {
  label: string
  htmlFor: string
  children: React.ReactNode
}) {
  return (
    <div className="flex flex-col gap-1 sm:flex-row sm:items-start sm:gap-4">
      <label
        htmlFor={htmlFor}
        className="text-muted-foreground text-sm sm:min-w-[140px] sm:shrink-0 sm:pt-2 sm:text-right"
      >
        {label}
      </label>
      <div className="flex-1">{children}</div>
    </div>
  )
}

/**
 * snake_case / camelCase → 人类可读标签
 */
function keyToLabel(key: string): string {
  return key
    .replace(/_/g, ' ')
    .replace(/([a-z])([A-Z])/g, '$1 $2')
    .replace(/^\w/, (c) => c.toUpperCase())
}

/**
 * 安全解析数字，保留空值
 */
function parseNumber(raw: string): number | null {
  if (raw === '' || raw === '-') return null
  const n = Number(raw)
  return Number.isNaN(n) ? null : n
}

/**
 * 不可变地设置嵌套对象中的值
 */
function setNestedValue(obj: Record<string, unknown>, path: string[], value: unknown): Record<string, unknown> {
  if (path.length === 0) return obj
  const [head, ...rest] = path
  if (rest.length === 0) {
    return { ...obj, [head]: value }
  }
  const child = obj[head]
  const safeChild = (child != null && typeof child === 'object' && !Array.isArray(child))
    ? child as Record<string, unknown>
    : {}
  return { ...obj, [head]: setNestedValue(safeChild, rest, value) }
}
