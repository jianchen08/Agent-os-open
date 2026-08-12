/**
 * DeclaredWidgetLayer — ui_schema 声明 widget 的渲染层（架构 §5.3 的生产消费者）
 *
 * 架构意图：contributionRegistry.getAllWidgets()（agents/pipelines 的 ui_schema.widgets）
 * 经 resolveDeclaredWidgets 桥梁解析为组件，按 space 渲染。这是「声明 → 实现」链路
 * 在渲染侧的落点，闭合架构断裂点 ⑧（getAllWidgets 零消费者）。
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

export function DeclaredWidgetLayer({
  space,
  declarations,
  className,
}: DeclaredWidgetLayerProps): ReactNode {
  // 读取移入 useMemo：getAllWidgets() 每次返回新数组引用，放内部避免记忆化失效
  const { resolved, unresolved, fallbackResolved } = useMemo(() => {
    const source = declarations ?? contributionRegistry.getAllWidgets()
    const result = resolveDeclaredWidgets(filterBySpace(source, space))
    return {
      resolved: result.resolved,
      unresolved: result.unresolved,
      fallbackResolved: result.resolved.filter((r) => r.viaFallback),
    }
  }, [declarations, space])

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
        fallbackResolved.map((r) => ({ id: r.declaration.id, type: r.declaration.type })),
      )
    }
  }, [unresolved, fallbackResolved])

  if (resolved.length === 0) return null

  return (
    <div
      className={cn('flex flex-col gap-1', className)}
      data-testid="declared-widget-layer"
      data-space={space}
    >
      {resolved.map(({ declaration, component }) => (
        <ResolvedItem key={declaration.id} declaration={declaration} Component={component} />
      ))}
    </div>
  )
}

export default DeclaredWidgetLayer
