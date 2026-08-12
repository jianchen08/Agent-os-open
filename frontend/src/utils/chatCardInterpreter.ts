/**
 * chatCardInterpreter —— ui.chat_card 声明 → ActivityDetailBlock[] 翻译层（TC S3 核心）
 *
 * 架构意图：插件用 ui.chat_card 声明工具卡片（title 模板 + blocks），本模块把它翻译成
 * 现有的 ActivityDetailBlock[]（ActivityCard 已有的块渲染器原样消费），不重造块。
 * 注入点：enhanceActivityWithToolConfig 优先查声明 → 本解释器 → 回退手写 registry → L0 推断。
 *
 * 本模块是纯函数翻译层（模板引擎 + source 路径求值 + when 条件），复用现有 safeParseResult。
 * YAML 加载/后端透传（G1）属后端工作；本层接收已反序列化的 ChatCardDeclaration。
 *
 * 协议参考：docs/working/design/tool-card-rendering-design.md §五
 */

import { safeParseResult } from '@/utils/toolCardRegistry'
import type { ActivityAction, ActivityDetailBlock, DetailContentType } from '@/types/activity'

/** 卡片内容块声明（type 与 ActivityCard 现有 contentType 对齐） */
export interface ChatCardBlockDecl {
  type: 'text' | 'code' | 'json' | 'markdown' | 'diff' | 'kv' | 'file' | 'image' | 'link' | 'log'
  label?: string
  /** source 路径表达式（args.x / result.y / output.z / error / duration_ms / partial_output） */
  source?: string
  language?: string
  collapsible?: boolean
  defaultExpanded?: boolean
  /** 条件：falsy → 整块不渲染 */
  when?: string
  /** kv 专用：字段映射 */
  fields?: Array<{ key: string; source: string }>
  /** diff 专用 */
  diffOldSource?: string
  diffNewSource?: string
}

export interface ChatCardActionDecl {
  id: string
  label: string
  icon?: string
  /** on_click 协议（open_file/open_url/preview_image/copy/run_action）——执行接线待 actions 落地 */
  onClick?: Record<string, unknown>
}

/** ui.chat_card 声明（已反序列化） */
export interface ChatCardDeclaration {
  icon?: string
  /** 标题模板，如 '{{args.command | first_line | truncate:60}}' */
  title?: string
  summary?: string
  blocks?: ChatCardBlockDecl[]
  actions?: ChatCardActionDecl[]
}

/** 工具调用上下文（source/when 的取值域） */
export interface ToolCallContext {
  args?: Record<string, unknown>
  /** 原始结果（字符串会经 safeParseResult 解析 Python dict） */
  result?: unknown
  error?: string
  duration_ms?: number
  partial_output?: unknown
}

/** 模板过滤器（设计文档 §五最小集） */
type Filter = (value: string, arg?: string) => string

const FILTERS: Record<string, Filter> = {
  first_line: (v) => v.split('\n', 1)[0],
  truncate: (v, n) => {
    const max = Number(n ?? 60)
    return v.length > max ? `${v.slice(0, max)}…` : v
  },
  basename: (v) => v.split(/[/\\]/).pop() ?? v,
  hostname: (v) => {
    try {
      return new URL(v).hostname
    } catch {
      return v
    }
  },
  default: (v, d) => (v === '' || v === 'undefined' || v === 'null' ? d ?? '' : v),
}

/** 沿点路径取值（result/output 经 safeParse） */
export function evalPath(ctx: ToolCallContext, path?: string): unknown {
  if (!path) return undefined
  const [root, ...rest] = path.split('.')
  let current: unknown
  switch (root) {
    case 'args':
      current = ctx.args
      break
    case 'result':
      current = typeof ctx.result === 'string' ? safeParseResult(ctx.result) : ctx.result
      break
    case 'output':
      current =
        (typeof ctx.result === 'string' ? safeParseResult(ctx.result) : (ctx.result as Record<string, unknown> | null | undefined))?.output
      break
    case 'error':
      current = ctx.error
      break
    case 'duration_ms':
      current = ctx.duration_ms
      break
    case 'partial_output':
      current = ctx.partial_output
      break
    default:
      return undefined
  }
  for (const key of rest) {
    if (current == null || typeof current !== 'object') return undefined
    current = (current as Record<string, unknown>)[key]
  }
  return current
}

const TEMPLATE_RE = /\{\{\s*([^}]+?)\s*\}\}/g

/** 渲染 {{ expr | filter:arg }} 模板 */
export function renderTemplate(template: string, ctx: ToolCallContext): string {
  return template.replace(TEMPLATE_RE, (_m, expr: string) => {
    const [path, ...filterParts] = expr.split('|').map((s) => s.trim())
    const value = evalPath(ctx, path)
    if (value == null) return ''
    let str = String(value)
    for (const fp of filterParts) {
      const colon = fp.indexOf(':')
      const fname = (colon >= 0 ? fp.slice(0, colon) : fp).trim()
      const arg = colon >= 0 ? fp.slice(colon + 1).trim() : undefined
      const filter = FILTERS[fname]
      str = filter ? filter(str, arg) : str
    }
    return str
  })
}

/** when 条件求值（falsy → 跳过） */
function evalTruthy(ctx: ToolCallContext, expr: string): boolean {
  const v = evalPath(ctx, expr)
  return v !== null && v !== undefined && v !== '' && v !== false
}

function toStr(v: unknown): string {
  if (v == null) return ''
  return typeof v === 'object' ? JSON.stringify(v) : String(v)
}

/** 单个块声明 → ActivityDetailBlock（值缺失返回 null） */
function translateBlock(decl: ChatCardBlockDecl, ctx: ToolCallContext): ActivityDetailBlock | null {
  const label = decl.label ?? ''
  const base = { label, collapsible: decl.collapsible, defaultExpanded: decl.defaultExpanded }

  switch (decl.type) {
    case 'kv': {
      const kvItems = (decl.fields ?? [])
        .map((f) => ({ key: f.key, value: toStr(evalPath(ctx, f.source)) }))
        .filter((kv) => kv.value !== '')
      if (kvItems.length === 0) return null
      return { ...base, contentType: 'kv' as DetailContentType, kvItems }
    }
    case 'file':
    case 'image': {
      const path = toStr(evalPath(ctx, decl.source))
      if (!path) return null
      return { ...base, contentType: decl.type as DetailContentType, path }
    }
    case 'link': {
      const url = toStr(evalPath(ctx, decl.source))
      if (!url) return null
      return { ...base, contentType: 'link' as DetailContentType, url }
    }
    case 'log':
    case 'text':
    case 'markdown': {
      const content = toStr(evalPath(ctx, decl.source))
      if (content === '') return null
      return { ...base, contentType: decl.type as DetailContentType, content }
    }
    case 'code': {
      const content = toStr(evalPath(ctx, decl.source))
      if (content === '') return null
      return { ...base, contentType: 'code' as DetailContentType, content, language: decl.language }
    }
    case 'json': {
      const raw = evalPath(ctx, decl.source)
      if (raw == null) return null
      return { ...base, contentType: 'json' as DetailContentType, content: raw as Record<string, unknown> }
    }
    case 'diff': {
      const diffOld = toStr(evalPath(ctx, decl.diffOldSource))
      const diffNew = toStr(evalPath(ctx, decl.diffNewSource))
      if (!diffOld && !diffNew) return null
      return { ...base, contentType: 'diff' as DetailContentType, content: '', diffOld, diffNew }
    }
    default:
      return null
  }
}

export interface InterpretedChatCard {
  title?: string
  icon?: string
  summary?: string
  details: ActivityDetailBlock[]
  actions: ActivityAction[]
}

/**
 * 据 ui.chat_card 声明 + 工具调用上下文，翻译成 ActivityCard 可直接渲染的 details/actions。
 *
 * - title/summary 经模板引擎求值
 * - blocks 按 when 过滤后翻译为 ActivityDetailBlock[]（值缺失的块自动跳过）
 * - actions 产出结构（on_click 执行接线待后续落地）
 */
export function interpretChatCard(decl: ChatCardDeclaration, ctx: ToolCallContext): InterpretedChatCard {
  const details: ActivityDetailBlock[] = []
  for (const block of decl.blocks ?? []) {
    if (block.when !== undefined && !evalTruthy(ctx, block.when)) continue
    const translated = translateBlock(block, ctx)
    if (translated) details.push(translated)
  }

  const actions: ActivityAction[] = (decl.actions ?? []).map((a) => ({
    id: a.id,
    icon: null,
    label: a.label,
  }))

  return {
    title: decl.title ? renderTemplate(decl.title, ctx) : undefined,
    icon: decl.icon,
    summary: decl.summary ? renderTemplate(decl.summary, ctx) : undefined,
    details,
    actions,
  }
}
