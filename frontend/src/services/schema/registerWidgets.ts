/** 组件注册初始化 将所有已实现的组件注册到 widgetRegistry */

import { ChartWidget } from '@/components/schema/widgets/ChartWidget'
import { CodeBlockWidget } from '@/components/schema/widgets/CodeBlockWidget'
import { DecisionWidget } from '@/components/schema/widgets/DecisionWidget'
import { FileTreeWidget } from '@/components/schema/widgets/FileTreeWidget'
import { FormWidget } from '@/components/schema/widgets/FormWidget'
import { GalleryWidget } from '@/components/schema/widgets/GalleryWidget'
import { HtmlPreviewWidget } from '@/components/schema/widgets/HtmlPreviewWidget'
import { ProgressWidget } from '@/components/schema/widgets/ProgressWidget'
import { StatusCardWidget } from '@/components/schema/widgets/StatusCardWidget'
import { TableWidget } from '@/components/schema/widgets/TableWidget'
import { widgetRegistry as composerRegistry } from './composer'
import { widgetRegistry } from './WidgetRegistry'
import type { WidgetComponent } from './WidgetRegistry'

/** 初始化所有预置组件注册 同时注册到两套 Widget Registry */
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
    {
      name: 'html_preview',
      component: HtmlPreviewWidget,
      spaces: ['workspace', 'floating', 'fullscreen'],
      fallback: 'code_block',
    },
  ]

  widgets.forEach(({ name, component, spaces, fallback }) => {
    // 注册到 Composer 的 registry（用于消息渲染管道）
    composerRegistry.register(name, {
      component: component as React.ComponentType<Record<string, unknown>>,
      supportedSpaces: spaces,
    })
    // 注册到 WidgetRegistry（用于 RenderingEngine 独立渲染）
    widgetRegistry.register(name, component as WidgetComponent, {
      name,
      supportedSpaces: spaces as Array<'chat' | 'workspace' | 'floating' | 'dock' | 'fullscreen'>,
      fallbackWidget: fallback,
    })
  })
}
