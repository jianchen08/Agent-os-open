/**
 * render 意图路由层（task_dsh_plugin_adapter 任务 1d + 任务 3b）。
 *
 * 后端工具契约的 `render` 字段（对齐 DSH ToolResultView 词汇表：
 * card = terminal|diff|read|web|search|generic）在此消费：工具结果按声明
 * 路由到 DSH vendor 组件（frontend/src/components/vendor/dsh/），未声明时
 * 回退现有级联（chat_card 声明 → 手写 registry → L0 推断）。
 *
 * 本模块全部是**纯函数**（除注册表装载/查询外无副作用）——数据映射
 * （灵汐 toolCall/resultData → DSH 卡片 props）与 DSH 侧 card model
 * （read-card-model 等）同构：字段绑定可经 render.bindings 覆盖，默认按
 * 灵汐工具约定的字段名族（args.command/result.output|stdout/…）解析。
 */

import type { ActivityData, ActivityDetailBlock } from '@/types/activity'
import type { MessageToolCall } from '@/types/models'

/** DSH ToolResultView 的 card 词汇表。 */
export type RenderIntentCard = 'terminal' | 'diff' | 'read' | 'web' | 'search' | 'generic'

/**
 * 工具 render 声明（ToolDescriptor.render 的前端形态）。
 * bindings：卡片字段 → 取值路径（`args.x` / `result.y`），覆盖默认字段族。
 */
export interface ToolRenderIntent {
  card: RenderIntentCard
  bindings?: Record<string, string> | undefined
  /** 卡片标题覆盖（缺省用工具名/结果 path） */
  title?: string | undefined
}

/** render 意图注册表：toolName → 声明（schema 装载时填充）。 */
const renderIntents = new Map<string, ToolRenderIntent>()

/**
 * 从 /api/v1/schema 的 tools[]（ToolDescriptor 序列化形态）装载 render 意图。
 * 清空重装（对齐 loadChatCardDeclarations 语义）。
 */
export function loadRenderIntents(
  tools: Array<{ name?: string; render?: Record<string, unknown> }>,
): void {
  renderIntents.clear()
  for (const t of tools) {
    const intent = normalizeRenderIntent(t.render)
    if (t.name && intent) {
      renderIntents.set(t.name, intent)
    }
  }
}

/** 追加单个声明（不清空，测试/热补用）。 */
export function addRenderIntent(toolName: string, intent: ToolRenderIntent): void {
  renderIntents.set(toolName, intent)
}

/** 按 toolName 查 render 声明。 */
export function getRenderIntent(toolName: string): ToolRenderIntent | undefined {
  return renderIntents.get(toolName)
}

/** 清空（测试用）。 */
export function clearRenderIntents(): void {
  renderIntents.clear()
}

/** 宽松 JSON → ToolRenderIntent（card 非法即弃，防止坏声明崩渲染）。 */
function normalizeRenderIntent(raw: Record<string, unknown> | undefined): ToolRenderIntent | undefined {
  if (!raw || typeof raw !== 'object') return undefined
  const card = raw.card
  const CARDS: readonly string[] = ['terminal', 'diff', 'read', 'web', 'search', 'generic']
  if (typeof card !== 'string' || !CARDS.includes(card)) return undefined
  const intent: ToolRenderIntent = { card: card as RenderIntentCard }
  if (raw.bindings && typeof raw.bindings === 'object') {
    intent.bindings = raw.bindings as Record<string, string>
  }
  if (typeof raw.title === 'string') intent.title = raw.title
  return intent
}

// ── 取值路径解析（对齐 chatCardInterpreter 的 source 路径语义） ──────────

type SourceData = Record<string, unknown> | undefined | null

/** 按 `args.x` / `result.y` / `error` 路径取值（缺失返回 undefined）。 */
function resolvePath(source: 'args' | 'result' | 'error', path: string, ctx: RenderContext): unknown {
  const root: SourceData =
    source === 'args' ? ctx.args : source === 'result' ? ctx.result : null
  if (root == null || typeof root !== 'object') return undefined
  let current: unknown = root
  for (const key of path.split('.')) {
    if (current == null || typeof current !== 'object') return undefined
    current = (current as Record<string, unknown>)[key]
  }
  return current
}

/** bindings 优先的字段解析：多个候选路径取首个非 undefined。 */
function pickField(
  field: string,
  defaultPaths: readonly string[],
  ctx: RenderContext,
  intent: ToolRenderIntent,
): unknown {
  const bound = intent.bindings?.[field]
  if (bound) {
    const source: 'args' | 'result' | 'error' = bound.startsWith('args.')
      ? 'args'
      : bound.startsWith('error') ? 'error' : 'result'
    const path = bound.slice(bound.indexOf('.') + 1)
    const v = resolvePath(source, path, ctx)
    if (v !== undefined) return v
  }
  for (const p of defaultPaths) {
    const source: 'args' | 'result' = p.startsWith('args.') ? 'args' : 'result'
    const path = p.slice(p.indexOf('.') + 1)
    const v = resolvePath(source, path, ctx)
    if (v !== undefined) return v
  }
  return undefined
}

/** 渲染上下文（对齐 chatCardInterpreter 的 ToolCallContext 子集）。 */
export interface RenderContext {
  args: SourceData
  result: SourceData
  error?: string | null
  duration_ms?: number | null
}

// ── 卡片字段族默认路径（灵汐工具约定） ─────────────────────────────────

const str = (v: unknown): string | undefined => (typeof v === 'string' ? v : undefined)
const num = (v: unknown): number | undefined => (typeof v === 'number' && Number.isFinite(v) ? v : undefined)

/** terminal 卡 payload（DSH canonicalBashResult 字族 + 灵汐 bash_execute 字族）。 */
export function terminalPayload(ctx: RenderContext, intent: ToolRenderIntent): Record<string, unknown> | null {
  const command = str(pickField('command', ['args.command', 'args.cmd'], ctx, intent))
  if (command === undefined) return null
  const stdout = pickField('output', ['result.stdout.text', 'result.stdout', 'result.output'], ctx, intent)
  const stderr = pickField('stderr', ['result.stderr.text', 'result.stderr'], ctx, intent)
  const exit = num(pickField('exitCode', ['result.exit_code', 'result.exitCode'], ctx, intent))
  const output = [str(stdout) ?? '', str(stderr) ?? ''].filter(s => s !== '').join('\n')
  return {
    command,
    cwd: str(pickField('cwd', ['args.working_dir', 'args.cwd'], ctx, intent)),
    output,
    exitCode: exit,
    running: false,
  }
}

/** read 卡 payload（DSH read 结果 {path, offset, lines, totalLines} 字族）。 */
export function readPayload(ctx: RenderContext, intent: ToolRenderIntent): Record<string, unknown> | null {
  const path = str(pickField('path', ['result.path', 'args.file_path', 'args.path'], ctx, intent))
  if (path === undefined) return null
  let lines = pickField('lines', ['result.lines'], ctx, intent)
  if (!Array.isArray(lines)) {
    // 灵汐 file_read 族：content 字符串 + offset → 折成行号结构
    const content = str(pickField('content', ['result.content'], ctx, intent))
    const offset = num(pickField('offset', ['result.offset', 'args.offset'], ctx, intent)) ?? 1
    if (content !== undefined) {
      lines = content.split('\n').map((text, i) => ({ number: offset + i, text }))
    }
  }
  if (!Array.isArray(lines)) return null
  const mapped = lines
    .filter((l): l is Record<string, unknown> => typeof l === 'object' && l !== null)
    .map(l => ({ number: num(l.number) ?? 0, text: String(l.text ?? '') }))
  const totalLines = num(pickField('totalLines', ['result.totalLines', 'result.total_lines'], ctx, intent)) ?? mapped.length
  return { label: intent.title ?? path, lines: mapped, totalLines, lang: str(pickField('lang', ['result.lang'], ctx, intent)) }
}

/** diff 卡 payload（DSH FileDiff {path, oldText, newText}[] 字族 + 灵汐 file_write 老新对）。 */
export function diffPayload(ctx: RenderContext, intent: ToolRenderIntent): Record<string, unknown> | null {
  const diffs = pickField('diffs', ['result.diffs'], ctx, intent)
  if (Array.isArray(diffs)) {
    const mapped = diffs
      .filter((d): d is Record<string, unknown> => typeof d === 'object' && d !== null)
      .map(d => ({
        path: str(d.path) ?? str(d.file_path) ?? '',
        oldText: typeof d.oldText === 'string' ? d.oldText : (typeof d.old_text === 'string' ? d.old_text : null),
        newText: str(d.newText) ?? str(d.new_text) ?? '',
      }))
    if (mapped.length > 0) return { diffs: mapped }
  }
  // 灵汐 file_write 族：old_content/new_content 单文件对
  const path = str(pickField('path', ['args.file_path', 'result.path', 'args.path'], ctx, intent))
  const oldText = pickField('oldText', ['result.old_content', 'result.oldText'], ctx, intent)
  const newText = pickField('newText', ['result.new_content', 'result.newText'], ctx, intent)
  if (path !== undefined && (typeof newText === 'string' || typeof oldText === 'string')) {
    return {
      diffs: [{
        path,
        oldText: typeof oldText === 'string' ? oldText : null,
        newText: typeof newText === 'string' ? newText : '',
      }],
    }
  }
  return null
}

/** search 卡 payload（DSH SearchResultView：matches（分组行）/ paths（平铺））。 */
export function searchPayload(ctx: RenderContext, intent: ToolRenderIntent): Record<string, unknown> | null {
  // matches 形态：result.files[{path, matches:[{lineNumber,line}]}] 或 result.matches
  const groups = pickField('files', ['result.files'], ctx, intent)
  if (Array.isArray(groups)) {
    const files = groups
      .filter((g): g is Record<string, unknown> => typeof g === 'object' && g !== null)
      .map(g => ({
        path: str(g.path) ?? str(g.file) ?? str(g.filename) ?? '',
        matches: Array.isArray(g.matches)
          ? g.matches
              .filter((m): m is Record<string, unknown> => typeof m === 'object' && m !== null)
              .map(m => ({ lineNumber: num(m.lineNumber) ?? num(m.line_number) ?? 0, line: String(m.line ?? m.text ?? '') }))
          : [],
      }))
      .filter(g => g.path !== '' || g.matches.length > 0)
    if (files.length > 0) {
      return { kind: 'matches' as const, files, truncated: false, total: files.reduce((s, f) => s + f.matches.length, 0) }
    }
  }
  // paths 形态：result.paths（DSH glob）/ result.results[].path
  const pathsRaw = pickField('paths', ['result.paths'], ctx, intent)
  if (Array.isArray(pathsRaw) && pathsRaw.every(p => typeof p === 'string')) {
    return { kind: 'paths' as const, paths: pathsRaw as string[], truncated: false, total: pathsRaw.length }
  }
  const results = pickField('results', ['result.results'], ctx, intent)
  if (Array.isArray(results)) {
    const paths = results
      .map((r) => (typeof r === 'string' ? r : (typeof r === 'object' && r !== null ? str((r as Record<string, unknown>).path) : undefined)))
      .filter((p): p is string => p !== undefined)
    if (paths.length > 0) {
      return { kind: 'paths' as const, paths, truncated: false, total: paths.length }
    }
  }
  return null
}

/** web 卡 payload（DSH WebResultView：search（sources）/ fetch（url+status））。 */
export function webPayload(ctx: RenderContext, intent: ToolRenderIntent): Record<string, unknown> | null {
  const sources = pickField('sources', ['result.sources', 'result.results'], ctx, intent)
  if (Array.isArray(sources) && sources.length > 0) {
    const mapped = sources
      .filter((s): s is Record<string, unknown> => typeof s === 'object' && s !== null)
      .map(s => ({
        url: str(s.url) ?? str(s.link) ?? '',
        title: str(s.title) ?? str(s.name),
        snippet: str(s.snippet) ?? str(s.content),
        publishedAt: str(s.publishedAt) ?? str(s.published_at) ?? str(s.date),
      }))
      .filter(s => s.url !== '')
    if (mapped.length > 0) {
      return { kind: 'search' as const, answer: str(pickField('answer', ['result.answer', 'result.summary'], ctx, intent)), sources: mapped, truncated: false }
    }
  }
  const url = str(pickField('url', ['result.url', 'args.url'], ctx, intent))
  const statusCode = num(pickField('statusCode', ['result.status_code', 'result.statusCode', 'result.status'], ctx, intent))
  if (url !== undefined) {
    return { kind: 'fetch' as const, url, statusCode: statusCode ?? 200, truncated: false }
  }
  return null
}

// ── 主入口：render 意图 → ActivityDetailBlock[] ────────────────────────

const PAYLOAD_BUILDERS: Record<Exclude<RenderIntentCard, 'generic'>, (ctx: RenderContext, intent: ToolRenderIntent) => Record<string, unknown> | null> = {
  terminal: terminalPayload,
  diff: diffPayload,
  read: readPayload,
  search: searchPayload,
  web: webPayload,
}

/** generic：无专门卡（保持现有级联渲染，声明只影响标题）。 */
export function renderIntentToBlocks(
  intent: ToolRenderIntent,
  ctx: RenderContext,
): ActivityDetailBlock[] {
  if (intent.card === 'generic') return []
  const payload = PAYLOAD_BUILDERS[intent.card](ctx, intent)
  if (payload === null) return []
  return [{
    // read/terminal 等卡的 payload 自带 label（文件路径/命令），优先于意图 title
    label: (typeof payload.label === 'string' && payload.label !== '' ? payload.label : intent.title) ?? intent.card,
    content: payload,
    contentType: `dsh:${intent.card}` as ActivityDetailBlock['contentType'],
    dshProps: payload,
  }]
}

/**
 * 增强入口（toolCardRegistry.enhanceActivityWithToolConfig 的 render 意图分支）。
 * 返回原 activity 表示"无 render 声明/映射失败"，调用方回退现有级联。
 */
export function applyRenderIntent(activity: ActivityData, toolCall: MessageToolCall): ActivityData | null {
  if (activity.type !== 'tool_call' || !activity.toolName) return null
  const intent = getRenderIntent(activity.toolName)
  if (!intent) return null
  const ctx: RenderContext = {
    args: (toolCall.tool_args ?? undefined) as SourceData,
    result: (toolCall.resultData ?? toolCall.result ?? undefined) as SourceData,
    error: toolCall.error ?? null,
    duration_ms: toolCall.duration_ms ?? null,
  }
  const blocks = renderIntentToBlocks(intent, ctx)
  if (blocks.length === 0) return null
  return {
    ...activity,
    details: blocks,
  }
}
