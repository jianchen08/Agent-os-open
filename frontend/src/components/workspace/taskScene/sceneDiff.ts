/**
 * 产物 diff 聚合器（纯函数，任务现场批 4）
 *
 * 数据源只有一个完整来源：`file_write` 的 `output.{file, old_content, new_content,
 * added, removed, size}`（`plugins/shared/builtin_tools/plugin.json`）。聚合规则
 * （方案 §3.4.1）：
 *
 * - 按 path 归并多轮写入：**首轮 old_content = 文件初始内容**（空串 = 新增），
 *   **末轮 new_content = 文件终态**，中间轮次折叠为「历史 N 轮」
 * - 状态判定：`old_content === ''` → 新增；否则 → 修改
 * - `delete_file` → 删除（**只有路径**，无 old_content —— 数据面固有局限 G6，
 *   如实降级为「仅路径」，不造假 diff）
 * - `copy_file` / `move_file` → **不产生 diff 条目**（无内容，避免假 diff）
 *
 * 性能分层（方案 §3.4.1 三级）：L0 文件清单**不算 LCS**——`+N -M` 取末轮工具上报
 * 的增删行（工具已算过，零成本）+ 轮次数；展开到 L1 才用 `computeDiff` 算**首末
 * 净差**（`computeFileNetStat`）。这样 100 个改动文件的 L0 可以瞬时渲染。
 */

import { computeDiff } from '@/utils/diffLcs'
import { describeToolAction } from '@/utils/toolCategory'
import type { SceneActivity } from './sceneActivities'

/** 文件变更状态 */
export type SceneFileChangeStatus = 'added' | 'modified' | 'deleted'

/** 单轮写入（工具上报的原始一轮） */
export interface SceneWriteRound {
  /** 该轮写前内容（null = 工具没给，按未知处理） */
  oldContent: string | null
  /** 该轮写后内容（null = 工具没给） */
  newContent: string | null
  /** 该轮工具上报的新增行（免 LCS 的 L0 数据源） */
  added?: number
  /** 该轮工具上报的删除行 */
  removed?: number
  /** 该轮写后文件字节数 */
  size?: number
  /** 该轮的调用 id（定位"谁改的"） */
  callId: string
  /** 该轮的现场摘要（工具动作一行文案） */
  summary: string
}

/** 聚合后的单个文件变更（L0 一行 / L1 单文件 diff / L2 历史轮次） */
export interface SceneFileChange {
  /** 文件路径（工具返回的写入路径优先，回落参数路径） */
  path: string
  status: SceneFileChangeStatus
  /** 历史轮次（按时间顺序） */
  rounds: SceneWriteRound[]
  /** 首轮写前内容（新增为空串；未知为 null） */
  firstOldContent: string | null
  /** 末轮写后内容（终态；未知为 null） */
  lastNewContent: string | null
  /** L0 增删行（末轮上报，非累计；缺失则 undefined） */
  added?: number
  /** L0 删除行（末轮上报，非累计；缺失则 undefined） */
  removed?: number
  /** 末轮写后字节数 */
  size?: number
  /** 首次出现的时间（ISO，排序用） */
  firstSeenAt?: string
  /** 产出工具名（"谁产出"的工具维度；delete_file 等） */
  producedBy: string
}

/** 大文件阈值（超过给「大文件」角标，点击按需分页加载） */
export const LARGE_FILE_BYTES = 5 * 1024 * 1024

/** 可内嵌预览的扩展名白名单（方案 §3.5 条件 2：图片/PDF/代码文本类） */
const INLINE_PREVIEW_EXTENSIONS: ReadonlySet<string> = new Set([
  '.png', '.jpg', '.jpeg', '.gif', '.svg', '.webp', '.bmp', '.ico',
  '.pdf',
  '.json', '.yaml', '.yml', '.md', '.txt', '.log', '.csv', '.tsv',
  '.ts', '.tsx', '.js', '.jsx', '.py', '.rs', '.go', '.java', '.sql',
  '.html', '.htm', '.css', '.sh', '.toml', '.ini', '.xml',
])

/** 取扩展名（小写含点；无扩展名返回空串） */
function extensionOf(path: string): string {
  const base = path.substring(Math.max(path.lastIndexOf('/'), path.lastIndexOf('\\')) + 1)
  const dot = base.lastIndexOf('.')
  return dot <= 0 ? '' : base.substring(dot).toLowerCase()
}

/**
 * 该文件能否在工作区内嵌预览（四条件里的「类型白名单」判定）。
 * 无扩展名/未知类型 → 不能内嵌 → 走悬浮标记（SceneBadge），不静默丢。
 */
export function canInlinePreview(path: string): boolean {
  return INLINE_PREVIEW_EXTENSIONS.has(extensionOf(path))
}

/** 文件变更状态文案（列表徽标） */
export const SCENE_CHANGE_LABELS: Record<SceneFileChangeStatus, string> = {
  added: '新增',
  modified: '修改',
  deleted: '删除',
}

/**
 * 现场活动 → 文件变更清单（按首次出现顺序，同路径归并）。
 *
 * 只消费文件域工具；其它类别活动与产物清单无关，不参与。
 */
export function buildFileChanges(activities: readonly SceneActivity[]): SceneFileChange[] {
  const byPath = new Map<string, SceneFileChange>()

  for (const activity of activities) {
    const write = writeRoundOf(activity)
    if (write) {
      const existing = byPath.get(write.path)
      if (existing) {
        existing.rounds.push(write.round)
        existing.lastNewContent = write.round.newContent
        if (write.round.added !== undefined) existing.added = write.round.added
        if (write.round.removed !== undefined) existing.removed = write.round.removed
        if (write.round.size !== undefined) existing.size = write.round.size
      } else {
        byPath.set(write.path, {
          path: write.path,
          status: write.round.oldContent === '' ? 'added' : 'modified',
          rounds: [write.round],
          firstOldContent: write.round.oldContent,
          lastNewContent: write.round.newContent,
          ...(write.round.added !== undefined ? { added: write.round.added } : {}),
          ...(write.round.removed !== undefined ? { removed: write.round.removed } : {}),
          ...(write.round.size !== undefined ? { size: write.round.size } : {}),
          ...(activity.startedAt ? { firstSeenAt: activity.startedAt } : {}),
          producedBy: activity.toolName,
        })
      }
      continue
    }

    // 删除：只有路径（结果无 old_content，G6 如实降级）
    const deletedPaths = deletedPathsOf(activity)
    for (const path of deletedPaths) {
      const existing = byPath.get(path)
      if (existing) {
        // 先写后删：状态落删除，保留写入历史（能看清"改过又删了"）
        existing.status = 'deleted'
        existing.lastNewContent = null
        existing.producedBy = activity.toolName
      } else {
        byPath.set(path, {
          path,
          status: 'deleted',
          rounds: [],
          firstOldContent: null,
          lastNewContent: null,
          ...(activity.startedAt ? { firstSeenAt: activity.startedAt } : {}),
          producedBy: activity.toolName,
        })
      }
    }
  }

  return [...byPath.values()]
}

/** file_write 一轮 → {path, round}（非 file_write 或结果缺字段返回 undefined） */
function writeRoundOf(
  activity: SceneActivity,
): { path: string; round: SceneWriteRound } | undefined {
  if (activity.toolName !== 'file_write') return undefined
  const data = record(activity.resultData) ?? record(activity.result)
  if (!data) return undefined
  const path = str(data.file) ?? str(activity.args.path) ?? str(activity.args.file_path)
  if (!path) return undefined
  const oldContent = nullableStr(data.old_content)
  const newContent = nullableStr(data.new_content)
  if (oldContent === null && newContent === null) return undefined
  return {
    path,
    round: {
      oldContent,
      newContent,
      ...(typeof data.added === 'number' ? { added: data.added } : {}),
      ...(typeof data.removed === 'number' ? { removed: data.removed } : {}),
      ...(typeof data.size === 'number' ? { size: data.size } : {}),
      callId: activity.callId,
      summary: describeToolAction(activity.toolName, activity.args),
    },
  }
}

/** delete_file 成功删除的路径（未成功的不算删除） */
function deletedPathsOf(activity: SceneActivity): string[] {
  if (activity.toolName !== 'delete_file') return []
  const data = record(activity.resultData) ?? record(activity.result)
  const results = data?.results
  if (Array.isArray(results)) {
    return results
      .map((item) => record(item))
      .filter((item): item is Record<string, unknown> => item !== undefined && item.deleted === true)
      .map((item) => str(item.path))
      .filter((path): path is string => path !== undefined)
  }
  // 兼容批量删参路径（结果缺 results 时按参数兜底，仍标删除但路径来自入参）
  const paths = activity.args.paths
  if (Array.isArray(paths)) {
    return paths.filter((p): p is string => typeof p === 'string' && p !== '')
  }
  const single = str(activity.args.path)
  return single ? [single] : []
}

/**
 * 首末净差（L1 展开时才调用——L0 清单不跑 LCS）。
 *
 * 首轮 old 与末轮 new 之间做一次 LCS，返回新增/删除行数；内容缺失时返回
 * undefined（如实缺省，不编造数字）。
 */
export function computeFileNetStat(
  change: SceneFileChange,
): { added: number; removed: number } | undefined {
  const oldText = change.firstOldContent
  const newText = change.lastNewContent
  if (oldText === null || newText === null) return undefined
  const { oldLines, newLines } = computeDiff(oldText, newText)
  let added = 0
  let removed = 0
  for (const line of newLines) if (line.type === 'added') added += 1
  for (const line of oldLines) if (line.type === 'removed') removed += 1
  return { added, removed }
}

/** 变更清单分组统计（头部汇总：新增 x / 修改 y / 删除 z） */
export function summarizeChanges(changes: readonly SceneFileChange[]): Record<SceneFileChangeStatus, number> {
  const out: Record<SceneFileChangeStatus, number> = { added: 0, modified: 0, deleted: 0 }
  for (const change of changes) out[change.status] += 1
  return out
}

function record(value: unknown): Record<string, unknown> | undefined {
  return typeof value === 'object' && value !== null ? (value as Record<string, unknown>) : undefined
}

function str(value: unknown): string | undefined {
  return typeof value === 'string' && value !== '' ? value : undefined
}

/** 可空字符串（null/缺失都按 null；空串是有效值 = 新增文件） */
function nullableStr(value: unknown): string | null {
  if (typeof value === 'string') return value
  return null
}
