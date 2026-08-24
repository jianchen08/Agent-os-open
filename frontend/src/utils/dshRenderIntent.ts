/**
 * 渲染路由器（原 render 意图路由层，task_dsh_plugin_adapter 任务 1d + 3b 泛化）。
 *
 * 工具卡片渲染采用「双路由」：
 * 1. **声明路由**：插件在 plugin.json 的 capabilities.tools[].render 直接声明渲染形式
 *    （对齐 DSH ToolResultView 词汇表：card = terminal|diff|read|web|search|generic|
 *    image|file|table|form + bindings + title），工具结果按声明路由到对应渲染组件。
 * 2. **数据路由**：无声明时按结果/参数的数据形状自动匹配渲染组件（结果含
 *    old/new 文本对 → diff；含 stdout+exit_code → terminal；含 lines+content → read；
 *    含 url → web；图片扩展名路径 → image；文件路径 → file；表头+二维数组 → table）。
 * 3. 均未命中返回 null，调用方落通用数据渲染（kv/json/code，非工具级硬编码）。
 *
 * 本模块全部是**纯函数**（除注册表装载/查询外无副作用）——数据映射
 * （灵汐 toolCall/resultData → 渲染组件 props）字段绑定可经 render.bindings
 * 覆盖，默认按灵汐工具约定的字段名族（args.command/result.output|stdout/…）解析。
 */

import type { ActivityData, ActivityDetailBlock } from '@/types/activity'
import type { MessageToolCall } from '@/types/models'

/** 渲染形式词汇表：声明路由（插件配置）与数据路由（形状推断）共用。 */
export type RenderIntentCard =
  | 'terminal'
  | 'diff'
  | 'read'
  | 'web'
  | 'search'
  | 'generic'
  | 'image'
  | 'file'
  | 'table'
  | 'form'

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

/** 宽松 JSON → ToolRenderIntent（card 非法即弃，防止坏声明崩渲染）。 */
function normalizeRenderIntent(raw: Record<string, unknown> | undefined): ToolRenderIntent | undefined {
  if (!raw || typeof raw !== 'object') return undefined
  const card = raw.card
  const CARDS: readonly string[] = [
    'terminal', 'diff', 'read', 'web', 'search', 'generic', 'image', 'file', 'table', 'form',
  ]
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
  const path = str(pickField('path', ['result.path', 'result.file', 'args.file_path', 'args.path'], ctx, intent))
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
  // 灵汐 file_write 族：old_content/new_content 单文件对（path 缺省 ''，
  // diff 内容才是关键——数据路由对「仅有 old/new 无 path」的结果也判 diff 形状）
  const path = str(pickField('path', ['args.file_path', 'result.path', 'args.path'], ctx, intent)) ?? ''
  const oldText = pickField('oldText', ['result.old_content', 'result.oldText'], ctx, intent)
  const newText = pickField('newText', ['result.new_content', 'result.newText'], ctx, intent)
  if (typeof newText === 'string' || typeof oldText === 'string') {
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

// ── 新增卡（image / file / table / form）payload 构造器 ──────────────────

const IMAGE_EXT_RE = /\.(png|jpe?g|webp|gif|svg|bmp)$/i

/** 结果/参数中的路径候选（file/image 卡共用字段族）。 */
const PATH_FIELDS = ['result.file_path', 'result.path', 'result.file', 'args.file_path', 'args.path'] as const

/** image 卡 payload：路径以图片扩展名结尾（媒体产物/截图）。 */
export function imagePayload(ctx: RenderContext, intent: ToolRenderIntent): Record<string, unknown> | null {
  const path = str(pickField('path', PATH_FIELDS, ctx, intent))
  if (!path || !IMAGE_EXT_RE.test(path)) return null
  return { path }
}

/** file 卡 payload：结果中的文件路径（下载产物/文件操作），排除 URL。 */
export function filePayload(ctx: RenderContext, intent: ToolRenderIntent): Record<string, unknown> | null {
  const path = str(pickField('path', PATH_FIELDS, ctx, intent))
  if (!path || /^https?:\/\//i.test(path)) return null
  return { path }
}

/** 二维数组行 → 字符串行（table 卡共用）。 */
function rowsToStrings(rows: unknown): string[][] | null {
  if (!Array.isArray(rows)) return null
  const mapped: string[][] = []
  for (const row of rows) {
    if (Array.isArray(row)) {
      mapped.push(row.map((c) => (c == null ? '' : String(c))))
    } else if (typeof row === 'object' && row !== null) {
      // 对象行（无表头）→ 值列
      mapped.push(Object.values(row as Record<string, unknown>).map((c) => (c == null ? '' : String(c))))
    } else {
      return null
    }
  }
  return mapped
}

/**
 * table 卡 payload（表头数组 + 二维数组）：
 * - 形态 A：result 里 `*_h`（string[] 表头）+ `*_d`（string[][] 行）配对（resource_search 表格）；
 * - 形态 B：result.d 二维数组（task_manage 列表，无表头 → 列1..N）；
 * - 形态 C：result.items 对象数组（list_directory，列名 = 对象 key 并集）。
 */
export function tablePayload(ctx: RenderContext, intent: ToolRenderIntent): Record<string, unknown> | null {
  const result = ctx.result
  if (result && typeof result === 'object') {
    // 形态 A：*_h + *_d 配对
    for (const [key, value] of Object.entries(result)) {
      if (key.endsWith('_d') && Array.isArray(value)) {
        const headerKey = `${key.slice(0, -2)}_h`
        const header = result[headerKey]
        if (Array.isArray(header) && header.every((h) => typeof h === 'string')) {
          const rows = rowsToStrings(value)
          if (rows) return { columns: header as string[], rows }
        }
      }
    }
    // 形态 C：items 对象数组（列名 = 首行 key 序）
    const items = pickField('items', ['result.items'], ctx, intent)
    if (Array.isArray(items) && items.length > 0 && items.every((i) => typeof i === 'object' && i !== null)) {
      const first = items[0] as Record<string, unknown>
      const columns = Object.keys(first)
      const rows = items.map((i) => {
        const obj = i as Record<string, unknown>
        return columns.map((c) => (obj[c] == null ? '' : String(obj[c])))
      })
      return { columns, rows }
    }
    // 形态 B：result.d 二维数组（无表头）
    const d = pickField('d', ['result.d'], ctx, intent)
    if (Array.isArray(d)) {
      const rows = rowsToStrings(d)
      if (rows && rows.length > 0) {
        const width = Math.max(...rows.map((r) => r.length))
        const columns = Array.from({ length: width }, (_, i) => `列${i + 1}`)
        return { columns, rows }
      }
    }
  }
  return null
}

/** 常用字段名中文化（form 卡 label 翻译）。 */
const FIELD_LABEL_ZH: Record<string, string> = {
  // 任务域
  task_id: '任务ID', title: '标题', status: '状态', message: '消息', warning: '警告',
  target_id: '目标Agent', target_type: '目标类型', priority: '优先级', max_retries: '最大重试',
  task_scope: '任务范围', workspace: '工作空间', workspace_mode: '空间拓扑', isolation_level: '隔离级别',
  parent_task_id: '父任务ID', acceptance_criteria: '验收标准', goal_title: '任务目标',
  goal_description: '任务描述', pipeline_id: '管道ID', overall_passed: '评估通过', summary: '摘要',
  metrics: '评估指标', reason: '原因', action: '操作',
  // 文件/命令/网络
  file_path: '文件路径', path: '路径', command: '命令', output: '输出', exit_code: '退出码',
  url: 'URL', status_code: '状态码', size: '大小', duration: '耗时', avg_speed: '平均速度',
  count: '数量', success: '成功', error: '错误', error_code: '错误码', hint: '提示',
}

function zhLabel(key: string): string {
  return FIELD_LABEL_ZH[key] ?? key
}

/**
 * form 卡 payload：表单式布局（kv 标量 + json 折叠区）。
 *
 * - 有 bindings：按 bindings 字段序收集（值路径 `result.x` / `args.x`）；
 * - 无 bindings：收集 args 标量（提交参数）+ result 标量（结果），长文本/对象/数组入 jsonItems。
 * 标量 = string(<120 且无换行) / number / boolean；其余入 jsonItems（折叠）。
 */
export function formPayload(
  ctx: RenderContext,
  intent: ToolRenderIntent,
): Record<string, unknown> | null {
  const kvItems: { key: string; value: string }[] = []
  const jsonItems: { label: string; content: unknown }[] = []
  const bindings = intent.bindings

  const collect = (source: 'args' | 'result', key: string, path: string): void => {
    const v = resolvePath(source, path, ctx)
    if (v === undefined) return
    const label = zhLabel(key)
    if (typeof v === 'string' && v.length < 120 && !v.includes('\n')) {
      kvItems.push({ key: label, value: v })
    } else if (typeof v === 'number' || typeof v === 'boolean') {
      kvItems.push({ key: label, value: String(v) })
    } else {
      jsonItems.push({ label, content: v })
    }
  }

  if (bindings && Object.keys(bindings).length > 0) {
    for (const [field, path] of Object.entries(bindings)) {
      const source: 'args' | 'result' = path.startsWith('args.') ? 'args' : 'result'
      const p = path.slice(path.indexOf('.') + 1)
      collect(source, field, p)
    }
  } else {
    for (const source of ['args', 'result'] as const) {
      const root = source === 'args' ? ctx.args : ctx.result
      if (root && typeof root === 'object') {
        for (const [key, value] of Object.entries(root)) {
          if (value === undefined || value === null) continue
          collect(source, key, key)
        }
      }
    }
  }

  if (kvItems.length === 0 && jsonItems.length === 0) return null
  return { kvItems, jsonItems }
}

// ── 数据路由：按数据形状推断渲染形式（无声明时） ────────────────────────

/**
 * 按数据形状保守匹配渲染形式（强特征优先，避免误判）：
 * diff（old/new 文本对）→ terminal（args.command）→ read（lines/content+path）→
 * search（files/paths/results）→ web（url/sources）→ image（图片扩展名路径）→
 * file（结果文件路径）→ table（*_h/*_d 或 d 二维数组或 items 对象数组）。
 * 未命中返回 undefined（调用方落通用数据渲染）。
 */
export function inferRenderIntent(ctx: RenderContext): ToolRenderIntent | undefined {
  const noop: ToolRenderIntent = { card: 'generic' }
  const diff = diffPayload(ctx, noop)
  if (diff) return { card: 'diff' }
  const terminal = terminalPayload(ctx, noop)
  if (terminal) return { card: 'terminal' }
  const read = readPayload(ctx, noop)
  if (read) return { card: 'read' }
  const search = searchPayload(ctx, noop)
  if (search) return { card: 'search' }
  const web = webPayload(ctx, noop)
  if (web) return { card: 'web' }
  const image = imagePayload(ctx, noop)
  if (image) return { card: 'image' }
  const file = filePayload(ctx, noop)
  if (file) return { card: 'file' }
  const table = tablePayload(ctx, noop)
  if (table) return { card: 'table' }
  return undefined
}

// ── 条目元信息（summary/filePath）提取 ────────────────────────────────

/** render 意图派生的条目元信息：summary = 头部摘要行，filePath = 可打开文件。 */
export interface CardMeta {
  /** 头部摘要（命令/路径/URL 等一句话，折叠态可见） */
  summary?: string
  /** 可在工作区打开的文件路径（存在时条目提供打开入口） */
  filePath?: string
}

/** search 卡的查询词字段族（args 侧）。 */
const QUERY_FIELDS = ['args.query', 'args.pattern', 'args.keyword', 'args.q', 'args.search', 'args.regex'] as const

/**
 * 按 render 意图提取条目元信息（纯函数，与 payload 构造器同字段族）。
 * read/file/image/diff → filePath + summary；terminal/web/search → summary；
 * table/form/generic → 无（返回空对象）。
 */
export function deriveCardMeta(ctx: RenderContext, intent: ToolRenderIntent): CardMeta {
  const path = str(pickField('path', [...PATH_FIELDS], ctx, intent))
  switch (intent.card) {
    case 'read':
    case 'file':
    case 'image':
      return path ? { summary: path, filePath: path } : {}
    case 'diff': {
      // 多文件 diff（diffs 数组）：摘要取首文件路径 + 计数，不给打开入口；
      // 单文件对（file_write 字族）→ path 字段族直取，可打开
      const diffs = pickField('diffs', ['result.diffs'], ctx, intent)
      if (Array.isArray(diffs) && diffs.length > 0) {
        const paths = diffs
          .filter((d): d is Record<string, unknown> => typeof d === 'object' && d !== null)
          .map(d => str(d.path) ?? str(d.file_path))
          .filter((p): p is string => p !== undefined)
        if (paths.length > 1) {
          return { summary: `${paths[0]} 等 ${paths.length} 个文件` }
        }
        if (paths.length === 1) {
          return { summary: paths[0], filePath: paths[0] }
        }
        return {}
      }
      return path ? { summary: path, filePath: path } : {}
    }
    case 'terminal': {
      const command = str(pickField('command', ['args.command', 'args.cmd'], ctx, intent))
      return command !== undefined ? { summary: command } : {}
    }
    case 'web': {
      const url = str(pickField('url', ['result.url', 'args.url'], ctx, intent))
      return url !== undefined ? { summary: url } : {}
    }
    case 'search': {
      const query = str(pickField('query', [...QUERY_FIELDS], ctx, intent))
      return query !== undefined ? { summary: query } : {}
    }
    default:
      return {}
  }
}

// ── 主入口：渲染意图 → ActivityDetailBlock[] ────────────────────────

const PAYLOAD_BUILDERS: Record<Exclude<RenderIntentCard, 'generic'>, (ctx: RenderContext, intent: ToolRenderIntent) => Record<string, unknown> | null> = {
  terminal: terminalPayload,
  diff: diffPayload,
  read: readPayload,
  search: searchPayload,
  web: webPayload,
  image: imagePayload,
  file: filePayload,
  table: tablePayload,
  form: formPayload,
}

/** generic：无专门卡（保持现有级联渲染，声明只影响标题）。 */
export function renderIntentToBlocks(
  intent: ToolRenderIntent,
  ctx: RenderContext,
): ActivityDetailBlock[] {
  if (intent.card === 'generic') return []
  const payload = PAYLOAD_BUILDERS[intent.card](ctx, intent)
  if (payload === null) return []

  // 新卡：image/file 落现有块类型；table/form 落结构化块（由 ActivityCard 消费）
  switch (intent.card) {
    case 'image':
    case 'file': {
      const path = typeof payload.path === 'string' ? payload.path : ''
      return [{ label: intent.title ?? (intent.card === 'image' ? '图片' : '文件'), content: '', contentType: intent.card, path }]
    }
    case 'table': {
      const columns = Array.isArray(payload.columns) ? payload.columns as string[] : []
      const rows = Array.isArray(payload.rows) ? payload.rows as string[][] : []
      return [{
        label: intent.title ?? '表格',
        content: '',
        contentType: 'table',
        table: { columns, rows },
        collapsible: true,
        defaultExpanded: true,
      }]
    }
    case 'form': {
      const kvItems = Array.isArray(payload.kvItems) ? payload.kvItems as { key: string; value: string }[] : []
      const jsonItems = Array.isArray(payload.jsonItems) ? payload.jsonItems as { label: string; content: unknown }[] : []
      return [{
        label: intent.title ?? '详情',
        content: '',
        contentType: 'form',
        kvItems,
        jsonItems,
        collapsible: true,
        defaultExpanded: true,
      }]
    }
    default:
      break
  }

  return [{
    // read/terminal 等卡的 payload 自带 label（文件路径/命令），优先于意图 title
    label: (typeof payload.label === 'string' && payload.label !== '' ? payload.label : intent.title) ?? intent.card,
    content: payload,
    contentType: `dsh:${intent.card}` as ActivityDetailBlock['contentType'],
    dshProps: payload,
  }]
}

/**
 * 增强入口（toolCardRegistry.enhanceActivityWithToolConfig 的**声明路由**分支）。
 * 仅处理插件显式声明的 render 意图；无声明返回 null（调用方继续走 chat_card
 * 声明 → 数据路由 → 通用数据渲染级联）。
 */
export function applyRenderIntent(activity: ActivityData, toolCall: MessageToolCall): ActivityData | null {
  if (activity.type !== 'tool_call' || !activity.toolName) return null
  const intent = getRenderIntent(activity.toolName)
  if (!intent) return null
  const ctx = buildRenderContext(toolCall)
  const blocks = renderIntentToBlocks(intent, ctx)
  if (blocks.length === 0) return null
  return {
    ...activity,
    details: blocks,
  }
}

/**
 * 增强入口（**数据路由**分支）：无任何插件声明（render/chat_card）时按数据形状
 * 推断渲染形式。由 toolCardRegistry 在 chat_card 声明之后调用——插件显式声明
 * 永远优先于自动推断。未命中返回 null（调用方落手写 registry / L0 通用渲染）。
 */
export function applyDataDrivenIntent(activity: ActivityData, toolCall: MessageToolCall): ActivityData | null {
  if (activity.type !== 'tool_call' || !activity.toolName) return null
  const intent = inferRenderIntent(buildRenderContext(toolCall))
  if (!intent) return null
  const blocks = renderIntentToBlocks(intent, buildRenderContext(toolCall))
  if (blocks.length === 0) return null
  return {
    ...activity,
    details: blocks,
  }
}

export function buildRenderContext(toolCall: MessageToolCall): RenderContext {
  return {
    args: (toolCall.tool_args ?? undefined) as SourceData,
    result: (toolCall.resultData ?? toolCall.result ?? undefined) as SourceData,
    error: toolCall.error ?? null,
    duration_ms: toolCall.duration_ms ?? null,
  }
}
