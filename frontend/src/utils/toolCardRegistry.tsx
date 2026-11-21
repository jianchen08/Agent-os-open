/**
 * 工具卡片渲染配置注册表
 *
 * 为不同工具的活动卡片提供差异化的渲染和交互配置。
 * 每个工具可以配置：自定义图标、标题格式化、详情区块、操作按钮。
 *
 * @module toolCardRegistry
 */

import { resolveChatCardIcon } from './chatCardIconRegistry'
import { interpretChatCard, getChatCardDeclaration, isSafeOpenUrl, type ToolCallContext } from './chatCardInterpreter'
import { buildOutputSchemaView, getOutputSchema } from './outputSchemaView'
import {
  applyDataDrivenIntent,
  applyRenderIntent,
  buildRenderContext,
  deriveCardMeta,
  deriveGenericSummary,
  getRenderIntent,
  inferRenderIntent,
} from './renderIntent'
import {
  buildTruncatedResultDetailBlock,
  truncatedSizeLabel,
  type TruncatedToolResult,
} from './toolResultTruncate'
import type { ActivityData, ActivityDetailBlock } from '@/types/activity'
import type { MessageToolCall } from '@/types/models'

/**
 * 注入文件打开能力（filePath + onOpenFile）。
 *
 * onOpenFile 第二参为 record 上的 containerTaskId；调用方可通过 options.onOpenFile
 * 接管并改用当前 Tab 的 taskId（优先于 record 值）。
 */
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

/** 打开任务执行管道时的任务附随信息（标签命名/任务定位用） */
export interface TaskPipelineInfo {
  /** 任务 ID（task=pipeline 统一后与管道 id 同值；透传给导航建标签） */
  taskId?: string
  /** 任务标题（导航后子标签名） */
  title?: string
}

// ── 全局任务管道打开回调（task_card 协议宿主，与文件打开回调同范式）──
let globalOnOpenTaskPipeline:
  | ((pipelineId: string, info?: TaskPipelineInfo) => void | Promise<void>)
  | null = null

/**
 * 注册全局任务管道打开回调
 *
 * 在应用启动时调用一次，宿主 = main.tsx（经 pipelineNavigator 跳转，
 * 与任务管理页「打开对话」同一通道）。
 */
export function registerGlobalOpenTaskPipelineCallback(
  callback: (pipelineId: string, info?: TaskPipelineInfo) => void | Promise<void>,
): void {
  globalOnOpenTaskPipeline = callback
}

/**
 * 获取全局任务管道打开回调
 */
export function getGlobalOpenTaskPipelineCallback(): (
  pipelineId: string,
  info?: TaskPipelineInfo,
) => void | Promise<void> {
  return globalOnOpenTaskPipeline || ((_pipelineId: string, _info?: TaskPipelineInfo) => {
    console.warn('[toolCardRegistry] 未注册任务管道打开回调，请在应用启动时调用 registerGlobalOpenTaskPipelineCallback')
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
      // 兜底新标签打开与 open_url 同协议白名单：preview src 可能来自插件
      // 输出（不可信面），javascript:/data: 等协议不得经 window.open 执行。
      if (!isSafeOpenUrl(src)) return
      window.open(src, '_blank', 'noopener,noreferrer')
    })
  )
}

/**
 * 注入文件打开能力（filePath + onOpenFile）。
 *
 * onOpenFile 第二参为 record 上的 containerTaskId；调用方可通过 options.onOpenFile
 * 接管并改用当前 Tab 的 taskId（优先于 record 值）。
 */
function injectFileOpen(
  activity: ActivityData,
  filePath: string,
  toolCall: MessageToolCall,
  options?: { onOpenFile?: (filePath: string, containerTaskId?: string) => void | Promise<void> },
): ActivityData {
  const openFileCallback = options?.onOpenFile || getGlobalOpenFileCallback()
  const recordTaskId = toolCall.containerTaskId
  return {
    ...activity,
    filePath,
    onOpenFile: () => openFileCallback(filePath, recordTaskId),
  }
}

// ── 全局插件页面打开回调（render.open_page 协议宿主，与文件打开回调同范式）──
let globalOnOpenPanel: ((pageId: string, itemId?: string) => void | Promise<void>) | null = null

/**
 * 注册全局插件页面打开回调（宿主 = main.tsx：openPluginPage 幂等开页 +
 * widget.event 下行定位条目）。
 */
export function registerGlobalOpenPanelCallback(
  callback: (pageId: string, itemId?: string) => void | Promise<void>,
): void {
  globalOnOpenPanel = callback
}

/** 获取全局插件页面打开回调（未注册时告警，与文件打开回调同口径） */
export function getGlobalOpenPanelCallback(): (pageId: string, itemId?: string) => void | Promise<void> {
  return globalOnOpenPanel || ((_pageId: string, _itemId?: string) => {
    console.warn('[toolCardRegistry] 未注册插件页面打开回调，请在应用启动时调用 registerGlobalOpenPanelCallback')
  })
}

/**
 * 从工具结果提取定位条目 id（open_page 跳转的目标条目）。
 *
 * 通用规则（不绑工具名）：结果对象按 `result.todo.id → result.record.id →
 * result.item.id → todo.id → record.id → item.id → id` 顺序取首个非空字符串
 * （pim capture 回执实体在 result.todo 内）；resultData（流式结构化完整数据）
 * 优先，回退 result 文本字段（历史消息从 DB 加载的场景）。缺失 = 只开页不
 * 定位（合法形态）。
 */
export function extractPanelItemId(toolCall: MessageToolCall): string | undefined {
  const idPaths = [
    ['result', 'todo', 'id'],
    ['result', 'record', 'id'],
    ['result', 'item', 'id'],
    ['todo', 'id'],
    ['record', 'id'],
    ['item', 'id'],
    ['id'],
  ] as const
  const candidates = [toolCall.resultData, toolCall.result]
  for (const raw of candidates) {
    let obj: unknown = raw
    if (typeof raw === 'string') {
      try {
        obj = JSON.parse(raw)
      } catch {
        continue
      }
    }
    if (!obj || typeof obj !== 'object') continue
    const rec = obj as Record<string, unknown>
    for (const path of idPaths) {
      let current: unknown = rec
      for (const key of path) {
        if (!current || typeof current !== 'object') {
          current = undefined
          break
        }
        current = (current as Record<string, unknown>)[key]
      }
      if (typeof current === 'string' && current) return current
    }
  }
  return undefined
}

/**
 * 注入插件页面打开能力（panelPageId/panelPageItemId/panelPageLabel + onOpenPanel）。
 *
 * 声明驱动：pageId 来自工具 render.open_page 声明；条目 id 从结果实体提取
 * （缺失 = 只开页不定位）。页面未装载/插件禁用时由调用方（applyCardMeta 的
 * intent 分支）天然不走到这里——零注入零按钮。
 */
function injectPanelOpen(
  activity: ActivityData,
  page: { page: string; label?: string | undefined },
  toolCall: MessageToolCall,
  options?: { onOpenPanel?: (pageId: string, itemId?: string) => void | Promise<void> },
): ActivityData {
  const openPanelCallback = options?.onOpenPanel || getGlobalOpenPanelCallback()
  const itemId = extractPanelItemId(toolCall)
  return {
    ...activity,
    panelPageId: page.page,
    panelPageLabel: page.label ?? '打开',
    ...(itemId ? { panelPageItemId: itemId } : {}),
    onOpenPanel: () => openPanelCallback(page.page, itemId),
  }
}

/**
 * render 意图分支（声明/数据路由）的条目增强：标题人性化 + 摘要行 +
 * 文件打开入口注入 + 任务管道打开入口注入。
 *
 * 双路由落地后全量工具走 render 分支早退——filePath/onOpenFile 与
 * pipelineId/onOpenPipeline 注入及标题人性化必须在此分支补齐（读文件卡片
 * 可打开文件、任务卡片可跳转执行管道、条目显示人性化标题）。
 */
function applyCardMeta(
  enhanced: ActivityData,
  base: ActivityData,
  toolCall: MessageToolCall,
  options?: {
    onOpenFile?: (filePath: string, containerTaskId?: string) => void | Promise<void>
    onOpenTaskPipeline?: (pipelineId: string, info?: TaskPipelineInfo) => void | Promise<void>
    onOpenPanel?: (pageId: string, itemId?: string) => void | Promise<void>
  },
): ActivityData {
  const ctx = buildRenderContext(toolCall)
  // 声明意图优先；数据路由分支（applyDataDrivenIntent 产物）按数据形状重推
  const intent = getRenderIntent(base.toolName!) ?? inferRenderIntent(ctx)
  const meta = intent ? deriveCardMeta(ctx, intent) : {}
  const humanizedTitle = enhanced.title === base.title ? humanizeToolName(base.toolName!) : enhanced.title
  const result: ActivityData = {
    ...enhanced,
    // render 分支不改标题：沿用工具名人性化（与 L0/契约视图口径一致）；
    // meta.title（file_card = 文件名）声明在先
    title: meta.title ?? humanizedTitle,
  }
  if (meta.summary) {
    result.subtitle = meta.summary
  }
  if (meta.size !== undefined) {
    result.size = meta.size
  }
  if (meta.diffStat) {
    result.diffStat = meta.diffStat
  }
  if (meta.icon) {
    const Icon = resolveChatCardIcon(meta.icon)
    result.customIcon = <Icon className="h-icon-md w-icon-md" />
  }
  if (meta.filePath) {
    return injectFileOpen(result, meta.filePath, toolCall, options)
  }
  if (meta.pipelineId) {
    const openTaskCallback = options?.onOpenTaskPipeline || getGlobalOpenTaskPipelineCallback()
    const pipelineId = meta.pipelineId
    return {
      ...result,
      pipelineId,
      onOpenPipeline: () => openTaskCallback(pipelineId, { taskId: meta.taskId, title: meta.title }),
    }
  }
  // render.open_page 声明 → 打开插件页面（条目定位 id 从结果实体提取；与
  // filePath/pipelineId 同注入位，页面未装载时 intent 本身不存在=零注入）
  if (intent?.openPage) {
    return injectPanelOpen(result, intent.openPage, toolCall, options)
  }
  return result
}

/**
 * 内容依赖卡判定：卡型的块产出依赖结果全文（old_content/new_content/content），
 * 结果被数据面截断（超 8KB → 截断标记）后 payload 产不出块，级联会跌落到
 * chat_card 代码块兜底（纯文本堆砌，diff/打开入口全丢）。此类工具截断态不走
 * 级联，走 enhanceTruncatedContentCard 一级「加载全文」块。
 */
export function isContentDependentRenderCard(toolName: string): boolean {
  return getRenderIntent(toolName)?.card === 'file_card'
}

/** file_card 截断态的加载块 label 前缀（总量经 truncatedSizeLabel 保留）。 */
const TRUNCATED_FILE_CARD_LABEL_PREFIX = '差异对比'

/**
 * 截断态内容依赖卡（file_card）的条目增强：
 *
 * - 详情区**替换**（非追加）为单个一级「差异对比（展开加载全文）」块——
 *   标准级联在此场景只会产出 chat_card 代码块堆砌（写入内容全文等），
 *   与载入后的真实 diff 卡形同义重复且抢占首屏；
 * - 折叠 chip 元信息照常注入（applyCardMeta：标题=文件名、摘要=路径、
 *   打开入口经 args.path 字段族兜底——结果侧 file/added/removed 已随截断
 *   丢失，载入回填后由标准渲染链补齐徽标与绝对路径）；
 * - onExpand 接既有单条回取回填（toolResultBackfill），成功后 part 的
 *   resultData 恢复全量，重渲染自然落回标准 file_card 管线。
 */
export function enhanceTruncatedContentCard(
  activity: ActivityData,
  toolCall: MessageToolCall,
  mark: TruncatedToolResult,
  onExpand: () => Promise<boolean>,
  options?: {
    onOpenFile?: (filePath: string, containerTaskId?: string) => void | Promise<void>
  },
): ActivityData {
  // details 清空（截断态级联块不可信），meta 面照常派生
  const stripped: ActivityData = { ...activity, details: [] }
  const withMeta = applyCardMeta(stripped, activity, toolCall, options)
  const label = `${TRUNCATED_FILE_CARD_LABEL_PREFIX}（共 ${truncatedSizeLabel(mark.totalBytes)}），展开加载全文`
  return {
    ...withMeta,
    details: [buildTruncatedResultDetailBlock(mark, onExpand, label)],
  }
}

/** call_tool 转发壳名（动态工具面单入口，plugins/shared/search） */
const DYNAMIC_FORWARD_SHELL_TOOL = 'call_tool'

/** call_tool 动态转发卡的条目标注（头部标题旁，区分目标原生调用） */
const DYNAMIC_FORWARD_SUBTITLE = '经 call_tool 动态转发'

/** 从结果数据包读目标工具名：metadata.target_tool 契约键优先，壳成功路径合并
 *  进数据包的 tool_name 次之；不可得返回空串。 */
function readTargetToolFromResultData(resultData: unknown): string {
  if (typeof resultData !== 'object' || resultData === null) return ''
  const record = resultData as Record<string, unknown>
  const metadata = record.metadata
  if (typeof metadata === 'object' && metadata !== null) {
    const target = (metadata as Record<string, unknown>).target_tool
    if (typeof target === 'string' && target) return target
  }
  const toolName = record.tool_name
  return typeof toolName === 'string' ? toolName : ''
}

/**
 * call_tool 动态转发目标解析（2026-11-18 用户裁定：前端展示实际被调用的目标
 * 工具，不是调用工具的工具）。
 *
 * 目标名解析链（逐级回退）：
 * 1. `resultData.metadata.target_tool` —— 后端 tool_results 信封的契约键
 *    （aaa42f4d9；上行投影到位时直接命中）；
 * 2. `resultData.tool_name` —— 壳成功路径合并进数据包的目标名（当前 WS 事件
 *    与冷路径 HTTP 投影实际可观测的形态）；
 * 3. `tool_args.tool` —— 失败信封（TOOL_NOT_FOUND 等）不携带目标名，声明侧
 *    目标名是唯一来源；仅在结果已回（终态证据在场）时启用，在途卡不启用。
 *
 * 名字不可解析（在途/中断/老数据无目标名）返回 null，调用方保持现状渲染。
 */
export function resolveDynamicForwardTarget(
  toolCall: Pick<MessageToolCall, 'tool_name' | 'tool_args' | 'result' | 'resultData' | 'error'>,
): { targetTool: string; targetArgs: Record<string, unknown> } | null {
  if (toolCall.tool_name !== DYNAMIC_FORWARD_SHELL_TOOL) return null
  const hasTerminalEvidence =
    toolCall.result != null || toolCall.resultData != null || toolCall.error != null
  if (!hasTerminalEvidence) return null

  const fromResult = readTargetToolFromResultData(toolCall.resultData)
  const declared = toolCall.tool_args?.tool
  const targetTool = fromResult || (typeof declared === 'string' && declared ? declared : '')
  if (!targetTool) return null

  const forwardedArgs = toolCall.tool_args?.arguments
  const targetArgs =
    typeof forwardedArgs === 'object' && forwardedArgs !== null
      ? (forwardedArgs as Record<string, unknown>)
      : {}
  return { targetTool, targetArgs }
}

/**
 * call_tool 动态转发展示改写：标题显示目标工具名（原始名，如 snippet_box），
 * 条目摘要换转发标注，参数区按目标实参重建（外层信封 {tool, arguments} 不进
 * 卡面）；结果区维持既有结果（壳成功信封的 data 本就是目标输出透传，无外壳）。
 * 声明级联仍按 call_tool 自身配置运行（不冒充目标原生卡，靠标注区分）。
 * 2026-11-19 用户裁定追加：置 indirectCall 徽标位——头部渲染「间接调用」标记
 * （ActivityCard），比摘要行更醒目地表明该卡是经壳转发而非原生直接调用。
 */
function applyDynamicForwardDisplay(activity: ActivityData, toolCall: MessageToolCall): ActivityData | null {
  const resolved = resolveDynamicForwardTarget(toolCall)
  if (!resolved) return null
  return {
    ...activity,
    title: resolved.targetTool,
    subtitle: DYNAMIC_FORWARD_SUBTITLE,
    indirectCall: true,
    details: inferDefaultDetails({ ...toolCall, tool_args: resolved.targetArgs }),
  }
}

/**
 * 使用工具配置增强 ActivityData
 *
 * 在 toolCallToActivity 转换后调用，用工具配置覆盖/增强默认渲染。
 * call_tool 动态转发卡在声明级联后按目标工具改写展示（见
 * applyDynamicForwardDisplay）。各声明分支（render/chat_card/契约/L0）都未
 * 产出条目摘要时，按通用对象字段族兜底推导一句话摘要（form/table 卡与无声明
 * 工具的条目信息量对齐 file_read/write 卡）。
 */
export function enhanceActivityWithToolConfig(
  activity: ActivityData,
  toolCall: MessageToolCall,
  options?: {
    onOpenFile?: (filePath: string, containerTaskId?: string) => void | Promise<void>
    onOpenTaskPipeline?: (pipelineId: string, info?: TaskPipelineInfo) => void | Promise<void>
    onOpenPanel?: (pageId: string, itemId?: string) => void | Promise<void>
  },
): ActivityData {
  const enhanced = enhanceWithDeclarations(activity, toolCall, options)
  const forwarded = enhanced.type === 'tool_call' ? applyDynamicForwardDisplay(enhanced, toolCall) : null
  if (forwarded) return forwarded
  if (enhanced.type !== 'tool_call' || !enhanced.toolName || enhanced.subtitle) {
    return enhanced
  }
  const summary = deriveGenericSummary(buildRenderContext(toolCall))
  return summary ? { ...enhanced, subtitle: summary } : enhanced
}

/**
 * 声明级联增强（render 声明 → chat_card 声明 → output_schema 契约 → 数据路由
 * → L0 推断），不含通用摘要兜底。
 */
function enhanceWithDeclarations(
  activity: ActivityData,
  toolCall: MessageToolCall,
  options?: {
    onOpenFile?: (filePath: string, containerTaskId?: string) => void | Promise<void>
    onOpenTaskPipeline?: (pipelineId: string, info?: TaskPipelineInfo) => void | Promise<void>
    onOpenPanel?: (pageId: string, itemId?: string) => void | Promise<void>
  },
): ActivityData {
  if (activity.type !== 'tool_call' || !activity.toolName) {
    return activity
  }

  // 声明路由：插件 render 声明（ToolDescriptor.render，schema 装载进
  // renderIntent 注册表）——工具作者对输出形态的契约，最高优先。
  const rendered = applyRenderIntent(activity, toolCall)
  if (rendered) {
    return applyCardMeta(rendered, activity, toolCall, options)
  }

  // 插件 ui.chat_card 声明（后端经 /api/v1/schema 的 tools[].ui.chat_card
  // 透传，前端在 schema 加载时装入注册表）→ 解释器翻译成 ActivityDetailBlock[]，
  // ActivityCard 原样渲染。无声明时回退下面的手写 registry / L0 推断。
  const declared = getChatCardDeclaration(activity.toolName)
  if (declared) {
    const ctx: ToolCallContext = {
      args: toolCall.tool_args,
      // resultData 优先：流式结构化的完整数据（未经截断）；其缺失时回退
      // result 文本字段（历史消息从 DB 加载或工具本身无结构化结果的场景）。
      result: toolCall.resultData ?? toolCall.result,
      error: toolCall.error,
      duration_ms: toolCall.duration_ms,
      partial_output: toolCall.partialOutput,
      container_task_id: toolCall.containerTaskId,
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
    }    // 声明 diffStat 求值产出 → 注入 activity.diffStat（等价手写 buildDiffStat，头部 +X -Y 徽标）
    if (interpreted.diffStat) {
      enhanced.diffStat = interpreted.diffStat
    }
    return enhanced
  }

  // 工具契约结构化视图（widget 化 T4）：带 output_schema 但无 render/chat_card
  // 声明的工具，结果按 schema 渲染只读结构化表单 + 契约违规标警（fail-closed
  // 前端镜像，权威校验在内核 tool_core）。有 render 声明的工具不进本分支——
  // 声明卡产不出块（如 file_read 目录结果无 content）时级联数据路由按形状
  // 重推，而非回落 schema 表单（一字段数组会被折叠成 json 区，表格视图丢失）。
  const outputSchema = getOutputSchema(activity.toolName)
  if (outputSchema && !getRenderIntent(activity.toolName)) {
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
  // 永远优先；未命中落 L0 通用数据渲染。
  const dataDriven = applyDataDrivenIntent(activity, toolCall)
  if (dataDriven) {
    return applyCardMeta(dataDriven, activity, toolCall, options)
  }

  // L0：无声明时的自动推断渲染——标题人性化 + 参数/结果按数据类型推断成内容块
  return {
    ...activity,
    title: humanizeToolName(activity.toolName),
    details: inferDefaultDetails(toolCall),
  }
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
/** 中文映射表（导出仅供契约测试与 manifest 工具名对账；运行时行为不变） */
export const TOOL_NAME_ZH: Record<string, string> = {
  file_read: '读取文件',
  file_write: '写入文件',
  bash: '执行命令',
  log: '读取命令日志',
  input: '发送进程输入',
  kill: '终止进程',
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
