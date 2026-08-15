/**
 * 自由 key-value 编辑器（管道 step 的 context / 路由 then.set）。
 *
 * 值按类型渲染：string → 文本框、number → 数字框、boolean → 勾选框、
 * 其余（对象/数组）→ JSON 文本域（草稿态，解析合法才提交）。
 * 整对象提交给上层（上层决定 set 或 remove，空对象语义由上层处理）。
 */

import { useEffect, useState } from 'react'
import { Plus, Trash2 } from '@/assets/icons'
import { Input } from '@/components/ui/input'
import { cn } from '@/lib/utils'

/** 值输入控件：非原生类型走 JSON 草稿 */
function ValueInput({
  value,
  onChange,
}: {
  value: unknown
  onChange: (next: unknown) => void
}) {
  const [draft, setDraft] = useState<string | null>(null)

  // 外部值变更（如整体保存后重载）时丢弃未提交草稿
  useEffect(() => {
    setDraft(null)
  }, [value])

  if (typeof value === 'boolean') {
    return (
      <input
        type="checkbox"
        checked={value}
        onChange={(e) => onChange(e.target.checked)}
        className="h-4 w-4 cursor-pointer accent-[var(--btn-primary-bg)]"
        aria-label="布尔值"
      />
    )
  }

  if (typeof value === 'number') {
    return (
      <Input
        type="number"
        value={Number.isNaN(value) ? '' : String(value)}
        onChange={(e) => {
          const parsed = Number(e.target.value)
          onChange(e.target.value === '' || Number.isNaN(parsed) ? e.target.value : parsed)
        }}
        className="h-7 font-mono text-xs"
        aria-label="数值"
      />
    )
  }

  if (typeof value === 'string' || value === undefined || value === null) {
    return (
      <Input
        value={String(value ?? '')}
        onChange={(e) => onChange(e.target.value)}
        className="h-7 font-mono text-xs"
        aria-label="字符串值"
      />
    )
  }

  // 对象/数组：JSON 文本域，草稿解析合法才提交
  const text = draft ?? JSON.stringify(value, null, 2)
  let parseError = false
  if (draft !== null) {
    try {
      JSON.parse(draft)
    } catch {
      parseError = true
    }
  }
  return (
    <textarea
      value={text}
      onChange={(e) => setDraft(e.target.value)}
      onBlur={() => {
        if (draft !== null) {
          try {
            onChange(JSON.parse(draft))
          } catch {
            // 非法 JSON 不提交，还原为外部值
          }
        }
        setDraft(null)
      }}
      rows={2}
      className={cn(
        'border-input bg-[var(--bg-input,hsl(var(--background)))] w-full rounded-lg border px-2 py-1 font-mono text-xs focus:outline-none',
        parseError && 'border-status-error',
      )}
      aria-label="JSON 值"
      spellCheck={false}
    />
  )
}

/**
 * key-value 编辑器。
 *
 * @param value 当前对象（可能 undefined）
 * @param onChange 提交整个新对象（key 重命名保持原顺序）
 * @param keyPlaceholder 新 key 的占位提示
 * @param emptyHint 空状态提示文案
 */
export function KeyValueEditor({
  value,
  onChange,
  keyPlaceholder = '字段名',
  emptyHint = '暂无字段',
}: {
  value: Record<string, unknown> | undefined
  onChange: (next: Record<string, unknown> | undefined) => void
  keyPlaceholder?: string
  emptyHint?: string
}) {
  const entries = Object.entries(value ?? {})

  const renameKey = (oldKey: string, newKey: string) => {
    if (!newKey || newKey === oldKey) return
    const next: Record<string, unknown> = {}
    for (const [k, v] of entries) next[k === oldKey ? newKey : k] = v
    onChange(next)
  }

  const setValue = (key: string, v: unknown) => {
    onChange({ ...(value ?? {}), [key]: v })
  }

  const removeKey = (key: string) => {
    if (!value) return
    const next = { ...value }
    delete next[key]
    // 清空后回传 undefined，由上层决定删除整个键（避免写出空对象）
    onChange(Object.keys(next).length > 0 ? next : undefined)
  }

  const addField = () => {
    const existing = new Set(entries.map(([k]) => k))
    let key = 'field_1'
    for (let i = 1; existing.has(key); i++) key = `field_${i}`
    onChange({ ...(value ?? {}), [key]: '' })
  }

  return (
    <div className="space-y-1.5">
      {entries.length === 0 && (
        <p className="text-muted-foreground text-xs">{emptyHint}</p>
      )}
      {entries.map(([key, val]) => (
        <div key={key} className="flex items-start gap-1.5">
          <Input
            value={key}
            onChange={(e) => renameKey(key, e.target.value)}
            className="h-7 w-44 shrink-0 font-mono text-xs"
            aria-label={`${key} 键名`}
            spellCheck={false}
          />
          <div className="min-w-0 flex-1 pt-0.5">
            <ValueInput value={val} onChange={(v) => setValue(key, v)} />
          </div>
          <button
            type="button"
            onClick={() => removeKey(key)}
            className="text-muted-foreground hover:text-status-error mt-1 rounded p-0.5"
            aria-label={`删除 ${key}`}
            title="删除字段"
          >
            <Trash2 className="h-3.5 w-3.5" />
          </button>
        </div>
      ))}
      <button
        type="button"
        onClick={addField}
        className="text-muted-foreground hover:text-foreground hover:bg-[var(--hover-overlay)] flex items-center gap-1 rounded px-1.5 py-1 text-xs"
      >
        <Plus className="h-3.5 w-3.5" />
        添加字段
      </button>
      {entries.length === 0 && (
        <span className="sr-only" data-testid="kv-empty">
          {keyPlaceholder}
        </span>
      )}
    </div>
  )
}
