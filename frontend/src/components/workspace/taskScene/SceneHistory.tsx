/**
 * 活动历史（默认折叠的复盘区）
 *
 * 全量工具调用流水**退出默认视图**（用户要求：现场面板不是把所有工具调用都展示
 * 出来），但复盘能力不能丢：命令 stdout、每轮写入、设备正文都要能查。形态是
 * ZCode「已结束 N」汇总行 + DSH trajectory 折叠视图的同构做法——
 *
 * - 折叠态：一行汇总（N 条调用 · M 个实例），不占主体空间
 * - 展开后：类别筛选条 → **按实例分组**（组默认收起）→ 组内复用 `SceneActivityRow`
 *
 * 分组而不是平铺：一次会话的几十条调用按「哪个通道、什么时候、最后干到哪」聚合，
 * 看的时候先看组头，需要细节再展开。
 */

import { useMemo, useState } from 'react'
import { ChevronDown, ChevronRight } from '@/assets/icons'
import { cn } from '@/lib/utils'
import { SCENE_CATEGORY_LABELS, SCENE_CATEGORY_ORDER } from '@/utils/toolCategory'
import { countByCategory, filterByCategories } from './sceneActivities'
import { SceneActivityRow } from './SceneActivityRow'
import { isInstanceCategory, isInflight } from './sceneInstances'
import { formatStartTime, sceneCategoryIcon } from './sceneRowHelpers'
import type { SceneActivity } from './sceneActivities'
import type { SceneInstance } from './sceneInstances'
import type { SceneToolCategory } from '@/utils/toolCategory'
import type { FC } from 'react'

/** 单组默认渲染上限（长任务里一个终端可能几百次调用，先给最近一段） */
const GROUP_RENDER_LIMIT = 100

/** 历史分组（一个实例一组；file/other 合成一组） */
interface HistoryGroup {
  key: string
  label: string
  category: SceneToolCategory
  /** 组内中最近一条（组头显示「最后 …」） */
  latest?: SceneActivity
  activities: SceneActivity[]
  /** 组内是否有在途调用 */
  inflight: boolean
}

/** 活动 → 分组（实例分组复用实例派生结果；file/other 单独归并） */
function buildHistoryGroups(
  activities: readonly SceneActivity[],
  instances: readonly SceneInstance[],
): HistoryGroup[] {
  const byKey = new Map<string, HistoryGroup>()

  // 先按实例铺组，保证顺序与看板一致（活的在前）
  for (const instance of instances) {
    byKey.set(instance.key, {
      key: instance.key,
      label: instance.label,
      category: instance.category,
      ...(instance.latest ? { latest: instance.latest } : {}),
      activities: [],
      inflight: false,
    })
  }

  const loose: HistoryGroup = {
    key: '__unattached__',
    label: '文件与其它动作',
    category: 'file',
    activities: [],
    inflight: false,
  }

  for (const activity of activities) {
    if (!isInstanceCategory(activity.category)) {
      loose.activities.push(activity)
      loose.latest = activity
      continue
    }
    // 实例键需与 sceneInstances 完全一致，场景内 events 已按同一规则派生；
    // 此处按 label 反查会失真，故用实例 activities 的 callId 归并
    const owner = instances.find((instance) =>
      instance.activities.some((item) => item.callId === activity.callId),
    )
    if (!owner) {
      loose.activities.push(activity)
      loose.latest = activity
      continue
    }
    const group = byKey.get(owner.key)
    if (!group) continue
    group.activities.push(activity)
    group.latest = activity
    if (isInflight(activity)) group.inflight = true
  }

  const groups: HistoryGroup[] = []
  for (const group of byKey.values()) {
    if (group.activities.length > 0) groups.push(group)
  }
  if (loose.activities.length > 0) groups.push(loose)
  return groups
}

export interface SceneHistoryProps {
  /** 全部活动（已按类别筛选前的全量） */
  activities: readonly SceneActivity[]
  /** 实例列表（分组用） */
  instances: readonly SceneInstance[]
}

/** 活动历史（默认折叠） */
export const SceneHistory: FC<SceneHistoryProps> = ({ activities, instances }) => {
  const [expanded, setExpanded] = useState(false)
  const [activeCategories, setActiveCategories] = useState<Set<SceneToolCategory>>(new Set())
  const [expandedGroups, setExpandedGroups] = useState<Set<string>>(new Set())
  const [groupLimits, setGroupLimits] = useState<Record<string, number>>({})

  const counts = useMemo(() => countByCategory(activities), [activities])
  const filtered = useMemo(
    () => filterByCategories(activities, activeCategories),
    [activities, activeCategories],
  )
  const groups = useMemo(() => buildHistoryGroups(filtered, instances), [filtered, instances])
  const instanceCount = instances.length

  const toggleCategory = (category: SceneToolCategory): void => {
    setActiveCategories((prev) => {
      const next = new Set(prev)
      if (next.has(category)) next.delete(category)
      else next.add(category)
      return next
    })
  }

  const toggleGroup = (key: string): void => {
    setExpandedGroups((prev) => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
  }

  return (
    <section
      className="border-border flex flex-shrink-0 flex-col border-t"
      aria-label="活动历史"
      data-testid="task-scene-history"
      data-expanded={expanded}
    >
      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        aria-expanded={expanded}
        className="hover:bg-[var(--hover-overlay)] flex flex-shrink-0 items-center gap-2 px-3 py-1.5 text-left"
        data-testid="task-scene-history-toggle"
      >
        {expanded ? (
          <ChevronDown className="text-muted-foreground h-icon-sm w-icon-sm" />
        ) : (
          <ChevronRight className="text-muted-foreground h-icon-sm w-icon-sm" />
        )}
        <span className="text-foreground text-xs font-medium">活动历史</span>
        <span className="text-muted-foreground/70 text-[11px]">
          {activities.length} 条调用 · {instanceCount} 个实例
        </span>
        <span className="text-muted-foreground/50 ml-auto text-[10px]">
          {expanded ? '收起' : '展开复盘'}
        </span>
      </button>

      {expanded && (
        <div className="flex min-h-0 flex-1 flex-col" data-testid="task-scene-history-body">
          {/* 类别筛选：只服务历史（看板不筛） */}
          <div
            className="border-border/60 flex flex-shrink-0 flex-wrap items-center gap-1 border-y px-3 py-1"
            role="group"
            aria-label="现场类别筛选"
            data-testid="task-scene-filters"
          >
            {SCENE_CATEGORY_ORDER.map((category) => {
              const active = activeCategories.has(category)
              const count = counts[category]
              return (
                <button
                  key={category}
                  type="button"
                  onClick={() => toggleCategory(category)}
                  aria-pressed={active}
                  className={cn(
                    'border-border flex items-center gap-1 rounded border px-1.5 py-0.5 text-[11px] transition-colors',
                    active
                      ? 'bg-primary/10 text-primary border-primary/40'
                      : count === 0
                        ? 'text-muted-foreground/50 hover:text-muted-foreground'
                        : 'text-muted-foreground hover:text-foreground',
                  )}
                  data-testid={`task-scene-filter-${category}`}
                  data-scene-count={count}
                >
                  {SCENE_CATEGORY_LABELS[category]}
                  <span className="font-mono text-[10px] opacity-70">{count}</span>
                </button>
              )
            })}
            {activeCategories.size > 0 && (
              <button
                type="button"
                onClick={() => setActiveCategories(new Set())}
                className="text-muted-foreground hover:text-foreground px-1 text-[11px] underline"
              >
                清除筛选
              </button>
            )}
          </div>

          <div className="min-h-0 flex-1 overflow-auto" data-testid="task-scene-timeline">
            {groups.length === 0 ? (
              <div className="text-muted-foreground p-3 text-center text-[11px]" data-testid="task-scene-empty">
                {activities.length === 0
                  ? '这个任务还没有产生现场记录（没有工具调用）'
                  : '当前筛选条件下没有记录'}
              </div>
            ) : (
              groups.map((group) => {
                const open = expandedGroups.has(group.key)
                const limit = groupLimits[group.key] ?? GROUP_RENDER_LIMIT
                const shown = open ? group.activities.slice(0, limit) : []
                const Icon = sceneCategoryIcon(group.category)
                return (
                  <div
                    key={group.key}
                    className="border-border/60 border-b last:border-b-0"
                    data-testid="scene-history-group"
                    data-group-key={group.key}
                    data-group-count={group.activities.length}
                  >
                    <button
                      type="button"
                      onClick={() => toggleGroup(group.key)}
                      aria-expanded={open}
                      className="hover:bg-[var(--hover-overlay)] flex w-full items-center gap-2 px-2 py-1.5 text-left"
                      data-testid="scene-history-group-toggle"
                    >
                      {open ? (
                        <ChevronDown className="text-muted-foreground h-icon-sm w-icon-sm flex-shrink-0" />
                      ) : (
                        <ChevronRight className="text-muted-foreground h-icon-sm w-icon-sm flex-shrink-0" />
                      )}
                      <Icon className="text-muted-foreground h-icon-sm w-icon-sm flex-shrink-0" />
                      <span className="text-foreground flex-shrink-0 text-xs">{group.label}</span>
                      {group.inflight && (
                        <span className="text-status-running flex-shrink-0 text-[10px]">进行中</span>
                      )}
                      <span
                        className="text-muted-foreground/70 min-w-0 flex-1 truncate text-[11px]"
                        title={group.latest?.summary}
                      >
                        {group.latest?.summary ?? ''}
                      </span>
                      <span className="text-muted-foreground/60 flex-shrink-0 text-[10px]">
                        {group.activities.length} 条
                      </span>
                      {group.latest?.startedAt && (
                        <span className="text-muted-foreground/50 flex-shrink-0 font-mono text-[10px]">
                          {formatStartTime(group.latest.startedAt)}
                        </span>
                      )}
                    </button>

                    {open && (
                      <div data-testid="scene-history-group-rows">
                        {shown.map((activity) => (
                          <SceneActivityRow key={activity.callId} activity={activity} />
                        ))}
                        {group.activities.length > shown.length && (
                          <button
                            type="button"
                            onClick={() =>
                              setGroupLimits((prev) => ({
                                ...prev,
                                [group.key]: (prev[group.key] ?? GROUP_RENDER_LIMIT) + GROUP_RENDER_LIMIT,
                              }))
                            }
                            className="text-primary hover:bg-[var(--hover-overlay)] w-full py-1 text-[11px]"
                            data-testid="scene-history-load-more"
                          >
                            加载更多（还剩 {group.activities.length - shown.length} 条）
                          </button>
                        )}
                      </div>
                    )}
                  </div>
                )
              })
            )}
          </div>
        </div>
      )}
    </section>
  )
}
