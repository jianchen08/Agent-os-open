/**
 * ToolviewCardHost — chat.toolview keyed 槽渲染器（C4 消息流统一，设计真值 =
 * docs/working/DSH生态嫁接通用机制设计_20261104.md §4.4「消息流渲染器通道规则」）
 *
 * 通道规则：
 * - 内建卡（六卡+JsonTree/Generic）= component widget **静态白名单**——词表
 *   + 生成闸守门（renderCardVocabulary.generated.ts），本组件不参与内建卡
 *   渲染（禁手抄，DetailBlock 既有 switch 分发不变）
 * - 插件自定义卡 = `<pluginId>/<name>` 卡型经 chat.toolview keyed 槽寻址：
 *   channel=component 走 W 通用递送注册的 widget（props 四股份：声明 props
 *   + 工具数据 JSON 透传）；channel=webview 走重自包含视图懒加载
 *   （React.lazy 每卡型一实例缓存）
 * - fail-closed：槽装载期已拦未注册 widget；此处防御槽缺失/禁用竞态——
 *   可见占位不静默空白
 */

import React, { lazy, Suspense, useMemo } from 'react'
import { WebviewWidget } from '@/components/schema/widgets/WebviewWidget'
import { SlotEntryErrorBoundary, SlotEntryPlaceholder } from '@/components/slots/SlotHost'
import { widgetRegistry } from '@/services/schema/WidgetRegistry'
import { slotRegistry } from '@/services/slots/slotRegistry'
import type { ActivityDetailBlock } from '@/types/activity'

interface ToolviewCardHostProps {
  /** 卡型键（`<pluginId>/<name>`） */
  card: string
  /** render 意图产出的透传块（content = { args, result }） */
  block: ActivityDetailBlock
}

/** webview 卡懒加载缓存：每卡型一个 lazy 包装（模块级，跨消息实例复用） */
const lazyWebviewByCard = new Map<string, React.ComponentType<Record<string, unknown>>>()

function lazyWebviewFor(card: string): React.ComponentType<Record<string, unknown>> {
  let cached = lazyWebviewByCard.get(card)
  if (!cached) {
    // 动态加载同一 WebviewWidget 实现（重自包含视图：独立文档/IPC 桥），
    // lazy 层让消息列表首帧不为重卡付费
    cached = lazy(async () => ({ default: WebviewWidget }))
    lazyWebviewByCard.set(card, cached)
  }
  return cached
}

export function ToolviewCardHost({ card, block }: ToolviewCardHostProps): React.ReactElement {
  const entry = useMemo(() => slotRegistry.getKeyedEntry('chat.toolview', card), [card])
  if (!entry) {
    return (
      <SlotEntryPlaceholder
        testId="toolview-card-missing"
        text={`自定义卡 ${card} 未装载（插件禁用或声明缺失）`}
      />
    )
  }
  const payload = (typeof block.content === 'object' ? block.content : {}) as Record<string, unknown>
  if (entry.channel === 'webview') {
    const LazyWebview = lazyWebviewFor(card)
    return (
      <Suspense fallback={<SlotEntryPlaceholder testId="toolview-card-loading" text="加载自定义卡…" />}>
        <SlotEntryErrorBoundary label={card}>
          <LazyWebview
            pluginId={entry.pluginId}
            htmlPath={entry.htmlPath ?? ''}
            widgetId={entry.id}
            {...payload}
          />
        </SlotEntryErrorBoundary>
      </Suspense>
    )
  }
  const Widget = widgetComponentFor(entry.widget ?? '')
  if (!Widget) {
    return (
      <SlotEntryPlaceholder
        testId="toolview-card-missing"
        text={`自定义卡 ${card} 的 widget "${entry.widget ?? ''}" 当前不可用`}
      />
    )
  }
  return (
    <SlotEntryErrorBoundary label={card}>
      {/* props 四股份：声明 props + 工具数据（JSON 透传，组件不见 store/ctx） */}
      <Widget {...(entry.props ?? {})} card={card} args={payload.args ?? null} result={payload.result ?? null} />
    </SlotEntryErrorBoundary>
  )
}

/** widgetRegistry 直查（静态白名单 + 动态插件命名空间双源统一入口） */
function widgetComponentFor(name: string): React.ComponentType<Record<string, unknown>> | undefined {
  return widgetRegistry.get(name) as React.ComponentType<Record<string, unknown>> | undefined
}
