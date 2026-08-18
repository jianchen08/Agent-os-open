/**
 * SortableList — 拖拽/按钮排序列表（widget 化 G5：富交互形态）
 *
 * 声明模型（ui_schema.widgets props）：
 *   { "type": "sortable_list",
 *     "props": {
 *       "items": ["a","b","c"],            // 或 [{label,value},...]
 *       "onChange": fn, 或经宿主注入受控 onChange（G4 桥）,
 *       "labels": {...}                    // 可选显示名映射
 *     } }
 *
 * 交互：原生 HTML5 DnD（draggable + dragover/drop 重排）+ 上/下按钮兜底
 * （jsdom 不可靠处维持可达性）。变更经 onChange(newItems) 流出（受控）。
 */
import { useMemo, useState } from 'react'
import { ChevronDown, ChevronUp } from '@/assets/icons'

export interface SortableItem {
  label: string
  value: string
}

function normalizeItems(raw: unknown): SortableItem[] {
  if (!Array.isArray(raw)) return []
  return raw
    .map((item) => {
      if (typeof item === 'string' || typeof item === 'number') {
        return { label: String(item), value: String(item) }
      }
      if (item && typeof item === 'object') {
        const o = item as { label?: unknown; value?: unknown }
        const value = String(o.value ?? o.label ?? '')
        return value ? { label: typeof o.label === 'string' ? o.label : value, value } : null
      }
      return null
    })
    .filter((x): x is SortableItem => x !== null)
}

export function SortableListWidget(props: Record<string, unknown>) {
  const items = useMemo(() => normalizeItems(props.items), [props.items])
  const labels = (props.labels as Record<string, string> | undefined) ?? {}
  const onChange = props.onChange as ((items: SortableItem[]) => void) | undefined
  const [dragIndex, setDragIndex] = useState<number | null>(null)

  const emit = (next: SortableItem[]) => {
    if (onChange) onChange(next)
  }
  const move = (from: number, to: number) => {
    if (to < 0 || to >= items.length || from === to) return
    const next = [...items]
    const [moved] = next.splice(from, 1)
    next.splice(to, 0, moved)
    emit(next)
  }

  return (
    <div className="space-y-1">
      {items.length === 0 && (
        <p className="text-muted-foreground py-2 text-center text-xs">暂无条目</p>
      )}
      {items.map((item, i) => (
        <div
          key={`${item.value}-${i}`}
          data-testid={`sortable-item-${i}`}
          draggable={items.length > 1}
          onDragStart={(e) => {
            setDragIndex(i)
            e.dataTransfer.effectAllowed = 'move'
          }}
          onDragOver={(e) => e.preventDefault()}
          onDrop={(e) => {
            e.preventDefault()
            if (dragIndex !== null) move(dragIndex, i)
            setDragIndex(null)
          }}
          className={`border-border/50 hover:border-primary/50 flex items-center gap-2 rounded-md border bg-transparent px-2 py-1.5 text-sm transition-colors ${
            dragIndex === i ? 'opacity-40' : ''
          }`}
        >
          <span className="text-muted-foreground/50 cursor-grab select-none text-xs">⋮⋮</span>
          <span className="min-w-0 flex-1 truncate">{labels[item.value] ?? item.label}</span>
          {items.length > 1 && (
            <span className="flex shrink-0 items-center gap-0.5">
              <button
                type="button"
                aria-label={`上移 ${item.label}`}
                disabled={i === 0}
                onClick={() => move(i, i - 1)}
                className="hover:bg-muted/60 disabled:opacity-30 rounded p-0.5"
              >
                <ChevronUp className="h-3.5 w-3.5" />
              </button>
              <button
                type="button"
                aria-label={`下移 ${item.label}`}
                disabled={i === items.length - 1}
                onClick={() => move(i, i + 1)}
                className="hover:bg-muted/60 disabled:opacity-30 rounded p-0.5"
              >
                <ChevronDown className="h-3.5 w-3.5" />
              </button>
            </span>
          )}
        </div>
      ))}
    </div>
  )
}
