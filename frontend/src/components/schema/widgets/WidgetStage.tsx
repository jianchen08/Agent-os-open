/**
 * WidgetStage — 声明 widget 组台宿主（contributes.pages 声明 {widget:
 * 'widget_stage', props: {space}} 的渲染目标，monitoring /monitoring 页、
 * trigger_setup_tool /triggers 页等复用）。
 *
 * 分组（2026-09-10）：声明带 `group` 时按组渲染 tab（组序 = 组员最小 order，
 * 未分组声明归入「概览」组）；无任何分组声明的空间保持平铺（兼容两件小页）。
 * 组内渲染委托 DeclaredWidgetLayer（声明 props 透传 + watch/refresh 联动）。
 */
import { Component, useEffect, useMemo, useState, type ReactNode } from 'react'
import { DeclaredWidgetLayer } from '@/components/schema/DeclaredWidgetLayer'
import { cn } from '@/lib/utils'
import { captureException } from '@/services/errorReporting'
import { contributionRegistry } from '@/services/schema/ContributionRegistry'
import type { WidgetDeclaration } from '@/services/schema/ContributionRegistry'

/** 未分组声明的归并组名 */
const DEFAULT_GROUP = '概览'

interface WidgetGroup {
  name: string
  members: WidgetDeclaration[]
}

interface GroupErrorBoundaryProps {
  groupName: string
  children: ReactNode
}

interface GroupErrorBoundaryState {
  error: Error | null
}

/**
 * 组级错误边界：组内任一声明 widget 渲染抛异常时降级为组内错误卡片（可重试），
 * 异常不逃逸出 widget_stage。声明页组（monitoring / triggers 等）的 widget 来自
 * 插件/agent ui_schema 声明，组件面不受前端冻结契约保护——没有这层隔离时渲染
 * 异常会炸穿 App 根边界，整页（侧栏/聊天/全部页签）被卸载（BUG-1 白屏的爆炸
 * 半径形态）。错误经 captureException 落 DEV 控制台，不吞异常。
 */
class WidgetGroupErrorBoundary extends Component<GroupErrorBoundaryProps, GroupErrorBoundaryState> {
  state: GroupErrorBoundaryState = { error: null }

  static getDerivedStateFromError(error: Error): GroupErrorBoundaryState {
    return { error }
  }

  componentDidCatch(error: Error): void {
    captureException(error, {
      component: 'WidgetStage',
      action: 'widget_group_render',
      group: this.props.groupName,
    })
  }

  render(): ReactNode {
    const { error } = this.state
    if (error) {
      return (
        <div
          data-testid="widget-stage-error"
          role="alert"
          className="border-status-error/40 bg-status-error/5 space-y-2 rounded-lg border p-4"
        >
          <p className="text-status-error text-sm font-medium">
            「{this.props.groupName}」组组件渲染出错
          </p>
          <p className="text-muted-foreground text-xs">{error.message}</p>
          <button
            type="button"
            data-testid="widget-stage-retry"
            onClick={() => this.setState({ error: null })}
            className="border-border text-muted-foreground hover:bg-muted/60 rounded border px-2 py-1 text-xs transition-colors"
          >
            重试
          </button>
        </div>
      )
    }
    return this.props.children
  }
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
        {/* key=组名：切组即重挂边界（上一组的错误态不残留到下一组） */}
        <WidgetGroupErrorBoundary key={active?.name ?? DEFAULT_GROUP} groupName={active?.name ?? DEFAULT_GROUP}>
          {active ? (
            <DeclaredWidgetLayer space={space} declarations={active.members} />
          ) : (
            <DeclaredWidgetLayer space={space} />
          )}
        </WidgetGroupErrorBoundary>
      </div>
    </div>
  )
}
