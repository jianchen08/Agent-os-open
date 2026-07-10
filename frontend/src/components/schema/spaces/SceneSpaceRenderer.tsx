/**
 * 场景空间渲染器
 *
 * 基于 UI Schema 的动态渲染区域，集成 SchemaRenderer 和 CompositionEngine。
 * 支持媒体组件嵌入（AudioPlayer、ImageGallery）和多种布局引擎
 * （GridLayout、SplitLayout、TabLayout、StackLayout）。
 *
 * @module spaces/SceneSpaceRenderer
 */

import React, { useMemo } from 'react'
import { AudioPlayer } from '@/components/media/AudioPlayer'
import { ImageGallery } from '@/components/media/ImageGallery'
import { CompositionRenderer } from '@/components/schema/composition/CompositionRenderer'
import { GridLayout } from '@/components/schema/composition/GridLayout'
import { SplitLayout } from '@/components/schema/composition/SplitLayout'
import { StackLayout } from '@/components/schema/composition/StackLayout'
import { TabLayout } from '@/components/schema/composition/TabLayout'
import type { Scene, SceneWidgetConfig, SceneLayoutConfig } from '@/services/api/scenes'

/** SceneSpaceRenderer 属性 */
export interface SceneSpaceRendererProps {
  /** 场景数据 */
  scene: Scene
  /** 自定义组件渲染器（可选） */
  componentRenderer?: (widget: SceneWidgetConfig) => React.ReactNode
  /** 额外的 CSS 类名 */
  className?: string
}

/**
 * 场景空间渲染器
 *
 * 接收 Scene 数据，根据 layout 配置选择布局引擎，
 * 渲染各组件。支持：
 * - 媒体组件嵌入：AudioPlayer、ImageGallery
 * - 布局引擎：grid、split、stack、tab
 * - 与 CompositionRenderer 集成渲染复合组件
 *
 * @param props - 渲染器属性
 * @returns 渲染结果
 */
export function SceneSpaceRenderer({
  scene,
  componentRenderer,
  className,
}: SceneSpaceRendererProps): React.ReactNode {
  const { layout, widgets } = scene

  // 按 position 排序组件
  const sortedWidgets = useMemo(
    () => [...widgets].sort((a, b) => a.position - b.position),
    [widgets],
  )

  // 渲染各组件为 React 节点
  const renderedWidgets = useMemo(
    () => sortedWidgets.map((w) => renderWidget(w, componentRenderer)),
    [sortedWidgets, componentRenderer],
  )

  // 无组件时显示占位
  if (renderedWidgets.length === 0) {
    return (
      <div className={className ?? 'flex h-full items-center justify-center'}>
        <div className="flex flex-col items-center gap-2 text-center">
          <div className="bg-muted text-muted-foreground rounded-full p-3">
            <svg
              className="h-5 w-5"
              fill="none"
              viewBox="0 0 24 24"
              stroke="currentColor"
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                strokeWidth={2}
                d="M4 6h16M4 12h16M4 18h16"
              />
            </svg>
          </div>
          <p className="text-muted-foreground text-sm">暂无场景内容</p>
        </div>
      </div>
    )
  }

  // 根据布局类型选择布局引擎
  const content = renderLayout(layout, renderedWidgets, sortedWidgets)

  return <div className={className ?? 'h-full w-full'}>{content}</div>
}

/**
 * 渲染单个组件
 *
 * 根据组件类型分发到对应的渲染逻辑，支持：
 * - audio_player → AudioPlayer
 * - image_gallery → ImageGallery
 * - 其他 → 占位符 / 自定义渲染器
 *
 * @param widget - 组件配置
 * @param customRenderer - 自定义渲染器
 * @returns 渲染结果
 */
function renderWidget(
  widget: SceneWidgetConfig,
  customRenderer?: (widget: SceneWidgetConfig) => React.ReactNode,
): React.ReactNode {
  // 优先使用自定义渲染器
  if (customRenderer) {
    return customRenderer(widget)
  }

  const { widget_type, props: widgetProps } = widget

  switch (widget_type) {
    case 'audio_player':
      return (
        <AudioPlayer
          src={(widgetProps.src as string) || ''}
          title={(widgetProps.title as string) || '音频'}
        />
      )

    case 'image_gallery':
      return (
        <ImageGallery
          images={
            Array.isArray(widgetProps.images)
              ? (widgetProps.images as Array<{
                  url: string
                  alt?: string
                  prompt?: string
                }>)
              : []
          }
          columns={(widgetProps.columns as number) || 3}
        />
      )

    case 'chat':
      return (
        <div className="flex h-full flex-col">
          <div className="border-border flex items-center border-b px-4 py-2">
            <span className="text-sm font-medium">💬 聊天面板</span>
          </div>
          <div className="flex flex-1 items-center justify-center">
            <p className="text-muted-foreground text-sm">聊天交互区</p>
          </div>
        </div>
      )

    case 'workspace':
      return (
        <div className="flex h-full flex-col">
          <div className="border-border flex items-center border-b px-4 py-2">
            <span className="text-sm font-medium">🛠 工作区</span>
          </div>
          <div className="flex flex-1 items-center justify-center">
            <p className="text-muted-foreground text-sm">工作区内容</p>
          </div>
        </div>
      )

    case 'chart':
      return (
        <div className="flex h-full flex-col rounded-lg border p-4">
          <h3 className="mb-3 text-sm font-medium">
            {(widgetProps.title as string) || '📊 图表'}
          </h3>
          <div className="flex flex-1 items-center justify-center">
            <p className="text-muted-foreground text-sm">
              {((widgetProps.chartType as string) || 'line').toUpperCase()} 图表区域
            </p>
          </div>
        </div>
      )

    case 'table':
      return (
        <div className="flex h-full flex-col rounded-lg border p-4">
          <h3 className="mb-3 text-sm font-medium">
            {(widgetProps.title as string) || '📋 数据表格'}
          </h3>
          <div className="flex flex-1 items-center justify-center">
            <p className="text-muted-foreground text-sm">表格数据区</p>
          </div>
        </div>
      )

    case 'status_card':
      return (
        <div className="flex h-full flex-col rounded-lg border p-4">
          <h3 className="mb-3 text-sm font-medium">
            {(widgetProps.title as string) || '🟢 状态卡片'}
          </h3>
          <div className="flex flex-1 items-center justify-center">
            <p className="text-muted-foreground text-sm">系统状态概览</p>
          </div>
        </div>
      )

    default:
      // 未知组件类型：显示占位符
      return (
        <div className="flex h-full flex-col items-center justify-center rounded-lg border border-dashed p-6">
          <p className="text-muted-foreground text-sm">
            组件: {widget_type}
          </p>
        </div>
      )
  }
}

/**
 * 根据布局配置选择布局引擎并渲染
 *
 * @param layout - 布局配置
 * @param children - 已渲染的子组件
 * @param widgets - 组件配置列表（用于 Tab 标签等）
 * @returns 布局渲染结果
 */
function renderLayout(
  layout: SceneLayoutConfig,
  children: React.ReactNode[],
  widgets: SceneWidgetConfig[],
): React.ReactNode {
  switch (layout.type) {
    case 'grid':
      return (
        <GridLayout
          layoutProps={{
            columns: layout.columns ?? 2,
          }}
        >
          {children}
        </GridLayout>
      )

    case 'split':
      return (
        <SplitLayout
          direction={layout.direction === 'vertical' ? 'vertical' : 'horizontal'}
          layoutProps={{
            ratio: layout.ratio ?? undefined,
          }}
        >
          {children}
        </SplitLayout>
      )

    case 'tab':
      return (
        <TabLayout
          layoutProps={{
            defaultTab: layout.default_tab ?? 0,
          }}
          tabs={widgets.map((w) => ({
            title: (w.props.title as string) || w.widget_type,
            icon: (w.props.icon as string) || undefined,
          }))}
        >
          {children}
        </TabLayout>
      )

    case 'stack':
      return <StackLayout>{children}</StackLayout>

    default:
      // 默认使用堆叠布局
      return <StackLayout>{children}</StackLayout>
  }
}

SceneSpaceRenderer.displayName = 'SceneSpaceRenderer'

export default SceneSpaceRenderer
