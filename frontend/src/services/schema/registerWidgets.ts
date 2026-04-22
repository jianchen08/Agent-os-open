/**
 * 组件注册初始化
 *
 * 将所有已实现的组件注册到 widgetRegistry
 */

import { widgetRegistry } from './composer'
import { FormWidget } from '@/components/schema/widgets/FormWidget'
import { ChartWidget } from '@/components/schema/widgets/ChartWidget'
import { GalleryWidget } from '@/components/schema/widgets/GalleryWidget'
import { TableWidget } from '@/components/schema/widgets/TableWidget'
import { ProgressWidget } from '@/components/schema/widgets/ProgressWidget'
import { CodeBlockWidget } from '@/components/schema/widgets/CodeBlockWidget'
import { StatusCardWidget } from '@/components/schema/widgets/StatusCardWidget'
import { DecisionWidget } from '@/components/schema/widgets/DecisionWidget'

/**
 * 初始化所有预置组件注册
 *
 * 将 8 个基础组件注册到 widgetRegistry，
 * 每个组件声明支持的空间类型和降级组件
 */
export function initializeWidgets(): void {
  const widgets = [
    { name: 'form', component: FormWidget, spaces: ['chat', 'workspace'], fallback: undefined },
    { name: 'chart', component: ChartWidget, spaces: ['chat', 'workspace', 'floating'], fallback: undefined },
    { name: 'gallery', component: GalleryWidget, spaces: ['chat', 'workspace', 'floating'], fallback: undefined },
    { name: 'table', component: TableWidget, spaces: ['chat', 'workspace'], fallback: 'status_card' },
    { name: 'progress', component: ProgressWidget, spaces: ['chat', 'workspace'], fallback: 'status_card' },
    { name: 'code_block', component: CodeBlockWidget, spaces: ['chat', 'workspace'], fallback: undefined },
    { name: 'status_card', component: StatusCardWidget, spaces: ['chat', 'workspace', 'floating'], fallback: undefined },
    { name: 'decision', component: DecisionWidget, spaces: ['chat'], fallback: 'form' },
  ]

  widgets.forEach(({ name, component, spaces }) => {
    widgetRegistry.register(name, {
      component: component as React.ComponentType<Record<string, unknown>>,
      supportedSpaces: spaces,
    })
  })
}
