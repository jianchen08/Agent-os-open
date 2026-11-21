/**
 * 任务管理面板筛选工具栏 + 筛选纯函数（自 PipelineManagerWidget 抽出，
 * 巨型文件冻结门禁：只许缩小不许增长）。
 *
 * 筛选模型：状态为平铺可叠加标签组（无「全部」项——标签全部未选中即全量；
 * 再点已选中标签取消；任意状态间叠加取并集，「运行中」不设独占档——用户裁定
 * 2026-11-13）；类型（任务/会话）维已按用户要求整体退役（2026-11-20）；会话筛选由活跃会话驱动（聊天侧切换即跟随），
 * 激活时一枚 chip、点 × 取消
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

/** 三维筛选（各维度 AND 叠加）——widget 的 filteredPipelineEntries 谓词单源；
 *  状态维为标签集合（空集 = 不筛选） */
export interface PipelineEntryFilters {
  statusFilters: ReadonlySet<string>
  sessionFilter: string | null
  keyword: string
  /** 下辖运行中的条目 key 集（状态筛选「运行中」的链活跃聚合输入；
   *  缺席 = 不聚合，只看条目自身状态） */
  runningChainKeys?: ReadonlySet<string>
}

/** 按状态 + 会话 + 关键词筛选管道条目（各维度 AND 叠加） */
export function matchesEntryFilters(entry: PipelineViewEntry, filters: PipelineEntryFilters): boolean {
  const { statusFilters, sessionFilter, keyword, runningChainKeys } = filters
  // 关键词：小写 contains。ID 命中和文字命中的放行口径不同（见下），
  // 先分类再判其余维度
  const kw = keyword.trim().toLowerCase()
  let idHit = false
  if (kw) {
    idHit = entryIdHit(entry, kw)
    if (!idHit && !entryTextFieldHit(entry, kw)) return false
  }
  // ID 直接命中 = 定位动作（用户拿着管道/任务/运行 ID 找那一条），不受浏览
  // 维度（状态/类型/会话）阻挡——否则终态管道在缺省「运行中」档下搜 ID 恒空，
  // 用户以为「不能按 ID 搜」（用户实测 2026-11-19）。文字命中照旧与其余维度 AND
  if (idHit) return true
  // 状态：键对齐条目状态词表，空集 = 全部。链活跃聚合：勾了「运行中」时，
  // 下辖有运行中子孙的祖先条目一并保留——否则「A 派 B、B 派 C，C 在跑」这条
  // 活链在只看运行中时被整条滤掉（用户实测 2026-11-19）
  if (statusFilters.size > 0 && !statusFilters.has(entry.status ?? '')) {
    const chainRunning = statusFilters.has('running') && runningChainKeys?.has(entry.key) === true
    if (!chainRunning) return false
  }
  // 会话筛选：主管道靠 threadId 命中，血缘子任务靠 originSessionId 命中
  // （孤儿两侧都空/不命中被滤掉）
  if (sessionFilter && entry.threadId !== sessionFilter && entry.originSessionId !== sessionFilter) {
    return false
  }
  return true
}

/** 条目 ID 命中（管道/任务/运行 ID 三面；大小写不敏感 contains，支持部分
 *  匹配——输入 `4f78` 命中 `4f780ce1f86f`）。三面同源：任务即管道，运行 ID
 *  是详情面板「运行 ID」行可复制的另一个标识（复制来搜必须也能命中）。 */
export function entryIdHit(entry: PipelineViewEntry, lowerKeyword: string): boolean {
  return (
    (entry.pipelineId ?? '').toLowerCase().includes(lowerKeyword)
    || (entry.taskId ?? '').toLowerCase().includes(lowerKeyword)
    || (entry.runId ?? '').toLowerCase().includes(lowerKeyword)
  )
}

/** 条目文字面命中（名称/agent/会话标题）；与 ID 面分离，供「文字命中仍受
 *  浏览维度约束」的放行判定单源复用 */
export function entryTextFieldHit(entry: PipelineViewEntry, lowerKeyword: string): boolean {
  return (
    (entry.name ?? '').toLowerCase().includes(lowerKeyword)
    || (entry.agentName ?? '').toLowerCase().includes(lowerKeyword)
    || (entry.sessionTitle ?? '').toLowerCase().includes(lowerKeyword)
  )
}

/** 状态标签切换（空集 = 全部）：平铺可叠加组，任意状态间取并集（「运行中」
 *  与其余状态可同选——用户裁定 2026-11-13）；再点已选中标签取消 */
export function toggleStatusFilterSet(prev: ReadonlySet<string>, value: string): Set<string> {
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
            placeholder="名称 / 管道 ID"
            aria-label="搜索管道"
            title="按名称、执行者、会话标题或管道 ID（支持前缀，如 4f78）搜索——ID 命中不受状态/类型/会话筛选阻挡"
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
        {/* 状态筛选标签（可叠加组；空集 = 全部，任意状态间取并集——见
            toggleStatusFilterSet。组前缀标签用 foreground 系
            （muted-foreground 对 background 在部分主题低对比，颜色对门禁违例）。
            组内 flex-wrap（P0-4，视觉审计 shots/01）：组是外层 flex 的单个 item，
            不可拆时 chip 多/长的组合在窄视口整组溢出——允许组内两行拆分 */}
        <div className="flex min-w-0 max-w-full flex-wrap items-center gap-x-0.5 gap-y-1">
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
        {/* 会话筛选 chip（值 = 用户选择的会话：活跃会话跟随 / 行级钉住）；
            max-w-full + 标题 truncate（P0-4 同刀）：长会话标题不撑破行宽 */}
        {sessionFilter && (
          <span className="bg-primary/15 text-primary flex min-w-0 max-w-full items-center gap-0.5 rounded-md py-0.5 pr-0.5 pl-1.5 text-[11px] font-medium">
            <span className="min-w-0 truncate">会话: {sessionFilterLabel}</span>
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
