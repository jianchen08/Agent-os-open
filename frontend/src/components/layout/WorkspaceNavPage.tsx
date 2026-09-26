/**
 * WorkspaceNavPage —— 统一导航页（新标签页按钮与空标签态同一内容源）
 *
 * 内容完全由 ContributionRegistry 聚合的页面声明驱动（无硬编码页面清单）：
 * - 顶部搜索框：按标题/页面 id/来源插件本地过滤（视图态，不持久化）
 * - 主体：workspace 空间大图标卡片网格——slot=tab 带 mode 扩展字段 → 模式面板组
 *   （置顶）；slot=tab 无 mode → 工作区页签组；slot=activity-bar → 活动栏组；
 *   调试中心/设置为一级直属分组（大卡片，不折叠）
 * - 「更多」折叠区：其余非 workspace 空间按 MORE_SPACE_GROUP_LABELS 顺序渲染
 *   小卡片分组（同卡片语言缩小号），默认折叠；搜索有值自动展开并跨组过滤，
 *   组内无匹配不占位
 * 点击条目经既有 openPluginPage 打开对应落点（path 直达 / widget 开工作区页签）。
 *
 * registry 由 GrowthLoop 全局装载（登录后初始化 + schema_updated 事件刷新），
 * 此处轮询条目数捕获迟到的装载结果（与侧栏入口同模式）。
 */

import { useEffect, useMemo, useState } from 'react'
import { ChevronDown, ChevronUp, FolderTree, Search } from '@/assets/icons'
import { Input } from '@/components/ui/input'
import { contributionRegistry } from '@/services/schema/ContributionRegistry'
import { openPluginPage } from '@/services/workspacePanelOpener'
import type { PageDeclaration, PageSpace } from '@/services/schema/ContributionRegistry'

/** 一级直属空间分组（大卡片、不折叠，按表序渲染在活动栏组之后） */
const PRIMARY_SPACE_GROUPS: ReadonlyArray<{ space: PageSpace; label: string }> = [
  { space: 'debug_center', label: '调试中心' },
  { space: 'settings', label: '设置' },
]

/** 「更多」区空间分组标题（其余非 workspace 空间按此表顺序渲染；未收录空间按原值兜底，条目不丢） */
const MORE_SPACE_GROUP_LABELS: ReadonlyArray<{ space: PageSpace; label: string }> = [
  { space: 'chat', label: '聊天动作' },
  { space: 'floating', label: '浮窗' },
  { space: 'dock', label: '状态条' },
  { space: 'fullscreen', label: '全屏' },
]

/** 页面是否带 mode 扩展字段（模式面板页声明的判定键） */
function modeKeyOf(page: PageDeclaration): string | undefined {
  const mode = page.mode
  return typeof mode === 'string' && mode !== '' ? mode : undefined
}

/** 条目是否命中搜索词（标题/页面 id/来源插件，大小写不敏感） */
function matchesQuery(page: PageDeclaration, query: string): boolean {
  if (!query) return true
  const haystack = [page.title, page.id, page.pluginId]
    .filter((v): v is string => typeof v === 'string')
    .join('\n')
  return haystack.toLowerCase().includes(query)
}

/** 导航卡片公共骨架：点击经 opener 打开对应工作区页签，尺寸差异由参数注入 */
function NavCard({
  page,
  className,
  iconClassName,
  titleClassName,
}: {
  page: PageDeclaration
  className: string
  iconClassName: string
  titleClassName: string
}) {
  return (
    <button
      type="button"
      onClick={() => openPluginPage(page)}
      data-testid={`nav-item-${page.pluginId ?? ''}:${page.id}`}
      title={page.title ?? page.id}
      className={className}
    >
      <span aria-hidden="true" className={iconClassName}>
        {page.icon || '◆'}
      </span>
      <span className={titleClassName}>{page.title ?? page.id}</span>
    </button>
  )
}

/** 单个导航卡片：大图标 + 标题（卡片网格形态，风格对齐主页欢迎卡） */
function NavItem({ page }: { page: PageDeclaration }) {
  return (
    <NavCard
      page={page}
      className="bg-card border-border hover:border-primary hover:shadow-md flex flex-col items-center justify-center gap-2 rounded-xl border px-2 py-4 text-center transition-all"
      iconClassName="text-2xl leading-none"
      titleClassName="text-muted-foreground w-full truncate text-xs group-hover:text-foreground"
    />
  )
}

/** 「更多」区小卡片：同卡片语言缩小号 */
function NavItemSmall({ page }: { page: PageDeclaration }) {
  return (
    <NavCard
      page={page}
      className="bg-card border-border hover:border-primary hover:shadow-md flex flex-col items-center justify-center gap-1 rounded-lg border px-1 py-2 text-center transition-all"
      iconClassName="text-lg leading-none"
      titleClassName="text-muted-foreground w-full truncate text-[11px]"
    />
  )
}

/** 单个导航分组：标题 + 卡片网格（small=true 为「更多」区缩小号） */
function NavGroup({
  testId,
  label,
  pages,
  small = false,
}: {
  testId: string
  label: string
  pages: PageDeclaration[]
  small?: boolean
}) {
  if (pages.length === 0) return null
  return (
    <section data-testid={testId} className="min-h-0">
      <div className="text-muted-foreground px-1 py-1 text-xs font-semibold">{label}</div>
      <div
        className={
          small
            ? 'grid grid-cols-[repeat(auto-fill,minmax(88px,1fr))] gap-1.5'
            : 'grid grid-cols-[repeat(auto-fill,minmax(112px,1fr))] gap-2'
        }
      >
        {pages.map((page) =>
          small ? (
            <NavItemSmall key={`${page.pluginId ?? ''}:${page.id}`} page={page} />
          ) : (
            <NavItem key={`${page.pluginId ?? ''}:${page.id}`} page={page} />
          ),
        )}
      </div>
    </section>
  )
}

/** 工作区导航页组件：schema 声明的全部页面分组导航（workspace 主体验 + 非 workspace 折叠区） */
export function WorkspaceNavPage() {
  const [contribTick, setContribTick] = useState(0)
  useEffect(() => {
    const id = window.setInterval(() => {
      const n = contributionRegistry.getPages().length
      setContribTick((prev) => (prev === n ? prev : n))
    }, 1500)
    return () => window.clearInterval(id)
  }, [])

  const [query, setQuery] = useState('')
  const [moreExpanded, setMoreExpanded] = useState(false)

  const { mode, tab, activityBar, primaryGroups, moreGroups, moreTotal, hasAnyPage } = useMemo(() => {
    void contribTick
    const pages = contributionRegistry.getPages()
    const q = query.trim().toLowerCase()
    const hit = (p: PageDeclaration) => matchesQuery(p, q)
    const workspace = pages.filter((p) => p.space === 'workspace' && hit(p))
    // 一级直属分组（调试中心/设置）+ 其余非 workspace 空间「更多」分组；
    // 未收录空间（PageSpace 运行时扩展）按原值兜底追加进「更多」，条目不丢
    const primarySpaces = new Set(PRIMARY_SPACE_GROUPS.map((g) => g.space))
    const knownSpaces = new Set([...primarySpaces, ...MORE_SPACE_GROUP_LABELS.map((g) => g.space)])
    const extras = [
      ...new Set(pages.map((p) => p.space).filter((s) => !knownSpaces.has(s) && s !== 'workspace')),
    ]
    return {
      mode: workspace.filter((p) => p.slot === 'tab' && modeKeyOf(p)),
      tab: workspace.filter((p) => p.slot === 'tab' && !modeKeyOf(p)),
      activityBar: workspace.filter((p) => p.slot === 'activity-bar'),
      primaryGroups: PRIMARY_SPACE_GROUPS.map(({ space, label }) => ({
        space,
        label,
        pages: pages.filter((p) => p.space === space && hit(p)),
      })).filter((g) => g.pages.length > 0),
      moreGroups: [...MORE_SPACE_GROUP_LABELS, ...extras.map((space) => ({ space, label: String(space) }))]
        .map(({ space, label }) => ({
          space,
          label,
          pages: pages.filter((p) => p.space === space && hit(p)),
        }))
        .filter((g) => g.pages.length > 0),
      moreTotal: pages.filter((p) => p.space !== 'workspace' && !primarySpaces.has(p.space)).length,
      hasAnyPage: pages.length > 0,
    }
  }, [contribTick, query])

  const searching = query.trim() !== ''
  const expanded = searching || moreExpanded
  const searchMissed =
    searching &&
    mode.length === 0 &&
    tab.length === 0 &&
    activityBar.length === 0 &&
    primaryGroups.length === 0 &&
    moreGroups.length === 0

  return (
    <div data-testid="workspace-nav-page" className="text-foreground flex h-full min-h-0 flex-col">
      {/* 搜索过滤（本地视图态，跨空间过滤全部页面） */}
      <div className="relative px-3 pt-3 pb-2">
        <Search className="text-muted-foreground pointer-events-none absolute top-1/2 left-5 h-4 w-4 -translate-y-1/2" />
        <Input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="搜索页面 / 插件..."
          aria-label="搜索页面"
          data-testid="nav-search"
          className="pl-8"
        />
      </div>
      {!hasAnyPage ? (
        <div
          data-testid="nav-empty"
          className="text-muted-foreground flex h-full flex-1 flex-col items-center justify-center gap-3 text-sm"
        >
          <FolderTree className="text-muted-foreground/40 h-10 w-10" />
          <span>暂无页面声明（插件 contributes.pages 为空或未装载）</span>
        </div>
      ) : searchMissed ? (
        <div
          data-testid="nav-no-match"
          className="text-muted-foreground flex h-full flex-1 flex-col items-center justify-center gap-1 p-4 text-center text-xs"
        >
          <span>没有匹配「{query.trim()}」的页面</span>
        </div>
      ) : (
        <div className="min-h-0 flex-1 overflow-auto px-3 pb-3">
          <NavGroup testId="nav-group-mode" label="模式面板" pages={mode} />
          <NavGroup testId="nav-group-tab" label="工作区页签" pages={tab} />
          <NavGroup testId="nav-group-activity-bar" label="活动栏" pages={activityBar} />
          {primaryGroups.map((g) => (
            <NavGroup key={g.space} testId={`nav-group-${g.space}`} label={g.label} pages={g.pages} />
          ))}
          {moreGroups.length > 0 && (
            <section data-testid="nav-more" className="mt-1">
              <button
                type="button"
                onClick={() => setMoreExpanded((v) => !v)}
                aria-expanded={expanded}
                data-testid="nav-more-toggle"
                className="text-foreground hover:bg-muted/50 flex w-full items-center gap-1.5 rounded-md px-2 py-1.5 text-left text-xs font-semibold"
              >
                {expanded ? (
                  <ChevronUp className="text-muted-foreground h-3.5 w-3.5" />
                ) : (
                  <ChevronDown className="text-muted-foreground h-3.5 w-3.5" />
                )}
                <span>更多</span>
                <span className="text-muted-foreground ml-auto font-normal tabular-nums">{moreTotal}</span>
              </button>
              {expanded && (
                <div className="flex flex-col gap-2 pt-1">
                  {moreGroups.map((g) => (
                    <NavGroup key={g.space} testId={`nav-group-${g.space}`} label={g.label} pages={g.pages} small />
                  ))}
                </div>
              )}
            </section>
          )}
        </div>
      )}
    </div>
  )
}
