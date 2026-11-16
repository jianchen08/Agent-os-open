/**
 * 任务管理面板筛选工具栏 + 筛选纯函数（自 PipelineManagerWidget 抽出，
 * 巨型文件冻结门禁：只许缩小不许增长）。
 *
 * 筛选模型：状态/类型为平铺可叠加标签组（无「全部」项——该组标签全部未选中
 * 即全量；再点已选中标签取消）。状态组「运行中」为独占档：与其余状态互斥，
 * 点任一方自动取消另一方（用户裁定 2026-10-17），非运行态之间叠加取并集；
 * 会话筛选由活跃会话驱动（聊天侧切换即跟随），激活时一枚 chip、点 × 取消
 ——行级会话钉住入口已下线（挤占行内操作区并把其后操作按钮挤出屏幕外）。
 */
import { Search, X as XIcon } from '@/assets/icons'
import { sanitizeSessionTitle } from '@/utils/sessionTitle'
import type { Session } from '@/types/models'
import type { PipelineViewEntry } from '@/types/pipeline'

/** 状态筛选值 → 展示标签（键对齐管道状态词表；标签组与空态提示共用；
 *  空集 = 全部） */
export const STATUS_FILTER_LABELS: Record<string, string> = {
  running: '运行中',
  suspended: '已暂停',
  completed: '已完成',
  failed: '失败',
}

/** 类型筛选值 → 展示标签（'all' = 全部，不筛选） */
export const KIND_FILTER_LABELS: Record<'task' | 'session', string> = {
  task: '任务',
  session: '会话',
}

/** 四维筛选（各维度 AND 叠加）——widget 的 filteredPipelineEntries 谓词单源；
 *  状态/类型两维为标签集合（空集 = 该维度不筛选） */
export interface PipelineEntryFilters {
  kindFilters: ReadonlySet<'task' | 'session'>
  statusFilters: ReadonlySet<string>
  sessionFilter: string | null
  keyword: string
}

/** 按类型 + 状态 + 会话 + 关键词筛选管道条目（各维度 AND 叠加） */
export function matchesEntryFilters(entry: PipelineViewEntry, filters: PipelineEntryFilters): boolean {
  const { kindFilters, statusFilters, sessionFilter, keyword } = filters
  // 类型：空集 = 全部；选中后项目登记行（kind=project）一并滤出
  if (kindFilters.size > 0) {
    if (entry.kind !== 'task' && entry.kind !== 'session') return false
    if (!kindFilters.has(entry.kind)) return false
  }
  // 状态：键对齐条目状态词表，空集 = 全部
  if (statusFilters.size > 0 && !statusFilters.has(entry.status ?? '')) return false
  // 会话筛选：主管道靠 threadId 命中，血缘子任务靠 originSessionId 命中
  // （孤儿两侧都空/不命中被滤掉）
  if (sessionFilter && entry.threadId !== sessionFilter && entry.originSessionId !== sessionFilter) {
    return false
  }
  // 关键词：小写 contains，五字段任一命中即过
  if (keyword) {
    const kw = keyword.toLowerCase()
    const hit =
      entry.name.toLowerCase().includes(kw)
      || (entry.agentName ?? '').toLowerCase().includes(kw)
      || (entry.sessionTitle ?? '').toLowerCase().includes(kw)
      || (entry.taskId ?? '').toLowerCase().includes(kw)
      || (entry.pipelineId ?? '').toLowerCase().includes(kw)
    if (!hit) return false
  }
  return true
}

/** 状态标签切换（空集 = 全部）：「运行中」为独占档——选中它清空其余状态，
 *  选中其余状态清掉「运行中」（互斥对点选自动反选，用户裁定 2026-10-17）；
 *  非运行态之间叠加取并集；再点已选中标签取消 */
export function toggleStatusFilterSet(prev: ReadonlySet<string>, value: string): Set<string> {
  const next = new Set(prev)
  if (!next.delete(value)) {
    if (value === 'running') next.clear()
    else next.delete('running')
    next.add(value)
  }
  return next
}

/** 类型标签切换（空集 = 全部；任务/会话可叠加并集，再点取消） */
export function toggleKindFilterSet(
  prev: ReadonlySet<'task' | 'session'>,
  value: 'task' | 'session',
): Set<'task' | 'session'> {
  const next = new Set(prev)
  if (next.has(value)) next.delete(value)
  else next.add(value)
  return next
}

/** 条目归属会话解析（与 handleEntryClick 同口径）：threadId 命中会话列表为
 *  第一真值，否则血缘 originSessionId 兜底；都不命中 = 无归属（null） */
export function resolveEntrySessionId(entry: PipelineViewEntry, sessions: Session[]): string | null {
  if (entry.threadId && sessions.some((s) => s.id === entry.threadId)) return entry.threadId
  if (
    entry.originSessionId
    && sessions.some((s) => s.id === entry.originSessionId)
  ) {
    return entry.originSessionId
  }
  return null
}

/** 会话 chip 标签：会话列表里的标题（展示净化），查不到回退 id 前 8 位 */
export function sessionFilterLabelOf(sessionFilter: string | null, sessions: Session[]): string {
  if (!sessionFilter) return ''
  const session = sessions.find((s) => s.id === sessionFilter)
  return session ? sanitizeSessionTitle(session.title) : `会话 ${sessionFilter.slice(0, 8)}`
}

/** 平铺筛选标签（单选组内一枚；标签形态=胶囊+描边，选中态填充高亮 +
 *  aria-pressed，name 带组前缀唯一定位） */
function FilterOptionTag({
  active,
  onClick,
  label,
  name,
}: {
  active: boolean
  onClick: () => void
  label: string
  name: string
}) {
  return (
    <button
      onClick={onClick}
      aria-label={name}
      aria-pressed={active}
      title={name}
      className={`rounded-full border px-2 py-0 text-[11px] transition-colors ${
        active
          ? 'border-primary/40 bg-primary/15 text-primary font-medium'
          : 'border-border text-foreground/90 hover:border-primary/40 hover:text-foreground'
      }`}
    >
      {label}
    </button>
  )
}

interface FilterBarProps {
  keyword: string
  setKeyword: (kw: string) => void
  statusFilters: ReadonlySet<string>
  setStatusFilters: (next: Set<string>) => void
  kindFilters: ReadonlySet<'task' | 'session'>
  setKindFilters: (next: Set<'task' | 'session'>) => void
  sessionFilter: string | null
  /** × 掉会话 chip = 清会话筛选（下一个会话选择动作恢复跟随/钉住） */
  clearSessionFilter: () => void
  sessionFilterLabel: string
}

/** 工具栏：关键词搜索 + 状态/类型平铺标签组（无「全部」项，空集 = 全部）+ 会话 chip */
export function PipelineManagerFilterBar({
  keyword,
  setKeyword,
  statusFilters,
  setStatusFilters,
  kindFilters,
  setKindFilters,
  sessionFilter,
  clearSessionFilter,
  sessionFilterLabel,
}: FilterBarProps) {
  return (
    <div className="border-b px-3 py-2">
      <div className="flex flex-wrap items-center gap-1">
        {/* 关键词搜索（带清空 ×） */}
        <div className="border-border focus-within:border-primary/50 flex h-6 items-center gap-1 rounded-md border px-1.5">
          <Search className="text-muted-foreground h-3 w-3 shrink-0" />
          <input
            value={keyword}
            onChange={(e) => setKeyword(e.target.value)}
            placeholder="搜索…"
            aria-label="搜索管道"
            className="placeholder:text-muted-foreground/60 w-24 bg-transparent text-[11px] outline-none"
          />
          {keyword && (
            <button
              onClick={() => setKeyword('')}
              aria-label="清空搜索"
              title="清空搜索"
              className="text-muted-foreground hover:text-foreground flex shrink-0"
            >
              <XIcon className="h-3 w-3" />
            </button>
          )}
        </div>
        <span className="bg-border mx-1 h-3 w-px" />
        {/* 状态筛选标签（可叠加组；空集 = 全部，「运行中」与其余状态互斥自动
            反选——见 toggleStatusFilterSet。组前缀标签用 foreground 系
            （muted-foreground 对 background 在部分主题低对比，颜色对门禁违例）） */}
        <div className="flex items-center gap-0.5">
          <span className="text-foreground/90 mr-0.5 text-[11px]">状态</span>
          {Object.entries(STATUS_FILTER_LABELS).map(([value, label]) => (
            <FilterOptionTag
              key={value}
              label={label}
              name={`状态：${label}`}
              active={statusFilters.has(value)}
              onClick={() => setStatusFilters(toggleStatusFilterSet(statusFilters, value))}
            />
          ))}
        </div>
        <span className="bg-border mx-1 h-3 w-px" />
        {/* 类型筛选标签（可叠加组；空集 = 全部） */}
        <div className="flex items-center gap-0.5">
          <span className="text-foreground/90 mr-0.5 text-[11px]">类型</span>
          {(Object.keys(KIND_FILTER_LABELS) as Array<'task' | 'session'>).map((value) => (
            <FilterOptionTag
              key={value}
              label={KIND_FILTER_LABELS[value]}
              name={`类型：${KIND_FILTER_LABELS[value]}`}
              active={kindFilters.has(value)}
              onClick={() => setKindFilters(toggleKindFilterSet(kindFilters, value))}
            />
          ))}
        </div>
        {/* 会话筛选 chip（值 = 用户选择的会话：活跃会话跟随 / 行级钉住） */}
        {sessionFilter && (
          <span className="bg-primary/15 text-primary flex items-center gap-0.5 rounded-md py-0.5 pr-0.5 pl-1.5 text-[11px] font-medium">
            会话: {sessionFilterLabel}
            <button
              onClick={clearSessionFilter}
              aria-label="移除会话筛选"
              title="移除会话筛选"
              className="hover:text-foreground/70 flex shrink-0 rounded"
            >
              <XIcon className="h-3 w-3" />
            </button>
          </span>
        )}
      </div>
    </div>
  )
}
