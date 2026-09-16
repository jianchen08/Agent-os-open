/**
 * 插件页面导航面板（widget: plugin_pages_hub_panel）
 *
 * 定位：所有插件推送页面的统一入口（模式体系落地设计 §4.1）——工作区的
 * 「全量目录」面板；模式只是插件的一种，hub 对模式零特例逻辑。
 *
 * 数据源：ContributionRegistry.getPages()（唯一真相源，contributes.pages 声明页 +
 * 旧贡献点归一化页），按 space 分组（工作区/设置/调试中心/聊天动作/浮窗/…）；
 * 条目 = 图标 + 标题 + 来源插件；点击经既有 openPluginPage 面板基建打开
 * （path 声明走 openWorkspacePanelByPath，widget 声明直开工作区页签）。
 *
 * 侧边栏预算配套（§4）：activity-bar 一级入口顶格零新增，长尾页面统一收进本
 * 面板——新插件装载页面自动出现，禁用插件条目同源消失（registry 刷新即收敛）。
 *
 * 搜索/折叠为面板本地视图态（不持久化）；空间分组按声明存在性渲染，空组不占位。
 */

import { useEffect, useMemo, useState } from 'react'
import { ChevronDown, ChevronUp, Search } from '@/assets/icons'
import { Input } from '@/components/ui/input'
import { cn } from '@/lib/utils'
import { contributionRegistry } from '@/services/schema/ContributionRegistry'
import type { PageDeclaration, PageSpace } from '@/services/schema/ContributionRegistry'
import { openPluginPage } from '@/services/workspacePanelOpener'

/** 空间分组标题（声明了页面的空间按此表顺序渲染；未收录空间不丢条目，按原值兜底） */
const SPACE_GROUP_LABELS: ReadonlyArray<{ space: PageSpace; label: string }> = [
  { space: 'workspace', label: '工作区' },
  { space: 'settings', label: '设置' },
  { space: 'debug_center', label: '调试中心' },
  { space: 'chat', label: '聊天动作' },
  { space: 'floating', label: '浮窗' },
  { space: 'dock', label: '状态条' },
  { space: 'fullscreen', label: '全屏' },
]

/** 条目是否命中搜索词（标题/页面 id/来源插件，大小写不敏感） */
function matchesQuery(page: PageDeclaration, query: string): boolean {
  if (!query) return true
  const haystack = [page.title, page.id, page.pluginId]
    .filter((v): v is string => typeof v === 'string')
    .join('\n')
  return haystack.toLowerCase().includes(query)
}

/** 单个可折叠空间分组 */
function PageGroup({
  space,
  label,
  pages,
  collapsed,
  onToggle,
}: {
  space: PageSpace
  label: string
  pages: PageDeclaration[]
  collapsed: boolean
  onToggle: () => void
}) {
  return (
    <section className="min-h-0">
      <button
        type="button"
        onClick={onToggle}
        aria-expanded={!collapsed}
        data-testid={`hub-group-${space}`}
        className="text-foreground hover:bg-muted/50 flex w-full items-center gap-1.5 rounded-md px-2 py-1.5 text-left text-xs font-semibold"
      >
        {collapsed ? (
          <ChevronDown className="text-muted-foreground h-3.5 w-3.5" />
        ) : (
          <ChevronUp className="text-muted-foreground h-3.5 w-3.5" />
        )}
        <span>{label}</span>
        <span className="text-muted-foreground ml-auto font-normal tabular-nums">{pages.length}</span>
      </button>
      {!collapsed && (
        <div className="flex flex-col gap-0.5 pb-1 pl-3">
          {pages.map((page) => (
            <button
              key={`${page.pluginId ?? ''}:${page.id}`}
              type="button"
              onClick={() => openPluginPage(page)}
              data-testid={`hub-item-${page.pluginId ?? ''}:${page.id}`}
              title={page.title ?? page.id}
              className="hover:bg-accent/50 flex items-center gap-2 rounded-md px-2 py-1.5 text-left text-sm transition-colors"
            >
              {page.icon && (
                <span aria-hidden="true" className="w-4 shrink-0 text-center">
                  {page.icon}
                </span>
              )}
              <span className="min-w-0 flex-1 truncate">{page.title ?? page.id}</span>
              {page.pluginId && (
                <span className="text-muted-foreground/70 shrink-0 text-[11px]">{page.pluginId}</span>
              )}
            </button>
          ))}
        </div>
      )}
    </section>
  )
}

/** 插件页面导航面板组件 */
export function PluginPagesHubPanel() {
  // 声明消费与调试中心 hub 同模式：registry 由 GrowthLoop 全局装载（登录后初始化 +
  // schema_updated 事件刷新），此处轮询条目数捕获迟到的装载结果
  const [contribTick, setContribTick] = useState(0)
  useEffect(() => {
    const id = window.setInterval(() => {
      const n = contributionRegistry.getPages().length
      setContribTick((prev) => (prev === n ? prev : n))
    }, 1500)
    return () => window.clearInterval(id)
  }, [])

  const [query, setQuery] = useState('')
  const [collapsedSpaces, setCollapsedSpaces] = useState<Record<string, boolean>>({})

  const groups = useMemo(() => {
    void contribTick
    const pages = contributionRegistry.getPages()
    const q = query.trim().toLowerCase()
    const known = new Set(SPACE_GROUP_LABELS.map((g) => g.space))
    // 已收录空间按表序；未收录空间（PageSpace 扩展）按原值兜底追加，条目不丢
    const extras = [...new Set(pages.map((p) => p.space).filter((s) => !known.has(s)))]
    return [...SPACE_GROUP_LABELS, ...extras.map((space) => ({ space, label: space }))]
      .map(({ space, label }) => ({
        space,
        label,
        pages: pages.filter((p) => p.space === space && matchesQuery(p, q)),
      }))
      .filter((g) => g.pages.length > 0)
  }, [contribTick, query])

  const toggleGroup = (space: PageSpace) =>
    setCollapsedSpaces((prev) => ({ ...prev, [space]: !prev[space] }))

  const hasAnyPage = contributionRegistry.getPages().length > 0

  return (
    <div className="flex h-full min-h-0 flex-col" data-testid="plugin-pages-hub">
      {/* 搜索过滤（面板本地视图态） */}
      <div className="relative px-3 pt-3 pb-2">
        <Search className="text-muted-foreground pointer-events-none absolute top-1/2 left-5 h-4 w-4 -translate-y-1/2" />
        <Input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="搜索页面 / 插件..."
          aria-label="搜索插件页面"
          data-testid="hub-search"
          className="pl-8"
        />
      </div>

      <div className="min-h-0 flex-1 overflow-auto px-2 pb-2">
        {groups.length === 0 ? (
          <div
            data-testid="hub-empty"
            className="text-muted-foreground flex h-full flex-col items-center justify-center gap-1 p-4 text-center text-xs"
          >
            {hasAnyPage ? (
              <span>没有匹配「{query.trim()}」的页面</span>
            ) : (
              <span>暂无插件页面声明（插件 contributes.pages 为空或未装载）</span>
            )}
          </div>
        ) : (
          <div className={cn('flex flex-col gap-1')}>
            {groups.map((group) => (
              <PageGroup
                key={group.space}
                space={group.space}
                label={group.label}
                pages={group.pages}
                collapsed={collapsedSpaces[group.space] === true}
                onToggle={() => toggleGroup(group.space)}
              />
            ))}
          </div>
        )}
      </div>
    </div>
  )
}

export default PluginPagesHubPanel
