/**
 * widgetChain — 接通「声明 → 实现」链路（架构 §5.3）
 *
 * 现状断链：contributionRegistry.getAllWidgets()（agents/pipelines 的 ui_schema.widgets
 * 声明）与 widgetRegistry（type → 组件）之间没有桥梁，声明侧零消费者。
 *
 * 本模块是那条桥梁：把 WidgetDeclaration[] 经 widgetRegistry 解析为可渲染组件。
 * 渲染侧（DeclaredWidgetLayer / PageRenderer）应通过本函数消费 getAllWidgets()，
 * 而不是绕过声明表直接查 widgetRegistry（那是 §5.3 批判的捷径）。
 *
 * 设计意图见 docs/working/重要设计/前端能力统一架构.md §5.3。
 */

import { widgetRegistry } from './WidgetRegistry'
import type { WidgetDeclaration } from './ContributionRegistry'
import type { WidgetComponent } from './WidgetRegistry'

/** 已解析的 widget：声明 + 组件 */
export interface ResolvedWidget {
  declaration: WidgetDeclaration
  component: WidgetComponent
  /** true 表示 type 未直接注册，走了降级链（断链的早期信号，调用方应可观测） */
  viaFallback: boolean
}

/** 未解析的 widget：声明 + 无法解析的原因 */
export interface UnresolvedWidget {
  declaration: WidgetDeclaration
  reason: string
}

/** 链路解析结果 */
export interface WidgetChainResult {
  resolved: ResolvedWidget[]
  unresolved: UnresolvedWidget[]
}

/**
 * 把 widget 声明经 widgetRegistry 解析为可渲染组件
 *
 * 解析顺序：精确注册 → findFallback 降级链 → 标记未解析。
 * 未解析项不静默丢弃，而是回传给调用方决定兜底（禁止假装一切正常——
 * information_integrity / code_reviewer §3 最终兜底原则）。
 *
 * @param declarations - 来自 contributionRegistry.getAllWidgets() 的声明
 * @param registry - 可注入，便于测试隔离（默认全局单例）
 */
export function resolveDeclaredWidgets(
  declarations: WidgetDeclaration[],
  registry: { get: (type: string) => WidgetComponent | undefined; findFallback: (type: string) => WidgetComponent | undefined } = widgetRegistry,
): WidgetChainResult {
  const resolved: ResolvedWidget[] = []
  const unresolved: UnresolvedWidget[] = []

  for (const declaration of declarations) {
    const direct = registry.get(declaration.type)
    if (direct) {
      resolved.push({ declaration, component: direct, viaFallback: false })
      continue
    }

    const fallback = registry.findFallback(declaration.type)
    if (fallback) {
      resolved.push({ declaration, component: fallback, viaFallback: true })
      continue
    }

    unresolved.push({
      declaration,
      reason: `类型 "${declaration.type}" 既无直接注册也无降级路径`,
    })
  }

  return { resolved, unresolved }
}
