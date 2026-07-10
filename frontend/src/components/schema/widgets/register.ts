/**
 * Widget 注册模块
 *
 * 将所有 8 种基础 Widget 组件注册到全局 widgetRegistry。
 * 导出 registerAllWidgets() 函数供应用启动时调用。
 *
 * @module widgets/register
 */

import { widgetRegistry } from '@/services/schema/WidgetRegistry'
import { ChartWidget } from './ChartWidget'
import { CodeBlockWidget } from './CodeBlockWidget'
import { DecisionWidget } from './DecisionWidget'
import { FileTreeWidget } from './FileTreeWidget'
import { FormWidget } from './FormWidget'
import { GalleryWidget } from './GalleryWidget'
import { ProgressWidget } from './ProgressWidget'
import { StatusCardWidget } from './StatusCardWidget'
import { TableWidget } from './TableWidget'
import type { ComponentType } from 'react'

/** Widget 注册条目定义 */
interface WidgetRegistration {
  /** 组件类型标识 */
  type: string
  /** React 组件 */
  component: ComponentType<Record<string, unknown>>
  /** 显示名称 */
  name: string
  /** 组件描述 */
  description: string
  /** 支持的渲染空间 */
  supportedSpaces: Array<'chat' | 'workspace' | 'floating' | 'dock' | 'fullscreen'>
  /** 降级组件类型 */
  fallbackWidget?: string
}

/** 所有基础 Widget 注册配置 */
const WIDGET_REGISTRATIONS: WidgetRegistration[] = [
  {
    type: 'form',
    component: FormWidget,
    name: '表单',
    description: '可交互表单组件，支持多种字段类型和布局',
    supportedSpaces: ['chat', 'workspace'],
  },
  {
    type: 'chart',
    component: ChartWidget,
    name: '图表',
    description: '数据可视化图表，支持折线、柱状、饼图等多种类型',
    supportedSpaces: ['chat', 'workspace', 'floating'],
  },
  {
    type: 'gallery',
    component: GalleryWidget,
    name: '画廊',
    description: '图片画廊，支持网格布局和点击放大预览',
    supportedSpaces: ['chat', 'workspace', 'floating'],
  },
  {
    type: 'table',
    component: TableWidget,
    name: '表格',
    description: '数据表格，支持排序、分页和斑马纹样式',
    supportedSpaces: ['chat', 'workspace'],
    fallbackWidget: 'status_card',
  },
  {
    type: 'progress',
    component: ProgressWidget,
    name: '进度',
    description: '进度展示，支持单进度、多步骤和不确定进度',
    supportedSpaces: ['chat', 'workspace'],
    fallbackWidget: 'status_card',
  },
  {
    type: 'code_block',
    component: CodeBlockWidget,
    name: '代码块',
    description: '代码展示，支持语法高亮和复制功能',
    supportedSpaces: ['chat', 'workspace'],
  },
  {
    type: 'status_card',
    component: StatusCardWidget,
    name: '状态卡片',
    description: '状态指标卡片，支持趋势显示和多指标',
    supportedSpaces: ['chat', 'workspace', 'floating'],
  },
  {
    type: 'decision',
    component: DecisionWidget,
    name: '决策',
    description: '决策选择组件，支持单选和多选模式',
    supportedSpaces: ['chat'],
    fallbackWidget: 'form',
  },
  {
    type: 'file_tree',
    component: FileTreeWidget,
    name: '文件树',
    description: '通用树形结构组件，支持递归嵌套、状态显示和进度追踪',
    supportedSpaces: ['chat', 'workspace'],
    fallbackWidget: 'table',
  },
  {
    type: 'tree',
    component: FileTreeWidget,
    name: '树形组件',
    description: '通用树形结构组件，支持递归嵌套、状态显示和进度追踪',
    supportedSpaces: ['chat', 'workspace'],
    fallbackWidget: 'table',
  },
]

/**
 * 注册所有基础 Widget 组件
 *
 * 遍历 WIDGET_REGISTRATIONS 配置，将每个组件注册到 widgetRegistry。
 * 注册后所有渲染场景（聊天、工作区等）中自动可用。
 *
 * @example
 * ```ts
 * import { registerAllWidgets } from '@/components/schema/widgets/register'
 *
 * // 应用启动时调用一次
 * registerAllWidgets()
 * ```
 */
export function registerAllWidgets(): void {
  for (const reg of WIDGET_REGISTRATIONS) {
    widgetRegistry.register(
      reg.type,
      reg.component,
      {
        name: reg.name,
        description: reg.description,
        supportedSpaces: reg.supportedSpaces,
        fallbackWidget: reg.fallbackWidget,
      },
    )
  }
}

/**
 * 获取所有 Widget 注册配置
 *
 * 用于调试和展示已注册组件列表。
 *
 * @returns Widget 注册配置数组（只读）
 */
export function getWidgetRegistrations(): readonly WidgetRegistration[] {
  return WIDGET_REGISTRATIONS
}
