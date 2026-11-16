/**
 * SlotHost — 槽位锚点宿主（C4 前端槽位扩展机制，设计真值 =
 * docs/working/DSH生态嫁接通用机制设计_20261104.md §4.4）
 *
 * 渲染契约（防错位契约，宿主全责）：
 * - 槽条目只填内容，声明面无 width/height/position 字段；锚点容器承载
 *   边界盒（overflow 约束），布局域契约随域而定（sidebar.block=全宽纵向
 *   卡流带上限）
 * - 每条目独立 ErrorBoundary：单条目炸只空一格（可见占位），不拖塌布局
 * - fail-closed 链路：装载期（SlotRegistry）已拦表外槽 key/未注册 widget；
 *   渲染期防御（热禁用竞态窗口）落可见占位，不静默空白（AC-F2）
 * - 零域词表：标题/标签随声明下发，宿主不内嵌任何域词
 */

import React, { useMemo, useSyncExternalStore } from 'react'
import { X } from '@/assets/icons'
import { WebviewWidget } from '@/components/schema/widgets/WebviewWidget'
import { cn } from '@/lib/utils'
import { evaluateWhen, type ContextKeys } from '@/services/schema/whenExpression'
import { widgetRegistry } from '@/services/schema/WidgetRegistry'
import { slotRegistry, type SlotEntry } from '@/services/slots/slotRegistry'
import { windowManager } from '@/services/window/WindowManager'
import type { ComponentType } from 'react'

interface SlotHostProps {
  /** 槽 key（词表内且开放；表外渲染零节点） */
  slotKey: string
  /** when 求值上下文（context keys；缺省仅内置键，条目声明自定义键按缺席求值） */
  whenContext?: ContextKeys
  /** 容器附加类（锚点域布局契约由调用侧注入，如 sidebar.block 带上限） */
  className?: string
}

/**
 * 槽位锚点：渲染某槽域全部条目（list 加聚排序 / keyed 全量；keyed 单键寻址
 * 场景请直接消费 slotRegistry.getKeyedEntry）。无条目渲染零节点。
 */
export function SlotHost({ slotKey, whenContext, className }: SlotHostProps): React.ReactElement | null {
  // 订阅注册表版本（非响应式单例的渲染响应化，ContributionRegistry 同款纪律）
  useSyncExternalStore(slotRegistry.subscribe, slotRegistry.getVersion)
  const entries = useMemo(
    () =>
      slotRegistry.getSlotEntries(slotKey).filter((e) =>
        evaluateWhen(e.when, whenContext ?? {}),
      ),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [slotKey, slotRegistry.getVersion(), whenContext],
  )

  if (entries.length === 0) return null

  return (
    <div
      data-slot-key={slotKey}
      data-testid={`slot-host-${slotKey}`}
      // 防御性边界盒：条目内容溢出约束在宿主容器内（条目声明无几何字段）
      className={cn('flex min-w-0 min-h-0 flex-col gap-1 overflow-hidden', className)}
    >
      {entries.map((entry) => (
        <SlotEntryFrame key={entry.id} entry={entry} />
      ))}
    </div>
  )
}

/** 单条目框：边界盒 + detachable 弹出入口 + ErrorBoundary + 通道内容 */
export function SlotEntryFrame({ entry }: { entry: SlotEntry }): React.ReactElement {
  const canPopout = entry.detachable?.popout !== false && !!entry.detachable
  return (
    <div
      className="relative min-h-0 min-w-0 overflow-hidden"
      data-testid={`slot-entry-${entry.id}`}
      aria-label={entry.title ?? entry.id}
    >
      {canPopout && (
        <button
          type="button"
          onClick={() => windowManager.openPopout(slotEntryAsPopoutPage(entry))}
          className="text-muted-foreground hover:text-foreground absolute right-1 top-1 z-10 rounded p-0.5"
          title={entry.title ? `弹出 ${entry.title}` : '弹出'}
          aria-label="弹出为浮窗"
          data-testid={`slot-entry-popout-${entry.id}`}
        >
          <X className="h-3 w-3 rotate-45" />
        </button>
      )}
      <SlotEntryErrorBoundary label={entry.id}>
        <SlotEntryContent entry={entry} />
      </SlotEntryErrorBoundary>
    </div>
  )
}

/**
 * 单条目通道内容（SlotOverlayHost/弹出链复用）：
 * - component：widgetRegistry 解析（静态白名单 + 动态插件命名空间双源），
 *   声明 props 直通（跨域只传 JSON）
 * - webview：WebviewWidget 沙箱承载（独立文档/IPC 桥，不可信插件代码执行面）
 */
export function SlotEntryContent({ entry }: { entry: SlotEntry }): React.ReactElement {
  if (entry.channel === 'webview') {
    return (
      <WebviewWidget
        pluginId={entry.pluginId}
        htmlPath={entry.htmlPath ?? ''}
        widgetId={entry.id}
        {...(entry.props ?? {})}
      />
    )
  }
  const Widget = widgetRegistry.get(entry.widget ?? '')
  if (!Widget) {
    // 渲染期防御（装载已拦，这里是热禁用竞态窗口）：可见占位不空白
    return (
      <SlotEntryPlaceholder
        testId="slot-entry-error"
        text={`${entry.id}：widget "${entry.widget ?? ''}" 当前不可用`}
      />
    )
  }
  const WidgetComponent = Widget as ComponentType<Record<string, unknown>>
  return <WidgetComponent {...(entry.props ?? {})} />
}

/** 条目错误占位（可见降级，非静默空白——FE3 兜底反模式禁令） */
export function SlotEntryPlaceholder({ testId, text }: { testId: string; text: string }): React.ReactElement {
  return (
    <div
      data-testid={testId}
      className="text-muted-foreground border-border/60 rounded border border-dashed p-2 text-xs"
    >
      {text}
    </div>
  )
}

/**
 * 每实例 ErrorBoundary：单条目炸只空一格（可见占位），不拖塌宿主布局。
 * SlotHost 槽条目与 ToolviewCardHost 自定义卡共用。
 */
export class SlotEntryErrorBoundary extends React.Component<
  { label: string; children: React.ReactNode },
  { error: Error | null }
> {
  state = { error: null as Error | null }

  static getDerivedStateFromError(error: Error): { error: Error } {
    return { error }
  }

  componentDidCatch(error: Error): void {
    console.error(`[SlotHost] 槽条目渲染异常（已隔离）: ${this.props.label}`, error)
  }

  render(): React.ReactNode {
    if (this.state.error) {
      return <SlotEntryPlaceholder testId="slot-entry-error" text={`${this.props.label}：组件渲染异常，已隔离`} />
    }
    return this.props.children
  }
}

/** 槽条目 → 弹出浮窗页声明（detachable 尺寸协商：仅弹出自管 defaultSize） */
function slotEntryAsPopoutPage(entry: SlotEntry): Parameters<typeof windowManager.openPopout>[0] {
  const webviewChannel = entry.channel === 'webview'
  return {
    type: 'pages',
    id: entry.id,
    title: entry.title ?? entry.id,
    icon: entry.icon,
    space: 'floating',
    widget: webviewChannel ? 'webview' : entry.widget,
    props: webviewChannel
      ? { pluginId: entry.pluginId, htmlPath: entry.htmlPath, widgetId: entry.id }
      : entry.props,
    detachable: entry.detachable,
    pluginId: entry.pluginId,
  } as Parameters<typeof windowManager.openPopout>[0]
}
