/*
 * Ported from DeepSeek Harness (DSH) — MIT License.
 * Copyright (c) 2026 DeepSeek. Source: packages/client/ui-primitives/src/JsonTree.tsx
 * Repo pinned at commit 47f943859bef60e4160492346772ded9b24f765a (version 0.1.0-rc.5).
 * Adapted for AgentOS (task_dsh_plugin_adapter 任务 3):
 * - 剥离 Menu.tsx 右键菜单与 dsh icons（109KB 图标集）——复制按钮退化为单击复制
 *   默认模式（对象=pretty JSON、原始值=原值），lucide-react 的 Check/Copy 替代图标；
 * - 键盘导航/展开/预览逻辑保持原样。
 */

import clsx from 'clsx'
import { useEffect, useId, useMemo, useRef, useState } from 'react'
import type {
  KeyboardEvent as ReactKeyboardEvent,
  MouseEvent as ReactMouseEvent,
  ReactNode,
  UIEvent as ReactUIEvent,
} from 'react'
import { Check, Copy } from 'lucide-react'
import css from './JsonTree.module.css'

const OBJECT_PREVIEW_LIMIT = 4
const ARRAY_PREVIEW_LIMIT = 5
const PREVIEW_DEPTH_LIMIT = 2

/**
 * Display copy for the tree's copy affordance; the owner passes localized
 * labels (this package is cordis-free, so copy arrives via props). Every field
 * defaults to the current built-in value, so existing consumers render
 * unchanged.
 */
export interface JsonTreeLabels {
  /** Copy-button state label after a successful copy. */
  copied: string
  /** Copy-button state label after a failed copy. */
  copyFailed: string
  /** Expander aria label while expanded. */
  collapseNode: string
  /** Expander aria label while collapsed. */
  expandNode: string
  /** Copy-button tooltip, given the current action label. */
  copyButtonTitle: (action: string) => string
}

const DEFAULT_LABELS: JsonTreeLabels = {
  copied: '已复制',
  copyFailed: '复制失败',
  collapseNode: 'Collapse JSON node',
  expandNode: 'Expand JSON node',
  copyButtonTitle: (action: string) => action,
}

type JsonPath = readonly (number | string)[]

interface RowTarget {
  path: JsonPath
  value: unknown
}

interface CopyTarget extends RowTarget {
  left: number
  side: 'bottom' | 'top'
  top: number
}

function isExpandableValue(value: unknown): value is object | unknown[] {
  return typeof value === 'object' && value !== null && !(value instanceof Date)
}

function entriesOf(value: object | unknown[]): readonly (readonly [string, unknown])[] {
  if (Array.isArray(value)) {
    return value.map((item, index) => [String(index), item] as const)
  }
  return Object.keys(value).map(key => [
    key,
    (value as Record<string, unknown>)[key],
  ] as const)
}

function bracketOf(value: object | unknown[]): readonly [string, string] {
  return Array.isArray(value) ? ['[', ']'] : ['{', '}']
}

function previewPrimitive(value: unknown): ReactNode {
  if (value === null) return <span className={css.keywordValue}>null</span>
  if (typeof value === 'string') {
    return <span className={css.stringValue}>{JSON.stringify(value)}</span>
  }
  if (typeof value === 'number') {
    return <span className={css.numberValue}>{String(value)}</span>
  }
  if (typeof value === 'boolean') {
    return <span className={css.keywordValue}>{String(value)}</span>
  }
  if (typeof value === 'bigint') {
    return <span className={css.otherValue}>{value.toString()}</span>
  }
  if (typeof value === 'undefined') {
    return <span className={css.otherValue}>undefined</span>
  }
  if (typeof value === 'symbol') {
    return <span className={css.otherValue}>{value.description ?? 'Symbol'}</span>
  }
  if (typeof value === 'function') {
    return <span className={css.otherValue}>{value.name || 'Function'}</span>
  }
  return null
}

function previewValue(value: unknown, depth: number): ReactNode {
  if (!isExpandableValue(value)) return previewPrimitive(value)

  const array = Array.isArray(value)
  const entries = entriesOf(value)
  const limit = array ? ARRAY_PREVIEW_LIMIT : OBJECT_PREVIEW_LIMIT
  const visible = entries.slice(0, limit)
  const [open, close] = bracketOf(value)

  return (
    <>
      <span className={css.punctuation}>{open}</span>
      {depth >= PREVIEW_DEPTH_LIMIT
        ? <span className={css.previewEllipsis}>…</span>
        : visible.map(([key, item], index) => (
          <span key={key}>
            {index > 0 && <span className={css.punctuation}>, </span>}
            {!array && (
              <>
                <span className={css.previewProperty}>{key}</span>
                <span className={css.punctuation}>: </span>
              </>
            )}
            {previewValue(item, depth + 1)}
          </span>
        ))}
      {depth < PREVIEW_DEPTH_LIMIT && entries.length > limit && (
        <span className={css.previewEllipsis}>, …</span>
      )}
      <span className={css.punctuation}>{close}</span>
    </>
  )
}

function primitiveValue(value: unknown): ReactNode {
  if (value === null) return <span className={css.keywordValue}>null</span>
  if (typeof value === 'string') {
    return <span className={css.stringValue}>{JSON.stringify(value)}</span>
  }
  if (typeof value === 'boolean') {
    return <span className={css.keywordValue}>{String(value)}</span>
  }
  if (typeof value === 'number') {
    return <span className={css.numberValue}>{String(value)}</span>
  }
  if (typeof value === 'bigint') {
    return <span className={css.numberValue}>{`${value.toString()}n`}</span>
  }
  if (value instanceof Date) {
    return <span className={css.otherValue}>{value.toISOString()}</span>
  }
  if (typeof value === 'function') {
    return <span className={css.otherValue}>function() {'{ }'}</span>
  }
  if (typeof value === 'undefined') {
    return <span className={css.otherValue}>undefined</span>
  }
  return <span className={css.otherValue}>{(value as symbol).toString()}</span>
}

function fieldText(field: string): string {
  return field === '' ? '""' : field
}

function pathId(path: JsonPath): string {
  return path.map(part => (
    typeof part === 'number' ? `n${String(part)}` : `s${String(part.length)}:${part}`
  )).join('/')
}

function claimFocus(button: HTMLElement): void {
  button.focus()
}

function moveFocus(button: HTMLElement, direction: -1 | 1): void {
  const tree = button.closest<HTMLElement>('[role="tree"]')
  if (tree === null) return
  const expanders = Array.from(tree.querySelectorAll<HTMLElement>('[data-json-expander]'))
  const current = expanders.indexOf(button)
  if (current < 0 || expanders.length === 0) return
  const next = (current + direction + expanders.length) % expanders.length
  const nextExpander = expanders[next]
  if (nextExpander !== undefined) claimFocus(nextExpander)
}

function NodeField({
  field,
  expandable,
  onToggle,
}: {
  field: string | undefined
  expandable: boolean
  onToggle: () => void
}) {
  if (field === undefined) return null
  return (
    <span
      className={clsx(css.label, expandable && css.clickableLabel)}
      onClick={expandable ? onToggle : undefined}
    >
      {fieldText(field)}:
    </span>
  )
}

interface JsonTreeNodeProps {
  field?: string
  initialExpanded: boolean
  labels: JsonTreeLabels
  lastElement: boolean
  onClaimTabStop: (id: string) => void
  onRowHover: (row: HTMLElement, target: RowTarget) => void
  path: JsonPath
  tabStopId: string | null
  value: unknown
}

function JsonTreeNode({
  field,
  initialExpanded,
  labels,
  lastElement,
  onClaimTabStop,
  onRowHover,
  path,
  tabStopId,
  value,
}: JsonTreeNodeProps) {
  const contentsId = useId()
  const expanderRef = useRef<HTMLSpanElement>(null)
  const [expanded, setExpanded] = useState(initialExpanded)
  const nodeId = pathId(path)
  const container = isExpandableValue(value)
  const entries = container ? entriesOf(value) : []
  const expandable = entries.length > 0

  const toggle = () => {
    setExpanded(current => !current)
    claimFocus(expanderRef.current as HTMLSpanElement)
  }

  const onExpanderKeyDown = (event: ReactKeyboardEvent<HTMLSpanElement>) => {
    if (event.key === 'ArrowRight' || event.key === 'ArrowLeft') {
      event.preventDefault()
      setExpanded(event.key === 'ArrowRight')
      return
    }
    if (event.key === 'ArrowUp' || event.key === 'ArrowDown') {
      event.preventDefault()
      moveFocus(event.currentTarget, event.key === 'ArrowUp' ? -1 : 1)
    }
  }

  const row = (children: ReactNode, ariaExpanded?: boolean) => (
    <div
      className={css.row}
      role="treeitem"
      aria-expanded={ariaExpanded}
      onMouseOver={(event) => {
        event.stopPropagation()
        onRowHover(event.currentTarget, { path, value })
      }}
    >
      {children}
    </div>
  )

  if (!container) {
    return row((
      <>
        <NodeField field={field} expandable={false} onToggle={toggle} />
        {primitiveValue(value)}
        {!lastElement && <span className={css.punctuation}>,</span>}
      </>
    ))
  }

  const [open, close] = bracketOf(value)
  if (!expandable) {
    return row((
      <>
        <NodeField field={field} expandable={false} onToggle={toggle} />
        <span className={css.punctuation}>{open}</span>
        <span className={css.punctuation}>{close}</span>
        {!lastElement && <span className={css.punctuation}>,</span>}
      </>
    ))
  }

  return row((
    <>
      <span
        ref={expanderRef}
        className={clsx(css.expander, expanded ? css.collapseIcon : css.expandIcon)}
        data-json-expander
        role="button"
        aria-label={expanded ? labels.collapseNode : labels.expandNode}
        aria-expanded={expanded}
        aria-controls={expanded ? contentsId : undefined}
        tabIndex={tabStopId === nodeId ? 0 : -1}
        onFocus={() => { onClaimTabStop(nodeId) }}
        onClick={toggle}
        onKeyDown={onExpanderKeyDown}
      />
      <NodeField field={field} expandable onToggle={toggle} />
      <span className={css.preview}>{previewValue(value, 0)}</span>
      {!lastElement && <span className={css.punctuation}>,</span>}
      {expanded && (
        <ul id={contentsId} role="group" className={css.children}>
          {entries.map(([key, item], index) => (
            <JsonTreeNode
              key={key}
              field={key}
              value={item}
              path={[...path, Array.isArray(value) ? index : key]}
              labels={labels}
              lastElement={index === entries.length - 1}
              initialExpanded={false}
              tabStopId={tabStopId}
              onClaimTabStop={onClaimTabStop}
              onRowHover={onRowHover}
            />
          ))}
        </ul>
      )}
    </>
  ), expanded)
}

function copyText(target: CopyTarget): string {
  // 单击复制默认模式：对象 → pretty JSON，原始值 → 原值（原右键菜单的默认项）。
  if (typeof target.value === 'object' && target.value !== null) {
    return JSON.stringify(target.value, null, 2)
  }
  if (typeof target.value === 'string') return target.value
  if (typeof target.value === 'undefined') return 'undefined'
  if (typeof target.value === 'bigint') return target.value.toString()
  if (typeof target.value === 'symbol') return target.value.description ?? 'Symbol'
  if (typeof target.value === 'function') return target.value.name || 'Function'
  return JSON.stringify(target.value)
}

/** Props for the read-only, token-themed JSON tree. */
export interface JsonTreeProps {
  /** Parsed JSON object or array. */
  data: object | unknown[]
  /** Accessible label for the tree. */
  label?: string
  /** Optional positioning class owned by the caller. */
  className?: string | undefined
  /** Whether JSON rows expose copy actions. */
  copyable?: boolean
  /** Whether the top-level object or array is always expanded. */
  expandTopLevel?: boolean
  /** Localized display copy; omitted fields keep the built-in defaults. */
  labels?: Partial<JsonTreeLabels> | undefined
}

/**
 * Render parsed JSON as a compact, keyboard-accessible inspector tree.
 * @param props - Parsed data, accessible label, and display options.
 * @returns A read-only JSON tree with an optionally fixed-open top level.
 */
export function JsonTree({
  data,
  label = 'JSON',
  className,
  copyable = true,
  expandTopLevel = true,
  labels,
}: JsonTreeProps) {
  const copyLabels = useMemo<JsonTreeLabels>(
    () => (labels === undefined ? DEFAULT_LABELS : { ...DEFAULT_LABELS, ...labels }),
    [labels],
  )
  const rootEntries = entriesOf(data)
  const firstExpandableIndex = rootEntries.findIndex(([, value]) => (
    isExpandableValue(value) && entriesOf(value).length > 0
  ))
  const firstExpandableEntry = rootEntries[firstExpandableIndex]
  const initialTabStopId = expandTopLevel
    ? firstExpandableEntry === undefined
      ? null
      : pathId([Array.isArray(data) ? firstExpandableIndex : firstExpandableEntry[0]])
    : isExpandableValue(data) && rootEntries.length > 0 ? pathId([]) : null
  const rootRef = useRef<HTMLDivElement>(null)
  const activeRowRef = useRef<HTMLElement | undefined>(undefined)
  const resetTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined)
  const [copyTarget, setCopyTarget] = useState<CopyTarget>()
  const [copyState, setCopyState] = useState<'idle' | 'copied' | 'failed'>('idle')
  const [tabStopId, setTabStopId] = useState<string | null>(initialTabStopId)

  const setActiveRow = (row: HTMLElement | undefined) => {
    activeRowRef.current?.removeAttribute('data-json-copy-active')
    activeRowRef.current = row
    row?.setAttribute('data-json-copy-active', '')
  }

  const clearCopyTarget = () => {
    setActiveRow(undefined)
    setCopyTarget(undefined)
    setCopyState('idle')
  }

  const copyPosition = (row: HTMLElement): Pick<CopyTarget, 'left' | 'side' | 'top'> => {
    const root = rootRef.current
    if (root === null) throw new Error('JsonTree root is not mounted')
    const rootRect = root.getBoundingClientRect()
    const rowRect = row.getBoundingClientRect()
    return {
      left: rootRect.left + root.clientWidth - 26,
      side: rowRect.top - rootRect.top > root.clientHeight / 2 ? 'top' : 'bottom',
      top: rowRect.top,
    }
  }

  const positionCopyButton = (row: HTMLElement, target: RowTarget) => {
    const position = copyPosition(row)
    setCopyTarget({ ...target, ...position })
  }

  const repositionCopyButton = (row: HTMLElement) => {
    const position = copyPosition(row)
    setCopyTarget((current) => {
      if (current === undefined) return current
      return { ...current, ...position }
    })
  }

  useEffect(() => () => {
    if (resetTimer.current !== undefined) clearTimeout(resetTimer.current)
    activeRowRef.current?.removeAttribute('data-json-copy-active')
  }, [])

  useEffect(() => {
    activeRowRef.current?.removeAttribute('data-json-copy-active')
    activeRowRef.current = undefined
    setCopyTarget(undefined)
    setCopyState('idle')
    setTabStopId(initialTabStopId)
  }, [data, expandTopLevel, initialTabStopId])

  useEffect(() => {
    const reposition = () => {
      const row = activeRowRef.current
      if (row !== undefined) repositionCopyButton(row)
    }
    window.addEventListener('scroll', reposition, true)
    window.addEventListener('resize', reposition)
    return () => {
      window.removeEventListener('scroll', reposition, true)
      window.removeEventListener('resize', reposition)
    }
  }, [])

  const handleRowHover = (row: HTMLElement, target: RowTarget) => {
    if (!copyable) return
    if (activeRowRef.current === row) return
    setActiveRow(row)
    setCopyState('idle')
    positionCopyButton(row, target)
  }

  const handleRootMouseOver = (event: ReactMouseEvent<HTMLDivElement>) => {
    if (!copyable) return
    if (!(event.target instanceof Element)) return
    if (event.target.closest('[data-json-copy-button]') === null) clearCopyTarget()
  }

  const handleScroll = (_event: ReactUIEvent<HTMLDivElement>) => {
    const row = activeRowRef.current
    if (row !== undefined) repositionCopyButton(row)
  }

  const copy = async () => {
    if (copyTarget === undefined) return
    try {
      await navigator.clipboard.writeText(copyText(copyTarget))
      setCopyState('copied')
    } catch {
      setCopyState('failed')
    }
    if (resetTimer.current !== undefined) clearTimeout(resetTimer.current)
    resetTimer.current = setTimeout(() => { setCopyState('idle') }, 1_500)
  }

  const [rootOpen, rootClose] = bracketOf(data)
  const copyTitle = copyState === 'copied'
    ? copyLabels.copied
    : copyState === 'failed'
      ? copyLabels.copyFailed
      : 'Copy'

  return (
    <div
      ref={rootRef}
      className={clsx(css.root, className)}
      onMouseOver={handleRootMouseOver}
      onMouseLeave={() => { clearCopyTarget() }}
      onScroll={handleScroll}
    >
      {expandTopLevel
        ? (
          <div className={css.expandedTopLevel}>
            <div
              className={clsx(css.row, css.topLevelBracket)}
              data-json-root-row
              onMouseOver={(event) => {
                event.stopPropagation()
                handleRowHover(event.currentTarget, { path: [], value: data })
              }}
            >
              <span className={css.punctuation}>{rootOpen}</span>
            </div>
            <div
              aria-label={label}
              className={clsx(css.container, css.expandedTopLevelContainer)}
              role="tree"
            >
              {rootEntries.map(([key, value], index) => (
                <JsonTreeNode
                  key={key}
                  field={key}
                  value={value}
                  path={[Array.isArray(data) ? index : key]}
                  labels={copyLabels}
                  lastElement={index === rootEntries.length - 1}
                  initialExpanded={false}
                  tabStopId={tabStopId}
                  onClaimTabStop={setTabStopId}
                  onRowHover={handleRowHover}
                />
              ))}
            </div>
            <div className={clsx(css.row, css.topLevelBracket)}>
              <span className={css.punctuation}>{rootClose}</span>
            </div>
          </div>
        )
        : (
          <div aria-label={label} className={css.container} role="tree">
            <JsonTreeNode
              value={data}
              path={[]}
              labels={copyLabels}
              lastElement
              initialExpanded
              tabStopId={tabStopId}
              onClaimTabStop={setTabStopId}
              onRowHover={handleRowHover}
            />
          </div>
        )}
      {copyTarget !== undefined && (
        <span
          className={css.copyAnchor}
          style={{ left: copyTarget.left, top: copyTarget.top }}
        >
          <button
            type="button"
            className={css.copyButton}
            data-json-copy-button
            data-state={copyState}
            aria-label={copyTitle}
            title={copyLabels.copyButtonTitle(copyTitle)}
            onClick={() => { void copy() }}
          >
            {copyState === 'copied'
              ? <Check size={12} aria-hidden="true" />
              : <Copy size={12} aria-hidden="true" />}
          </button>
        </span>
      )}
    </div>
  )
}
