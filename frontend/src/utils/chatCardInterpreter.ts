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
  /** 区块 ID（透传到 ActivityDetailBlock.id，供测试/外部按 id 定位） */
  id?: string
  label?: string
  /** source 路径表达式（args.x / result.y / output.z / error / duration_ms / partial_output） */
  source?: string
  language?: string
  collapsible?: boolean
  defaultExpanded?: boolean
  /** 条件：falsy → 整块不渲染（when 的补集为 unless） */
  when?: string
  /** 条件：truthy → 整块不渲染（when 的补集，用于 if/else 互斥分支的另一侧） */
  unless?: string
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
  /**
   * 文件路径 source（如 'args.file_path'）。求值非空时，enhance 会注入
   * activity.filePath + onOpenFile，使卡片标题可点击打开文件（等价手写 hasFilePath）。
   */
  filePathSource?: string
  /**
   * 头部增删行数徽标 source（如 file_write 的 +X -Y）。addedSource / removedSource
   * 各支持 `||` 路径回退；两者求值均为 number 时才产出 diffStat（对齐 extractWriteDiff）。
   */
  diffStat?: { addedSource: string; removedSource: string }
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

/** 沿单条点路径取值（result/output 经 safeParse） */
function evalSinglePath(ctx: ToolCallContext, path: string): unknown {
  const [root, ...rest] = path.split('.')
  let current: unknown
  switch (root) {
    case 'args':
      current = ctx.args
      break
    case 'result':
      // 字符串 result 优先按 Python dict/JSON 解析（供 result.x 取值）；
      // 解析失败（如 file_read/bash 的纯文本输出）回退为原始字符串，避免内容丢失。
      current =
        typeof ctx.result === 'string' ? (safeParseResult(ctx.result) ?? ctx.result) : ctx.result
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

/**
 * 沿路径取值，支持 `||` 路径回退。
 *
 * `'output.added || result.added'` → 依次求值每条点路径，返回第一个非 undefined 的结果
 * （空串 '' 视为有效非 undefined 值，会返回）。用于兼容「output 子层包装」与「扁平结构」
 * 两种数据形态（如 file_write 的 resultData 可能是 {output:{...}} 或扁平 {...}）。
 */
export function evalPath(ctx: ToolCallContext, path?: string): unknown {
  if (!path) return undefined
  if (path.includes('||')) {
    for (const alt of path.split('||').map((s) => s.trim())) {
      if (!alt) continue
      const v = evalSinglePath(ctx, alt)
      if (v !== undefined) return v
    }
    return undefined
  }
  return evalSinglePath(ctx, path)
}

/**
 * 求值 source 表达式（可选 `| filter:arg` 管道，与模板引擎同语义）。
 *
 * - 无过滤器：返回 evalPath 原始值（保留对象类型，供 json 块等直接消费）
 * - 有过滤器：字符串化后依次应用（first_line / truncate:N / basename / hostname / default:xxx）
 *
 * 用于内容块（text/code/markdown/log）的 source，如 fetch 的 `result | truncate:500`。
 */
export function evalSource(ctx: ToolCallContext, expr?: string): unknown {
  if (!expr) return undefined
  const parts = expr.split('|').map((s) => s.trim())
  const value = evalPath(ctx, parts[0])
  if (parts.length === 1) return value
  let str = value == null ? '' : String(value)
  for (let i = 1; i < parts.length; i++) {
    const colon = parts[i].indexOf(':')
    const fname = (colon >= 0 ? parts[i].slice(0, colon) : parts[i]).trim()
    const arg = colon >= 0 ? parts[i].slice(colon + 1).trim() : undefined
    const filter = FILTERS[fname]
    str = filter ? filter(str, arg) : str
  }
  return str
}

const TEMPLATE_RE = /\{\{\s*([^}]+?)\s*\}\}/g

/** 渲染 {{ expr | filter:arg }} 模板 */
export function renderTemplate(template: string, ctx: ToolCallContext): string {
  return template.replace(TEMPLATE_RE, (_m, expr: string) => {
    const [path, ...filterParts] = expr.split('|').map((s) => s.trim())
    const value = evalPath(ctx, path)
    // 即使 source 为 null/undefined 也走过滤器管道，使 default:xxx 能兜底缺失字段
    let str = value == null ? '' : String(value)
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
  const base = {
    id: decl.id,
    label,
    collapsible: decl.collapsible,
    defaultExpanded: decl.defaultExpanded,
  }

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
      const content = toStr(evalSource(ctx, decl.source))
      if (content === '') return null
      return { ...base, contentType: decl.type as DetailContentType, content }
    }
    case 'code': {
      const content = toStr(evalSource(ctx, decl.source))
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
  /** 由 filePathSource 求值得到的文件路径（供 enhance 注入点击打开行为） */
  filePath?: string
  /** 由 diffStat 声明求值得到的增删统计（供 enhance 注入头部 +X -Y 徽标） */
  diffStat?: { added: number; removed: number }
}

// ── 声明注册表：toolName → chat_card 声明（从 /api/v1/schema 的 tools[].ui.chat_card 装载） ──
// 声明是静态的（每个工具一份），按名查比塞进每个 tool 事件更合理；后端只需在 ToolDescriptor
// 透传 ui 字段（已落地），前端在 schema 加载时填充本表。
const chatCardDeclarations = new Map<string, ChatCardDeclaration>()

/**
 * 从 schema.tools[].ui.chat_card 装载声明注册表（幂等：先清空再装）
 *
 * @param tools - /api/v1/schema 的 tools 字段（ToolDescriptor 序列化形态）
 */
export function loadChatCardDeclarations(
  tools: Array<{ name?: string; ui?: { chat_card?: ChatCardDeclaration } }>,
): void {
  chatCardDeclarations.clear()
  for (const t of tools) {
    if (t.name && t.ui?.chat_card) {
      chatCardDeclarations.set(t.name, t.ui.chat_card)
    }
  }
}

/** 按 toolName 查 chat_card 声明（供 enhanceActivityWithToolConfig 优先消费） */
export function getChatCardDeclaration(toolName: string): ChatCardDeclaration | undefined {
  return chatCardDeclarations.get(toolName)
}

/**
 * 追加单个工具的 chat_card 声明（不清空注册表）。
 *
 * 用途：内置工具声明在 schema 装载（loadChatCardDeclarations，会清空全表）之后追加，
 * 使内置卡片在 schema 热重载后依然生效，并覆盖同名 schema 声明（builtin 在上层）。
 */
export function addChatCardDeclaration(toolName: string, decl: ChatCardDeclaration): void {
  chatCardDeclarations.set(toolName, decl)
}

/** 清空声明注册表（测试 / 销毁用） */
export function clearChatCardDeclarations(): void {
  chatCardDeclarations.clear()
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
    // when（falsy 跳过）与 unless（truthy 跳过）互补，支持 if/else 互斥分支
    if (block.when !== undefined && !evalTruthy(ctx, block.when)) continue
    if (block.unless !== undefined && evalTruthy(ctx, block.unless)) continue
    const translated = translateBlock(block, ctx)
    if (translated) details.push(translated)
  }

  const actions: ActivityAction[] = (decl.actions ?? []).map((a) => ({
    id: a.id,
    icon: null,
    label: a.label,
  }))

  // filePathSource 求值 → 交由 enhance 注入 activity.filePath + onOpenFile
  let filePath: string | undefined
  if (decl.filePathSource) {
    const v = evalPath(ctx, decl.filePathSource)
    if (v != null && v !== '') filePath = String(v)
  }

  // diffStat 求值：addedSource/removedSource 各支持 `||` 回退；两者均为 number 才产出
  // （对齐 extractWriteDiff：仅 added/removed 同为 number 时视为有效统计）
  let diffStat: { added: number; removed: number } | undefined
  if (decl.diffStat) {
    const added = evalPath(ctx, decl.diffStat.addedSource)
    const removed = evalPath(ctx, decl.diffStat.removedSource)
    if (typeof added === 'number' && typeof removed === 'number') {
      diffStat = { added, removed }
    }
  }

  return {
    title: decl.title ? renderTemplate(decl.title, ctx) : undefined,
    icon: decl.icon,
    summary: decl.summary ? renderTemplate(decl.summary, ctx) : undefined,
    details,
    actions,
    filePath,
    diffStat,
  }
}
