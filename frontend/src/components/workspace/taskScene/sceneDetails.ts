/**
 * 现场活动 → 展示块（复用既有卡型渲染管线，不自研终端/差异视图）
 *
 * 现场页展开区显示的内容与对话里的工具卡片**同源**：先把 `SceneActivity` 还原成
 * `MessageToolCall`，走 `toolCallToActivity`（声明级联：render 声明 → chat_card
 * 声明 → output_schema 契约 → 数据路由 → L0 推断），拿到 `ActivityDetailBlock[]`
 * 后交给既有 `DetailBlock` 渲染——终端块、差异块、搜索块等一个都不新写。
 *
 * 两处补强（现场页特有，仍走既有件）：
 * - 命令类（bash）补 render 意图终端块：`renderIntentToBlocks({card:'terminal'})`
 *   的 canonical payload 映射，保证「完整命令行 + exit N + stdout」齐备；
 * - 截断结果按既有截断契约解析（`resolveTruncatedToolResultForDisplay`），
 *   展开时经 `expandTruncatedToolResult` 回取全量。
 */

import { toolCallToActivity } from '@/utils/activityConverter'
import {
  buildRenderContext,
  renderIntentToBlocks,
} from '@/utils/renderIntent'
import {
  buildTruncatedResultDetailBlock,
  resolveTruncatedToolResultForDisplay,
} from '@/utils/toolResultTruncate'
import { extractResultText } from './sceneResultText'
import type { SceneActivity } from './sceneActivities'
import type { ActivityDetailBlock, ActivityStatus } from '@/types/activity'
import type { MessageToolCall, ToolCallStatus } from '@/types/models'

/** part 状态 → 工具调用状态（详情块渲染只看 args/result，状态仅占位） */
function toToolCallStatus(state: SceneActivity['state']): ToolCallStatus {
  switch (state) {
    case 'done':
      return 'completed'
    case 'error':
      return 'failed'
    case 'cancelled':
      return 'cancelled'
    default:
      return 'running'
  }
}

/** 活动条目 → MessageToolCall（既有转换器/意图路由的入参形态） */
export function buildSceneToolCall(activity: SceneActivity): MessageToolCall {
  return {
    call_id: activity.callId,
    tool_name: activity.toolName,
    tool_args: activity.args,
    status: toToolCallStatus(activity.state),
    ...(activity.result !== undefined ? { result: activity.result } : {}),
    ...(activity.resultData !== undefined ? { resultData: activity.resultData } : {}),
    ...(activity.error !== undefined ? { error: activity.error } : {}),
    ...(typeof activity.durationMs === 'number' ? { duration_ms: activity.durationMs } : {}),
    ...(activity.partialOutput ? { partialOutput: activity.partialOutput } : {}),
    ...(activity.startedAt ? { started_at: activity.startedAt } : {}),
    ...(activity.containerTaskId ? { containerTaskId: activity.containerTaskId } : {}),
  }
}

/** 活动条目 → 活动状态（行状态图标/左边条用） */
export function toActivityStatus(activity: SceneActivity): ActivityStatus {
  switch (activity.state) {
    case 'done':
      return 'completed'
    case 'error':
      return 'failed'
    case 'cancelled':
      return 'cancelled'
    case 'calling':
      return 'running'
    default:
      return 'running'
  }
}

/** 需要把结果正文单独成块的类别（设备类：正文即现场） */
const DEVICE_TEXT_CATEGORIES: ReadonlySet<SceneActivity['category']> = new Set([
  'desktop',
  'phone',
  'browser',
])

/**
 * 活动条目 → 展开区详情块。
 *
 * @param activity 现场活动
 * @param onExpand 截断结果的回填回调（缺省时截断块不可展开，仅显示预览）
 */
export function buildSceneDetailBlocks(
  activity: SceneActivity,
  onExpand?: () => Promise<boolean>,
): ActivityDetailBlock[] {
  // 截断标记先解析：渲染分支按数据形状路由，标记对象进去会产出垃圾视图
  const resolved = resolveTruncatedToolResultForDisplay(activity.result, activity.resultData)
  const toolCall = buildSceneToolCall({
    ...activity,
    result: resolved.result,
    resultData: resolved.resultData,
  })

  const activityData = toolCallToActivity(toolCall, { actions: [] })
  const blocks: ActivityDetailBlock[] = [...(activityData.details ?? [])]

  // 命令类：保证终端块在场（声明缺失时也可见完整命令行 + stdout + exit）
  if (activity.category === 'terminal' && !blocks.some((b) => b.contentType === 'terminal')) {
    const terminalBlocks = renderIntentToBlocks(
      { card: 'terminal' },
      buildRenderContext(toolCall),
    )
    blocks.unshift(...terminalBlocks)
  }

  // 设备类正文（桌面 Snapshot 元素树 / 手机 tree / 浏览器快照文本）：
  // 通用 JSON 折叠块只列字段名，看不到正文；现场页要的正是一句话之外的正文，
  // 故按已知结果形状抽文本单独成块（抽不到不加，不造假内容）
  if (DEVICE_TEXT_CATEGORIES.has(activity.category)) {
    const text = extractResultText(resolved.resultData ?? resolved.result)
    if (text && !blocks.some((b) => b.id === 'scene-result-text')) {
      const label =
        activity.category === 'desktop'
          ? '元素树 / 快照文本'
          : activity.category === 'phone'
            ? '手机元素/输出文本'
            : '页面文本'
      blocks.push({
        id: 'scene-result-text',
        label,
        content: text,
        contentType: 'text',
        collapsible: true,
        defaultExpanded: true,
      })
    }
  }

  // 实时输出增量（tool_progress 累积；终态后作为补充详情）
  if (activity.partialOutput && activity.partialOutput.length > 0) {
    blocks.push({
      id: 'partial-output',
      label: '实时输出',
      content: activity.partialOutput.join('\n'),
      contentType: 'text',
      collapsible: true,
      defaultExpanded: false,
    })
  }

  // 截断结果：预览 + 展开回取全量（既有契约）
  if (resolved.mark) {
    const expand = onExpand ?? (async () => false)
    blocks.push(buildTruncatedResultDetailBlock(resolved.mark, expand))
  }

  // 失败信封（错误信息属现场一部分，展示在展开区，不静默）
  if (activity.error !== undefined && activity.error !== null) {
    blocks.push({
      id: 'error',
      label: '错误',
      content: formatError(activity.error),
      contentType: 'text',
      collapsible: false,
    })
  }

  return blocks
}

/** 错误展示文本（统一信封取 message，旧形态直接字符串化） */
export function formatError(error: unknown): string {
  if (typeof error === 'string') return error
  if (typeof error === 'object' && error !== null) {
    const message = (error as { message?: unknown }).message
    if (typeof message === 'string' && message !== '') return message
    try {
      return JSON.stringify(error, null, 2)
    } catch {
      return String(error)
    }
  }
  return String(error)
}
