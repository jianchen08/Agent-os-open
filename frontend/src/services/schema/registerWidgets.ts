/**
 * 组件注册初始化
 *
 * 将所有已实现的组件注册到 widgetRegistry
 */

import { ChartWidget } from '@/components/schema/widgets/ChartWidget'
import { CodeBlockWidget } from '@/components/schema/widgets/CodeBlockWidget'
import { DecisionWidget } from '@/components/schema/widgets/DecisionWidget'
import { FormWidget } from '@/components/schema/widgets/FormWidget'
import { GalleryWidget } from '@/components/schema/widgets/GalleryWidget'
import { ProgressWidget } from '@/components/schema/widgets/ProgressWidget'
import { StatusCardWidget } from '@/components/schema/widgets/StatusCardWidget'
import { TableWidget } from '@/components/schema/widgets/TableWidget'
import { FileTreeWidget } from '@/components/schema/widgets/FileTreeWidget'
import { widgetRegistry as composerRegistry } from './composer'
import { widgetRegistry } from './WidgetRegistry'
import type { WidgetComponent } from './WidgetRegistry'

/**
 * 初始化所有预置组件注册
 *
 * BUG-FIX-fix_20260505_001: 同时注册到两套 Widget Registry
 * 问题根因: 组件只注册到 composer.tsx 的 widgetRegistry，RenderingEngine 使用的是 WidgetRegistry.ts 的 widgetRegistry
 * 修复方案: 遍历 widgets 时同时注册到两个 registry
 *
 * 将组件注册到 composer 的 registry（兼容旧代码）和 WidgetRegistry.ts 的 registry（RenderingEngine 使用）
 */
export function initializeWidgets(): void {
  const widgets = [
    { name: 'form', component: FormWidget, spaces: ['chat', 'workspace'], fallback: undefined },
    {
      name: 'chart',
      component: ChartWidget,
      spaces: ['chat', 'workspace', 'floating'],
      fallback: undefined,
    },
    {
      name: 'gallery',
      component: GalleryWidget,
      spaces: ['chat', 'workspace', 'floating'],
      fallback: undefined,
    },
    {
      name: 'table',
      component: TableWidget,
      spaces: ['chat', 'workspace'],
      fallback: 'status_card',
    },
    {
      name: 'progress',
      component: ProgressWidget,
      spaces: ['chat', 'workspace'],
      fallback: 'status_card',
    },
    {
      name: 'code_block',
      component: CodeBlockWidget,
      spaces: ['chat', 'workspace'],
      fallback: undefined,
    },
    {
      name: 'status_card',
      component: StatusCardWidget,
      spaces: ['chat', 'workspace', 'floating'],
      fallback: undefined,
    },
    { name: 'decision', component: DecisionWidget, spaces: ['chat'], fallback: 'form' },
    {
      name: 'file_tree',
      component: FileTreeWidget,
      spaces: ['chat', 'workspace'],
      fallback: 'table',
    },
    {
      name: 'tree',
      component: FileTreeWidget,
      spaces: ['chat', 'workspace'],
      fallback: 'table',
    },
  ]

  widgets.forEach(({ name, component, spaces, fallback }) => {
    // 注册到 composer 的 registry（兼容旧代码）
    composerRegistry.register(name, {
      component: component as React.ComponentType<Record<string, unknown>>,
      supportedSpaces: spaces,
    })
    // 注册到 WidgetRegistry.ts 的 registry（RenderingEngine 使用）
    widgetRegistry.register(name, component as WidgetComponent, {
      name,
      supportedSpaces: spaces as Array<'chat' | 'workspace' | 'floating' | 'dock' | 'fullscreen'>,
      fallbackWidget: fallback,
    })
  })
}
