/**
 * ChatFloatingLayer · 聊天区通用悬浮挂载槽（2026-10-24 用户裁定：悬浮挂载
 * 槽位置不固定，按插件声明定位）。
 *
 * 纯基础设施：消费 space=floating / slot=panel 且 props.anchor 在场的页面
 * 声明，按声明定位渲染插件 webview 小部件——本层零业务知识（不知道任何
 * 插件做什么），位置/偏移全由插件声明指定：
 *   props.anchor: 'top-right' | 'top-left' | 'bottom-right' | 'bottom-left'
 *   props.offset: { x?: number; y?: number }（像素，缺省 12）
 * 无 anchor 的 floating 声明（浮窗面板等既有形态）不经本层。
 */

import { type CSSProperties, type FC } from 'react'
import { WebviewWidget } from '@/components/schema/widgets/WebviewWidget'
import { contributionRegistry, type PageDeclaration } from '@/services/schema/ContributionRegistry'

type Anchor = 'top-right' | 'top-left' | 'bottom-right' | 'bottom-left'

interface FloatingDeclProps {
  anchor?: unknown
  offset?: { x?: number; y?: number } | unknown
  pluginId?: unknown
  htmlPath?: unknown
  widgetId?: unknown
  /**
   * 透明底声明（2026-10-25 用户实测"圆角钮带方框背景"）：原样透传给
   * WebviewWidget——悬浮叠加形态 iframe 元素底改透明，插件页自带圆角透出
   * 宿主；缺省 false = canvas 画布底（全页面形态）。
   */
  transparent?: unknown
}

const isAnchor = (v: unknown): v is Anchor =>
  v === 'top-right' || v === 'top-left' || v === 'bottom-right' || v === 'bottom-left'

function positionOf(anchor: Anchor, offset: { x?: number; y?: number } | undefined): CSSProperties {
  const x = offset?.x ?? 12
  const y = offset?.y ?? 12
  const pos: CSSProperties = {}
  if (anchor.startsWith('top')) pos.top = y
  else pos.bottom = y
  if (anchor.endsWith('right')) pos.right = x
  else pos.left = x
  return pos
}

export const ChatFloatingLayer: FC = () => {
  const decls = contributionRegistry
    .getPagesBySpace('floating')
    .filter((p) => p.slot === 'panel')

  const items = decls.flatMap((page: PageDeclaration) => {
    const props = (page.props ?? {}) as FloatingDeclProps
    if (!isAnchor(props.anchor)) return []
    const pluginId =
      typeof props.pluginId === 'string' && props.pluginId !== ''
        ? props.pluginId
        : (page.pluginId ?? '')
    if (!pluginId) return []
    const offset = isOffset(props.offset) ? props.offset : undefined
    return [
      {
        key: `${pluginId}:${page.id}`,
        style: positionOf(props.anchor, offset),
        pluginId,
        htmlPath: typeof props.htmlPath === 'string' ? props.htmlPath : undefined,
        widgetId:
          typeof props.widgetId === 'string' && props.widgetId !== ''
            ? props.widgetId
            : `float:${pluginId}:${page.id}`,
        title: page.title ?? page.id,
        transparent: props.transparent === true,
      },
    ]
  })

  if (items.length === 0) return null

  return (
    <div className="pointer-events-none absolute inset-0 z-20" data-testid="chat-floating-layer">
      {items.map((item) => (
        <div
          key={item.key}
          className="pointer-events-auto absolute"
          style={item.style}
          data-testid="chat-floating-item"
        >
          <WebviewWidget
            pluginId={item.pluginId}
            htmlPath={item.htmlPath}
            widgetId={item.widgetId}
            title={item.title}
            autoHeight
            transparent={item.transparent}
          />
        </div>
      ))}
    </div>
  )
}

function isOffset(v: unknown): v is { x?: number; y?: number } {
  if (typeof v !== 'object' || v === null) return false
  const o = v as { x?: unknown; y?: unknown }
  return (o.x === undefined || typeof o.x === 'number') && (o.y === undefined || typeof o.y === 'number')
}

export default ChatFloatingLayer
