/**
 * 现场实例派生（纯函数，无 store/网络依赖）
 *
 * 现场面板的主视图是「当前正在运行的实例 + 对应产物」，不是工具调用流水。
 * 实例 = **通道实例**：终端 / 浏览器 / 桌面 / 软件 / 手机，一类通道一个实例
 * （身份映射口径与 `utils/toolCategory.ts` 同一套）。
 *
 * 为什么按通道聚合而不是按目标（URL/窗口/应用）拆分：
 * - 消息面没有会话 id，按目标拆会把同一会话的连续动作切碎——例：`browser_navigate`
 *   带 url 而 `browser_take_screenshot` 不带，同一浏览器会话会被拆成两个实例并列；
 * - ZCode 的运行中实例也是通道级聚合（`runningBashWorks` / `runningSubagents`）；
 * - 目标（host / 窗口 / 应用）只作「当前焦点」展示，不作实例身份。
 *
 * 数据诚实边界：消息面只有 `tool_call` part，**没有内核进程注册表**（浏览器 PID、
 * 桌面会话句柄都不在数据面），所以「运行中」只能由调用生命周期推断——
 * `state ∈ {calling, streaming}` 且管道未结束。UI 上如实标注推断口径。
 *
 * `file` / `other` 类别不成实例（它们是动作不是会话）：不进看板，只在历史区
 * 按分组出现。
 */

import { SCENE_CATEGORY_LABELS, deriveSceneCategory } from '@/utils/toolCategory'
import type { SceneActivity } from './sceneActivities'
import type { SceneToolCategory } from '@/utils/toolCategory'

/** 可能成为实例的通道类别（有"活着"语义的五类） */
export const INSTANCE_CATEGORIES: readonly SceneToolCategory[] = [
  'terminal',
  'browser',
  'desktop',
  'software',
  'phone',
]

/** 该类别能否构成实例 */
export function isInstanceCategory(category: SceneToolCategory): boolean {
  return INSTANCE_CATEGORIES.includes(category)
}

/** 现场实例（一个通道会话及其产物） */
export interface SceneInstance {
  /** 实例键（= 通道类别，一类通道一个实例） */
  key: string
  /** 通道类别 */
  category: SceneToolCategory
  /** 实例名（类别名） */
  label: string
  /** 当前焦点（host / 窗口 / 应用；取不到为空串，不猜） */
  focus: string
  /** 是否还活着（存在在途调用且管道未结束） */
  live: boolean
  /** 当前动作（最新一条调用的行文案；无则空串） */
  current: string
  /** 该实例最早一条调用的时间（ISO；缺省 undefined） */
  startedAt?: string
  /** 该实例最近一条调用 */
  latest?: SceneActivity
  /** 该实例的全部调用（时序） */
  activities: SceneActivity[]
  /** 其中在途的调用数 */
  inflightCount: number
  /** 该实例产出的截图类活动（卡面缩略图 + 产物区截图段） */
  screenshots: SceneActivity[]
}

/** 调用是否在途（calling/streaming 两态） */
export function isInflight(activity: SceneActivity): boolean {
  return activity.state === 'calling' || activity.state === 'streaming'
}

/** 取字符串参数（去空白；非串/空串 → undefined） */
function strArg(args: Record<string, unknown>, key: string): string | undefined {
  const value = args[key]
  return typeof value === 'string' && value.trim() !== '' ? value.trim() : undefined
}

/** URL → host（非法 URL 返回 undefined，不猜） */
export function hostOf(url: string | undefined): string | undefined {
  if (!url) return undefined
  try {
    return new URL(url).host || undefined
  } catch {
    return undefined
  }
}

/** 浏览器焦点：先参数 url，再结果里回显的 url */
function browserFocus(activity: SceneActivity): string | undefined {
  const fromArgs = hostOf(strArg(activity.args, 'url'))
  if (fromArgs) return fromArgs
  const data = activity.resultData
  if (typeof data === 'object' && data !== null) {
    const url = (data as { url?: unknown }).url
    if (typeof url === 'string') return hostOf(url)
  }
  return undefined
}

/**
 * 实例键 = 通道类别（一类通道一个实例）。
 *
 * 非通道类别（file/other）原样返回类别名，由调用方过滤——不做"伪实例"。
 */
export function instanceKeyOf(activity: SceneActivity): string {
  return activity.category
}

/** 实例显示名（= 类别名，与 `toolCategory` 的文案同源） */
export function instanceLabelOf(activity: SceneActivity): string {
  return SCENE_CATEGORY_LABELS[activity.category]
}

/**
 * 调用 → 当前焦点描述（最新一条携带的目标；取不到空串）。
 *
 * 只报已知值：浏览器取 host，桌面取窗口名，软件取启动目标，手机取动作，
 * 终端取命令行首段。不编造"当前页面""当前窗口"这类没数据的字眼。
 */
export function instanceFocusOf(activity: SceneActivity): string {
  switch (activity.category) {
    case 'browser':
      return browserFocus(activity) ?? ''
    case 'desktop':
      return strArg(activity.args, 'window') ?? ''
    case 'software':
      return (
        strArg(activity.args, 'app') ??
        strArg(activity.args, 'file') ??
        strArg(activity.args, 'url') ??
        strArg(activity.args, 'name') ??
        ''
      )
    case 'phone':
      return strArg(activity.args, 'action') ?? ''
    case 'terminal':
      return strArg(activity.args, 'command') ?? ''
    default:
      return ''
  }
}

/**
 * 工具名 → 产物归属的通道名（产物区标注「来自 终端/浏览器/…」）。
 *
 * 只做类别映射：文件变更只知道是哪个工具写的，标到通道级是数据面允许的最细粒度。
 */
export function channelLabelOfTool(toolName: string): string {
  // 类别派生复用 utils/toolCategory 单源（deriveSceneCategory），不另立映射表
  return SCENE_CATEGORY_LABELS[deriveSceneCategory(toolName)]
}

/** 实例状态（活 = running） */
export type SceneInstanceState = 'running' | 'idle'

/** 实例状态（live → running） */
export function instanceStateOf(instance: SceneInstance): SceneInstanceState {
  return instance.live ? 'running' : 'idle'
}

export interface BuildInstancesOptions {
  /** 管道是否仍在运行（false/undefined 时不把在途状态当运行中） */
  pipelineRunning?: boolean
  /** 截图判定（注入避免本模块依赖图片抽取细节） */
  isScreenshot?: (activity: SceneActivity) => boolean
}

/**
 * 活动列表 → 实例列表（一通道一实例）。
 *
 * 排序：活的在前（按最早调用时间升序，先开跑的在前），死的在后（按最近调用
 * 时间倒序）——与 DSH `JobListAction.ordered()` 同口径。`file`/`other` 不成实例。
 *
 * @param activities 现场活动（时序）
 * @param options 管道状态与截图判定注入
 * @returns 实例列表（可能为空）
 */
export function buildSceneInstances(
  activities: readonly SceneActivity[],
  options: BuildInstancesOptions = {},
): SceneInstance[] {
  const { pipelineRunning = false, isScreenshot } = options
  const groups = new Map<string, SceneInstance>()

  for (const activity of activities) {
    if (!isInstanceCategory(activity.category)) continue
    const key = instanceKeyOf(activity)
    const existing = groups.get(key)
    const inflight = isInflight(activity) ? 1 : 0
    if (!existing) {
      groups.set(key, {
        key,
        category: activity.category,
        label: instanceLabelOf(activity),
        focus: instanceFocusOf(activity),
        live: false,
        current: activity.summary,
        ...(activity.startedAt ? { startedAt: activity.startedAt } : {}),
        latest: activity,
        activities: [activity],
        inflightCount: inflight,
        screenshots: isScreenshot?.(activity) ? [activity] : [],
      })
      continue
    }
    existing.activities.push(activity)
    existing.inflightCount += inflight
    existing.latest = activity
    existing.current = activity.summary
    const focus = instanceFocusOf(activity)
    if (focus) existing.focus = focus
    if (isScreenshot?.(activity)) existing.screenshots.push(activity)
  }

  const instances = Array.from(groups.values()).map((instance) => ({
    ...instance,
    live: instance.inflightCount > 0 && pipelineRunning,
  }))

  return instances.sort((left, right) => {
    if (left.live !== right.live) return left.live ? -1 : 1
    const leftStart = left.startedAt ?? ''
    const rightStart = right.startedAt ?? ''
    if (left.live) return leftStart.localeCompare(rightStart)
    const leftLatest = left.latest?.startedAt ?? leftStart
    const rightLatest = right.latest?.startedAt ?? rightStart
    return rightLatest.localeCompare(leftLatest)
  })
}

/** 只看活着的实例（看板用） */
export function liveInstances(instances: readonly SceneInstance[]): SceneInstance[] {
  return instances.filter((instance) => instance.live)
}

/** 实例累计活动条数（折叠条计数用） */
export function totalActivities(instances: readonly SceneInstance[]): number {
  return instances.reduce((sum, instance) => sum + instance.activities.length, 0)
}

/** 未成实例的类别（file/other）的活动条数（折叠条「另 N 条文件/其它」用） */
export function unattachedCount(activities: readonly SceneActivity[]): number {
  return activities.filter((activity) => !isInstanceCategory(activity.category)).length
}
