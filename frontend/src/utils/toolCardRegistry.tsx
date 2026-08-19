/**
 * 工具卡片渲染配置注册表
 *
 * 为不同工具的活动卡片提供差异化的渲染和交互配置。
 * 每个工具可以配置：自定义图标、标题格式化、详情区块、操作按钮。
 *
 * @module toolCardRegistry
 */

import type { ActivityAction, ActivityData, ActivityDetailBlock } from '@/types/activity'
import type { MessageToolCall } from '@/types/models'
import type { ReactNode } from 'react'
import { interpretChatCard } from './chatCardInterpreter'
import { getChatCardDeclaration } from './chatCardInterpreter'
import { applyRenderIntent, applyDataDrivenIntent } from './dshRenderIntent'
import { resolveChatCardIcon } from './chatCardIconRegistry'
import { buildOutputSchemaView, getOutputSchema } from './outputSchemaView'
import type { ToolCallContext } from './chatCardInterpreter'

/**
 * 工具卡片渲染配置
 */
export interface ToolCardConfig {
  /** 工具名称 */
  name: string
  /** 自定义图标 */
  icon?: ReactNode
  /** 自定义标题（传入 toolCall 参数，返回显示标题） */
  formatTitle?: (toolCall: MessageToolCall) => string
  /** 构建详情区块（传入 toolCall，返回详情区块列表） */
  buildDetails?: (toolCall: MessageToolCall) => ActivityDetailBlock[]
  /** 构建操作按钮（传入 toolCall，返回操作按钮列表） */
  buildActions?: (toolCall: MessageToolCall) => ActivityAction[]
  /** 构建头部增删行数徽标（如 file_write 的 +X -Y），返回 undefined 则不展示 */
  buildDiffStat?: (toolCall: MessageToolCall) => { added: number; removed: number } | undefined
  /** 自定义样式类名 */
  className?: string
  /** 运行状态的自定义颜色（CSS 色值），用于区分阻塞等待用户的工具 */
  runningColor?: string
  /** 是否关联文件（为 true 时自动从参数中提取文件路径） */
  hasFilePath?: boolean
}

/**
 * 注册表：工具名 → 渲染配置
 */
const registry = new Map<string, ToolCardConfig>()

/** 全局文件打开回调（支持 containerTaskId） */
let globalOnOpenFile: ((filePath: string, containerTaskId?: string) => void | Promise<void>) | null = null

/**
 * 注册全局文件打开回调
 *
 * 在应用启动时调用一次，用于设置文件打开的统一处理逻辑。
 *
 * @param callback - 文件打开回调函数
 */
export function registerGlobalOpenFileCallback(
  callback: (filePath: string, containerTaskId?: string) => void | Promise<void>,
): void {
  globalOnOpenFile = callback
}

/**
 * 获取全局文件打开回调
 */
export function getGlobalOpenFileCallback(): (filePath: string, containerTaskId?: string) => void | Promise<void> {
  return globalOnOpenFile || ((_containerTaskId?: string) => {
    console.warn('[toolCardRegistry] 未注册文件打开回调，请在应用启动时调用 registerGlobalOpenFileCallback')
  })
}

// ── 全局图片预览回调（chat_card actions on_click preview_image 协议宿主，widget 化 T3）──
let globalImagePreview: ((src: string) => void) | null = null

/**
 * 注册全局图片预览回调（传 null 恢复缺省行为）。
 *
 * 宿主：main.tsx 挂载的 ImagePreviewHost（全屏灯箱）。缺省兜底：新标签打开。
 */
export function registerGlobalImagePreviewCallback(callback: ((src: string) => void) | null): void {
  globalImagePreview = callback
}

/** 获取全局图片预览回调（未注册时兜底新标签打开） */
export function getGlobalImagePreviewCallback(): (src: string) => void {
  return (
    globalImagePreview ||
    ((src: string) => {
      window.open(src, '_blank', 'noopener,noreferrer')
    })
  )
}

/**
 * 注册工具卡片配置
 */
export function registerToolCard(config: ToolCardConfig): void {
  registry.set(config.name, config)
}

/**
 * 获取工具卡片配置
 */
export function getToolCardConfig(toolName: string): ToolCardConfig | undefined {
  return registry.get(toolName)
}

/**
 * 使用工具配置增强 ActivityData
 *
 * 在 toolCallToActivity 转换后调用，用工具配置覆盖/增强默认渲染
 */
export function enhanceActivityWithToolConfig(
  activity: ActivityData,
  toolCall: MessageToolCall,
  options?: {
    onOpenFile?: (filePath: string, containerTaskId?: string) => void | Promise<void>
  },
): ActivityData {
  if (activity.type !== 'tool_call' || !activity.toolName) {
    return activity
  }

  // 声明路由：插件 render 声明（ToolDescriptor.render，schema 装载进
  // dshRenderIntent 注册表）——工具作者对输出形态的契约，最高优先。
  const rendered = applyRenderIntent(activity, toolCall)
  if (rendered) {
    return rendered
  }

  // 插件 ui.chat_card 声明（后端经 /api/v1/schema 的 tools[].ui.chat_card
  // 透传，前端在 schema 加载时装入注册表）→ 解释器翻译成 ActivityDetailBlock[]，
  // ActivityCard 原样渲染。无声明时回退下面的手写 registry / L0 推断。
  const declared = getChatCardDeclaration(activity.toolName)
  if (declared) {
    const ctx: ToolCallContext = {
      args: toolCall.tool_args,
      // resultData 优先（流式结构化完整数据，未截断）；缺失时回退 result（历史/兜底）。
      // 优先级为 resultData ?? result（resultData 优先）——对齐 file_write diff 数据来源
      // 语义；已迁工具 sample 仅设 result 不设 resultData，故翻转不影响其等价测试。
      result: toolCall.resultData ?? toolCall.result,
      error: toolCall.error,
      duration_ms: toolCall.duration_ms,
      partial_output: toolCall.partialOutput,
    }
    const interpreted = interpretChatCard(declared, ctx)
    const Icon = resolveChatCardIcon(interpreted.icon)
    const enhanced: ActivityData = {
      ...activity,
      title: interpreted.title ?? activity.title,
      customIcon: interpreted.icon ? <Icon className="h-icon-md w-icon-md" /> : activity.customIcon,
      details: interpreted.details.length > 0 ? interpreted.details : activity.details,
      actions: interpreted.actions.length > 0 ? interpreted.actions : activity.actions,
    }
    // 声明 filePathSource 求值非空 → 注入 filePath + onOpenFile（等价手写 hasFilePath，
    // 使 ActivityCard 头部标题可点击打开文件）
    if (interpreted.filePath) {
      enhanced.filePath = interpreted.filePath
      const openFileCallback = options?.onOpenFile || getGlobalOpenFileCallback()
      const recordTaskId = toolCall.containerTaskId
      enhanced.onOpenFile = () => openFileCallback(interpreted.filePath as string, recordTaskId)
    }
    // 声明 diffStat 求值产出 → 注入 activity.diffStat（等价手写 buildDiffStat，头部 +X -Y 徽标）
    if (interpreted.diffStat) {
      enhanced.diffStat = interpreted.diffStat
    }
    return enhanced
  }

  // 工具契约结构化视图（widget 化 T4）：带 output_schema 但无 render/chat_card
  // 声明的工具，结果按 schema 渲染只读结构化表单 + 契约违规标警（fail-closed
  // 前端镜像，权威校验在内核 tool_core）。显式声明已在上文优先返回。
  const outputSchema = getOutputSchema(activity.toolName)
  if (outputSchema) {
    const view = buildOutputSchemaView(
      outputSchema,
      toolCall.result,
      toolCall.resultData,
    )
    if (view) {
      const enhancedContract: ActivityData = {
        ...activity,
        title: humanizeToolName(activity.toolName),
        details: [view.block, ...inferDefaultDetails(toolCall)],
      }
      if (view.violations.length > 0) {
        enhancedContract.error = `output_schema 契约校验（前端镜像）：\n${view.violations.join('\n')}`
      }
      return enhancedContract
    }
  }

  // 数据路由：无任何插件声明（render/chat_card）时按数据形状自动匹配渲染组件
  // （diff 形状 → diff 组件、stdout+exit_code → terminal 组件等）。插件显式声明
  // 永远优先；未命中落下方手写 registry / L0 通用数据渲染。
  const dataDriven = applyDataDrivenIntent(activity, toolCall)
  if (dataDriven) {
    return dataDriven
  }

  const config = getToolCardConfig(activity.toolName)
  if (!config) {
    // L0：无声明时的自动推断渲染——标题人性化 + 参数/结果按数据类型推断成内容块
    return {
      ...activity,
      title: humanizeToolName(activity.toolName),
      details: inferDefaultDetails(toolCall),
    }
  }

  const enhanced = { ...activity }

  if (config.formatTitle) {
    enhanced.title = config.formatTitle(toolCall)
  }

  if (config.buildDetails) {
    enhanced.details = config.buildDetails(toolCall)
  }

  if (config.buildDiffStat) {
    enhanced.diffStat = config.buildDiffStat(toolCall)
  }

  if (config.buildActions) {
    enhanced.actions = config.buildActions(toolCall)
  }

  if (config.icon) {
    enhanced.customIcon = config.icon
  }

  if (config.className) {
    enhanced.customClassName = config.className
  }

  if (config.runningColor) {
    enhanced.customColor = config.runningColor
  }

  // 自动提取文件路径并注入打开文件回调
  if (config.hasFilePath) {
    const filePath = extractFilePath(toolCall)
    if (filePath) {
      enhanced.filePath = filePath
      // onOpenFile 第二参为 record 上的 containerTaskId；调用方可通过 options.onOpenFile
      // 接管并改用当前 Tab 的 taskId（优先于 record 值）。
      const openFileCallback = options?.onOpenFile || getGlobalOpenFileCallback()
      const recordTaskId = toolCall.containerTaskId
      enhanced.onOpenFile = () => openFileCallback(filePath, recordTaskId)
    }
  }

  return enhanced
}

/** ========== L0 自动推断（无声明配置时，让默认卡片也"按数据渲染"）========== */

const IMAGE_EXT_RE = /\.(png|jpe?g|webp|gif|svg|bmp)$/i
const URL_RE = /^https?:\/\/\S+$/i
/** 常见"文件路径"参数名 */
const FILE_PATH_KEYS = new Set([
  'file_path',
  'path',
  'output_file',
  'screenshot_path',
  'save_path',
  'target_file',
  'source_file',
  'file',
])

/** 判断字符串是否像文件路径（含分隔符、盘符或已知扩展名） */
function looksLikePath(value: string): boolean {
  return (
    /(?:^[a-zA-Z]:[\\/]|^\/|^\.{1,2}[\\/]|^[^/\\]*[\\/][^/\\])/.test(value) ||
    /\.\w{1,6}$/.test(value)
  )
}

/** 常用工具名的中文显示映射（L0 标题人性化） */
const TOOL_NAME_ZH: Record<string, string> = {
  file_read: '读取文件',
  file_write: '写入文件',
  bash_execute: '执行命令',
  web_search: '网页搜索',
  fetch: '访问网页',
  task_submit: '提交任务',
  task_manage: '任务管理',
  human_interaction: '人工交互',
}

/** 工具名人性化：中文映射优先，其次下划线转空格 + 首字母大写 */
function humanizeToolName(toolName: string): string {
  if (TOOL_NAME_ZH[toolName]) return TOOL_NAME_ZH[toolName]
  return toolName
    .split('_')
    .map((w) => (w ? w[0].toUpperCase() + w.slice(1) : w))
    .join(' ')
}

interface KvEntry {
  key: string
  value: string
}

/**
 * 对单个字段值推断内容块：URL→link、图片路径→image、路径→file、
 * 长文本→code 折叠、标量→kv 收集、对象/数组→json 折叠
 */
function pushInferredBlocks(
  key: string,
  value: unknown,
  kv: KvEntry[],
  blocks: ActivityDetailBlock[],
): void {
  if (typeof value === 'string') {
    const str = value
    if (str && URL_RE.test(str)) {
      blocks.push({
        id: `link-${key}`,
        label: key,
        content: '',
        contentType: 'link',
        url: str,
        collapsible: false,
      })
    } else if (str && IMAGE_EXT_RE.test(str) && looksLikePath(str)) {
      blocks.push({
        id: `image-${key}`,
        label: key,
        content: '',
        contentType: 'image',
        path: str,
        collapsible: false,
      })
    } else if (FILE_PATH_KEYS.has(key) && str && looksLikePath(str)) {
      blocks.push({
        id: `file-${key}`,
        label: key,
        content: '',
        contentType: 'file',
        path: str,
        collapsible: false,
      })
    } else if (str.length > 120 || str.includes('\n')) {
      blocks.push({
        id: `code-${key}`,
        label: key,
        content: str,
        contentType: 'code',
        collapsible: true,
        defaultExpanded: false,
      })
    } else {
      kv.push({ key, value: str })
    }
    return
  }

  if (typeof value === 'number' || typeof value === 'boolean' || value === null || value === undefined) {
    kv.push({ key, value: String(value) })
    return
  }

  // 对象 / 数组 → json 折叠块
  blocks.push({
    id: `json-${key}`,
    label: key,
    content: value as Record<string, unknown>,
    contentType: 'json',
    collapsible: true,
    defaultExpanded: false,
  })
}

/** 把扁平记录按类型推断成内容块（args/result 通用） */
function inferRecordBlocks(prefix: string, record: Record<string, unknown>): ActivityDetailBlock[] {
  const blocks: ActivityDetailBlock[] = []
  const kv: KvEntry[] = []
  for (const [key, value] of Object.entries(record)) {
    pushInferredBlocks(key, value, kv, blocks)
  }
  if (kv.length > 0) {
    blocks.unshift({
      id: `${prefix}-kv`,
      label: prefix,
      content: '',
      contentType: 'kv',
      kvItems: kv,
      collapsible: false,
    })
  }
  return blocks
}

/** L0 默认详情：参数 / 结果按数据类型推断渲染，输出流走 log 块 */
function inferDefaultDetails(toolCall: MessageToolCall): ActivityDetailBlock[] {
  const details: ActivityDetailBlock[] = []

  const args = toolCall.tool_args as Record<string, unknown> | null
  if (args && Object.keys(args).length > 0) {
    details.push(...inferRecordBlocks('参数', args))
  }

  if (toolCall.result !== undefined && toolCall.result !== null) {
    const parsed = safeParseResult(toolCall.result)
    if (parsed) {
      details.push(...inferRecordBlocks('结果', parsed))
    } else if (typeof toolCall.result === 'string') {
      const str = toolCall.result
      const isLong = str.length > 120
      details.push({
        id: 'result-text',
        label: '结果',
        content: str,
        contentType: isLong ? 'code' : 'text',
        collapsible: isLong,
        defaultExpanded: false,
      })
    }
  }

  if (toolCall.partialOutput && toolCall.partialOutput.length > 0) {
    details.push({
      id: 'output',
      label: '执行输出',
      content: toolCall.partialOutput.join('\n'),
      contentType: 'log',
      collapsible: false,
    })
  }

  return details
}

function extractFilePath(toolCall: MessageToolCall): string {
  const args = toolCall.tool_args as Record<string, unknown> | null
  if (!args) return ''
  // 不同工具使用不同参数名（read_file 用 file_path，其他用 path），
  // 此处兼容多种工具定义，非 API 格式兼容
  return (args.file_path as string) || (args.path as string) || ''
}

/**
 * 安全解析可能是 Python dict 字符串的结果
 *
 * 处理 tc.result 可能的多种格式：
 * 1. 已经是对象 → 直接返回
 * 2. 标准 JSON 字符串 → JSON.parse 解析
 * 3. Python dict 字符串（单引号、True/False/None）→ 替换后解析
 * 4. 解析失败 → 返回 null
 *
 * @param result - 工具调用的返回结果，可能是对象或字符串
 * @returns 解析后的对象，或解析失败时返回 null
 */
export function safeParseResult(result: unknown): Record<string, unknown> | null {
  // 已经是对象，直接返回
  if (result !== null && result !== undefined && typeof result === 'object') {
    return result as Record<string, unknown>
  }

  // 非字符串无法解析
  if (typeof result !== 'string') {
    return null
  }

  const str = result.trim()
  if (!str) return null

  // 第一次尝试：标准 JSON 解析
  try {
    const parsed = JSON.parse(str)
    if (parsed && typeof parsed === 'object') {
      return parsed as Record<string, unknown>
    }
  } catch {
    // 不是标准 JSON，继续尝试 Python dict 格式
  }

  // 第二次尝试：Python dict 格式（单引号 → 双引号，True/False/None → JSON 值）
  try {
    let normalized = str
    // 将 Python 布尔值和 None 替换为 JSON 兼容值
    normalized = normalized.replace(/\bTrue\b/g, 'true')
    normalized = normalized.replace(/\bFalse\b/g, 'false')
    normalized = normalized.replace(/\bNone\b/g, 'null')
    // 将单引号替换为双引号（注意：这只是简单替换，对嵌套引号场景可能有局限）
    normalized = normalized.replace(/'/g, '"')
    const parsed = JSON.parse(normalized)
    if (parsed && typeof parsed === 'object') {
      return parsed as Record<string, unknown>
    }
  } catch {
    // Python dict 格式也解析失败
  }

  return null
}

/**
 * human_interaction 工具卡片配置
 *
 * 运行状态使用主色（primary），视觉上区分阻塞等待用户的交互工具与普通执行工具
 */
registerToolCard({
  name: 'human_interaction',
  runningColor: 'hsl(var(--primary))',
})

export default registry
