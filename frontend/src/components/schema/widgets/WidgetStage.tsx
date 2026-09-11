/**
 * WidgetStage — 声明 widget 组台宿主（contributes.pages 声明 {widget:
 * 'widget_stage', props: {space}} 的渲染目标，monitoring /monitoring 页、
 * trigger_setup_tool /triggers 页等复用）。
 *
 * 分组（2026-09-10）：声明带 `group` 时按组渲染 tab（组序 = 组员最小 order，
 * 未分组声明归入「概览」组）；无任何分组声明的空间保持平铺（兼容两件小页）。
 * 组内渲染委托 DeclaredWidgetLayer（声明 props 透传 + watch/refresh 联动）。
 */
import { useEffect, useMemo, useState } from 'react'
import { DeclaredWidgetLayer } from '@/components/schema/DeclaredWidgetLayer'
import { cn } from '@/lib/utils'
import { contributionRegistry } from '@/services/schema/ContributionRegistry'
import type { WidgetDeclaration } from '@/services/schema/ContributionRegistry'

/** 未分组声明的归并组名 */
const DEFAULT_GROUP = '概览'

interface WidgetGroup {
  name: string
  members: WidgetDeclaration[]
}

export function WidgetStage(props: Record<string, unknown>) {
  const space = (props.space as string) ?? 'widget-stage'

  // registry 由 GrowthLoop 全局装载（登录后初始化 + schema_updated 刷新），
  // 轮询条目数捕获迟到的装载结果（与侧栏/调试中心同模式）
  const [contribTick, setContribTick] = useState(0)
  useEffect(() => {
    const id = window.setInterval(() => {
      const n = contributionRegistry.getAllWidgets().filter((d) => !d.space || d.space === space).length
      setContribTick((prev) => (prev === n ? prev : n))
    }, 1500)
    return () => window.clearInterval(id)
  }, [space])

  const groups = useMemo<WidgetGroup[]>(() => {
    void contribTick
    const inSpace = contributionRegistry
      .getAllWidgets()
      .filter((d) => !d.space || d.space === space)
    if (inSpace.length === 0) return []
    const byGroup = new Map<string, WidgetDeclaration[]>()
    for (const d of inSpace) {
      const name = d.group ?? DEFAULT_GROUP
      const list = byGroup.get(name) ?? []
      list.push(d)
      byGroup.set(name, list)
    }
    // 组序 = 组员最小 order（缺省 1000）；组内按 order 升序
    return [...byGroup.entries()]
      .map(([name, members]) => ({
        name,
        minOrder: Math.min(...members.map((m) => m.order ?? 1000)),
        members: [...members].sort((a, b) => (a.order ?? 1000) - (b.order ?? 1000)),
      }))
      .sort((a, b) => a.minOrder - b.minOrder)
      .map(({ name, members }) => ({ name, members }))
  }, [contribTick, space])

  const grouped = groups.length > 1 || (groups.length === 1 && groups[0].name !== DEFAULT_GROUP)
  const [activeGroup, setActiveGroup] = useState<string | null>(null)
  const active = grouped ? (groups.find((g) => g.name === activeGroup) ?? groups[0]) : null

  return (
    <div className="flex h-full min-h-0 flex-col" data-testid="widget-stage">
      {grouped && (
        <div className="flex flex-wrap gap-1 border-b px-2 py-1.5">
          {groups.map((g) => (
            <button
              key={g.name}
              type="button"
              onClick={() => setActiveGroup(g.name)}
              data-testid={`widget-stage-tab-${g.name}`}
              className={cn(
                'rounded-md px-2.5 py-1 text-xs transition-colors',
                active?.name === g.name
                  ? 'bg-primary text-primary-foreground'
                  : 'text-muted-foreground hover:bg-accent/50',
              )}
            >
              {g.name}
            </button>
          ))}
        </div>
      )}
      <div className="min-h-0 flex-1 space-y-4 overflow-auto p-3">
        {active ? (
          <DeclaredWidgetLayer space={space} declarations={active.members} />
        ) : (
          <DeclaredWidgetLayer space={space} />
        )}
      </div>
    </div>
  )
}
