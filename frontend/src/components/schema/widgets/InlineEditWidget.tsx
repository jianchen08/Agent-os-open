/**
 * InlineEditWidget — 内联编辑（widget 化 G5：富交互形态）
 *
 * 声明模型（ui_schema.widgets props）：
 *   { "type": "inline_edit",
 *     "props": { "value": "...", "onChange": host/受控, "placeholder": "...",
 *                "viewLabel": "显示文案（缺省用 value）", "multiline": false } }
 *
 * 交互：只读态点击 → 输入框；Enter/失焦提交 onChange(newValue)；Esc 取消。
 */
import { useEffect, useRef, useState } from 'react'

export function InlineEditWidget(props: Record<string, unknown>) {
  const value = props.value as string | number | undefined
  const onChange = props.onChange as ((v: string) => void) | undefined
  const placeholder = (props.placeholder as string) ?? '点击编辑'
  const viewLabel = (props.viewLabel as string) ?? String(value ?? '')
  const multiline = props.multiline === true

  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState('')
  const inputRef = useRef<HTMLInputElement | HTMLTextAreaElement>(null)

  useEffect(() => {
    if (editing) inputRef.current?.focus()
  }, [editing])

  const begin = () => {
    setDraft(String(value ?? ''))
    setEditing(true)
  }
  const commit = () => {
    if (onChange && draft !== String(value ?? '')) onChange(draft)
    setEditing(false)
  }
  const cancel = () => setEditing(false)

  if (editing) {
    return multiline ? (
      <textarea
        ref={inputRef as React.Ref<HTMLTextAreaElement>}
        data-testid="inline-edit-input"
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={commit}
        onKeyDown={(e) => {
          if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) commit()
          else if (e.key === 'Escape') cancel()
        }}
        rows={3}
        className="border-primary bg-background w-full rounded-md border px-2 py-1 text-sm outline-none"
        placeholder={placeholder}
      />
    ) : (
      <input
        ref={inputRef as React.Ref<HTMLInputElement>}
        data-testid="inline-edit-input"
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={commit}
        onKeyDown={(e) => {
          if (e.key === 'Enter') commit()
          else if (e.key === 'Escape') cancel()
        }}
        className="border-primary bg-background w-full rounded-md border px-2 py-1 text-sm outline-none"
        placeholder={placeholder}
      />
    )
  }

  return (
    <button
      type="button"
      data-testid="inline-edit-view"
      onClick={begin}
      className="hover:bg-muted/60 text-muted-foreground hover:text-foreground w-full rounded px-2 py-1 text-left text-sm transition-colors"
      title="点击编辑"
    >
      {viewLabel === '' && value == null ? <span className="opacity-60">{placeholder}</span> : viewLabel}
    </button>
  )
}
