/**
 * WorkspaceNavPage —— 工作区导航页（标签区无已开页签时的内建兜底落点）
 *
 * 内容完全由 ContributionRegistry 聚合的 workspace 空间声明驱动（无硬编码页面
 * 清单）：slot=tab 带 mode 扩展字段 → 模式面板组；slot=tab 无 mode → 工作区
 * 页签组；slot=activity-bar → 活动栏组；空组不占位，非 workspace 空间不出现。
 * 点击条目经既有 openPluginPage 打开/激活对应工作区页签。
 *
 * registry 由 GrowthLoop 全局装载（登录后初始化 + schema_updated 事件刷新），
 * 此处轮询条目数捕获迟到的装载结果（与侧栏入口/插件页面 hub 同模式）。
 */

import { useEffect, useMemo, useState } from 'react'
import { FolderTree } from '@/assets/icons'
import { contributionRegistry } from '@/services/schema/ContributionRegistry'
import type { PageDeclaration } from '@/services/schema/ContributionRegistry'
import { openPluginPage } from '@/services/workspacePanelOpener'

/** 页面是否带 mode 扩展字段（模式面板页声明的判定键） */
function modeKeyOf(page: PageDeclaration): string | undefined {
  const mode = page.mode
  return typeof mode === 'string' && mode !== '' ? mode : undefined
}

/** 单个导航卡片：大图标 + 标题（卡片网格形态，风格对齐主页欢迎卡），点击经 opener 打开对应工作区页签 */
function NavItem({ page }: { page: PageDeclaration }) {
  return (
    <button
      type="button"
      onClick={() => openPluginPage(page)}
      data-testid={`nav-item-${page.pluginId ?? ''}:${page.id}`}
      title={page.title ?? page.id}
      className="bg-card border-border hover:border-primary hover:shadow-md flex flex-col items-center justify-center gap-2 rounded-xl border px-2 py-4 text-center transition-all"
    >
      <span aria-hidden="true" className="text-2xl leading-none">
        {page.icon || '◆'}
      </span>
      <span className="text-muted-foreground w-full truncate text-xs group-hover:text-foreground">
        {page.title ?? page.id}
      </span>
    </button>
  )
}

/** 单个导航分组：标题 + 卡片网格 */
function NavGroup({ testId, label, pages }: { testId: string; label: string; pages: PageDeclaration[] }) {
  if (pages.length === 0) return null
  return (
    <section data-testid={testId} className="min-h-0">
      <div className="text-muted-foreground px-1 py-1 text-xs font-semibold">{label}</div>
      <div className="grid grid-cols-[repeat(auto-fill,minmax(112px,1fr))] gap-2">
        {pages.map((page) => (
          <NavItem key={`${page.pluginId ?? ''}:${page.id}`} page={page} />
        ))}
      </div>
    </section>
  )
}

/** 工作区导航页组件：schema 声明的全部 workspace 页面分组列表 */
export function WorkspaceNavPage() {
  const [contribTick, setContribTick] = useState(0)
  useEffect(() => {
    const id = window.setInterval(() => {
      const n = contributionRegistry.getPagesBySpace('workspace').length
      setContribTick((prev) => (prev === n ? prev : n))
    }, 1500)
    return () => window.clearInterval(id)
  }, [])

  const groups = useMemo(() => {
    void contribTick
    const pages = contributionRegistry.getPagesBySpace('workspace')
    return {
      mode: pages.filter((p) => p.slot === 'tab' && modeKeyOf(p)),
      tab: pages.filter((p) => p.slot === 'tab' && !modeKeyOf(p)),
      activityBar: pages.filter((p) => p.slot === 'activity-bar'),
    }
  }, [contribTick])

  const isEmpty = groups.mode.length === 0 && groups.tab.length === 0 && groups.activityBar.length === 0

  return (
    <div data-testid="workspace-nav-page" className="text-foreground flex h-full min-h-0 flex-col">
      {isEmpty ? (
        <div
          data-testid="nav-empty"
          className="text-muted-foreground flex h-full flex-col items-center justify-center gap-3 text-sm"
        >
          <FolderTree className="text-muted-foreground/40 h-10 w-10" />
          <span>暂无工作区页面声明（插件 contributes.pages 为空或未装载）</span>
        </div>
      ) : (
        <div className="min-h-0 flex-1 overflow-auto px-3 py-3">
          <NavGroup testId="nav-group-mode" label="模式面板" pages={groups.mode} />
          <NavGroup testId="nav-group-tab" label="工作区页签" pages={groups.tab} />
          <NavGroup testId="nav-group-activity-bar" label="活动栏" pages={groups.activityBar} />
        </div>
      )}
    </div>
  )
}
