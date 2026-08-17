/**
 * DeclaredWidgetLayer — ui_schema 声明 widget 的渲染层（架构 §5.3 的生产消费者）
 *
 * 架构意图：contributionRegistry.getAllWidgets()（agents/pipelines 的 ui_schema.widgets）
 * 经 resolveDeclaredWidgets 桥梁解析为组件，按 space 渲染。这是「声明 → 实现」链路
 * 在渲染侧的落点，闭合架构断裂点 ⑧（getAllWidgets 零消费者）。
 *
 * 两种渲染语义：
 * 1. 附加式（无 slotId，原语义）：渲染该空间的全部声明 widget，插件往工具栏/空间追加内容。
 * 2. 槽位式（slotId + fallback）：渲染 id === slotId 的声明（覆盖语义）；无覆盖声明时
 *    渲染前端默认组件（fallback）。「前端提供默认件 + 插件声明可覆盖」——同一槽位，
 *    插件换 type（如 webview）即完全接管。多插件声明同槽位时按 order 小者胜。
 *
 * 当前作为 PageRenderer 的附加层挂载；M1 起将随页面统一迁移细化各空间的放置。
 *
 * 关联：docs/working/重要设计/前端能力统一架构.md §5.3 / §5.4
 *       docs/working/design/frontend-design-unification-execution-plan.md §三 M0.1
 */

import { useEffect, useMemo } from 'react'
import { cn } from '@/lib/utils'
import { contributionRegistry } from '@/services/schema/ContributionRegistry'
import { resolveDeclaredWidgets } from '@/services/schema/widgetChain'
import type { WidgetDeclaration } from '@/services/schema/ContributionRegistry'
import type { WidgetComponent } from '@/services/schema/WidgetRegistry'
import type { ReactNode } from 'react'

export interface DeclaredWidgetLayerProps {
  /** 仅渲染该空间的声明 widget（缺省渲染全部） */
  space?: string
  /** 显式传入声明（缺省读 contributionRegistry.getAllWidgets，即生产消费链路） */
  declarations?: WidgetDeclaration[]
  /**
   * 槽位 id：本层作为槽位渲染——只渲染 id 与之相等的声明（覆盖语义），
   * 无覆盖声明时渲染 fallback 默认组件。不传则为附加式（渲染空间全部声明）
   */
  slotId?: string
  /** 排除已被槽位层消费的声明 id（附加式层防重复渲染用） */
  excludeIds?: string[]
  /** 槽位默认组件：无覆盖声明时渲染（前端默认件，插件可覆盖） */
  fallback?: WidgetComponent
  /** 默认组件的 props（宿主注入场景：默认件需要宿主状态/回调） */
  fallbackProps?: Record<string, unknown>
  className?: string
}

/**
 * 渲染单个已解析 widget（透传声明 props）
 */
function ResolvedItem({
  declaration,
  Component,
}: {
  declaration: WidgetDeclaration
  Component: WidgetComponent
}) {
  return (
    <div
      data-testid={`declared-widget-${declaration.id}`}
      data-widget-type={declaration.type}
      className="min-h-0"
    >
      <Component {...(declaration.props ?? {})} />
    </div>
  )
}

/**
 * 按 space 过滤声明（声明未指定 space 时不被任何 space 过滤排除——
 * space 缺省时渲染全部）
 */
function filterBySpace(declarations: WidgetDeclaration[], space?: string): WidgetDeclaration[] {
  if (!space) return declarations
  return declarations.filter((d) => !d.space || d.space === space)
}

/** 槽位内排序去重：order 小者胜（缺省 1000），同 id 仅保留最优声明 */
function slotWinner(declarations: WidgetDeclaration[]): WidgetDeclaration[] {
  const sorted = [...declarations].sort((a, b) => (a.order ?? 1000) - (b.order ?? 1000))
  const seen = new Set<string>()
  const out: WidgetDeclaration[] = []
  for (const d of sorted) {
    if (seen.has(d.id)) continue
    seen.add(d.id)
    out.push(d)
  }
  return out
}

export function DeclaredWidgetLayer({
  space,
  declarations,
  slotId,
  excludeIds,
  fallback,
  fallbackProps,
  className,
}: DeclaredWidgetLayerProps): ReactNode {
  // 读取移入 useMemo：getAllWidgets() 每次返回新数组引用，放内部避免记忆化失效
  const { resolved, unresolved, fallbackResolved } = useMemo(() => {
    let source = declarations ?? contributionRegistry.getAllWidgets()
    source = filterBySpace(source, space)
    if (excludeIds && excludeIds.length > 0) {
      source = source.filter((d) => !excludeIds.includes(d.id))
    }
    // 槽位语义：只取 id === slotId 的声明（覆盖），order 小者胜且同 id 仅保留赢家
    if (slotId) {
      source = slotWinner(source.filter((d) => d.id === slotId))
    }
    const result = resolveDeclaredWidgets(source)
    return {
      resolved: result.resolved,
      unresolved: result.unresolved,
      fallbackResolved: result.resolved.filter((r) => r.viaFallback),
    }
  }, [declarations, space, slotId, excludeIds])

  // 副作用与渲染分离（StrictMode 下渲染体副作用会双调）：
  // 未解析与降级命中都是断链早期信号，集中诊断输出
  useEffect(() => {
    if (unresolved.length > 0) {
      console.warn(
        `[DeclaredWidgetLayer] ${unresolved.length} 个 widget 声明未解析：`,
        unresolved.map((u) => ({ id: u.declaration.id, type: u.declaration.type, reason: u.reason })),
      )
    }
    if (fallbackResolved.length > 0) {
      console.warn(
        `[DeclaredWidgetLayer] ${fallbackResolved.length} 个 widget 经降级解析（type 未直接注册，存在断链风险）：`,
        fallbackResolved.map((r) => r.declaration.type),
      )
    }
  }, [unresolved, fallbackResolved])

  // 槽位默认件：无覆盖声明且有 fallback 时渲染默认组件（前端默认 + 插件可覆盖）
  if (slotId && resolved.length === 0 && fallback) {
    return (
      <div
        className={cn('flex flex-col gap-1', className)}
        data-testid="declared-widget-layer"
        data-space={space}
        data-slot={slotId}
      >
        <div data-testid={`slot-default-${slotId}`} className="min-h-0">
          <FallbackItem Component={fallback} props={fallbackProps} />
        </div>
      </div>
    )
  }

  if (resolved.length === 0) return null

  return (
    <div
      className={cn('flex flex-col gap-1', className)}
      data-testid="declared-widget-layer"
      data-space={space}
      data-slot={slotId}
    >
      {resolved.map(({ declaration, component }) => (
        <ResolvedItem key={declaration.id} declaration={declaration} Component={component} />
      ))}
    </div>
  )
}

/** 默认件渲染（props 由宿主注入；覆盖声明走 ResolvedItem 的声明 props） */
function FallbackItem({
  Component,
  props,
}: {
  Component: WidgetComponent
  props?: Record<string, unknown>
}) {
  return <Component {...(props ?? {})} />
}

export default DeclaredWidgetLayer
