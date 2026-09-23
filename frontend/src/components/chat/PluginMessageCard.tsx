/**
 * PluginMessageCard · 消息卡通用 webview 容器（模式体系落地设计 §5.0）
 *
 * 聊天消息渲染路径的 message-style 路由终点：插件经 contributes.chatMessages
 * 声明样式（id=样式 id，props.htmlPath 指包内 HTML），消息携带
 * `metadata.message_style = <样式 id>` 即渲染为本容器——复用 WebviewWidget
 * 的 srcDoc 沙箱（opaque-origin iframe + CSP + 实例令牌桥），宿主零模式专属
 * 消息组件；声明禁用（registry 无此样式）同源消失，消息回退默认渲染。
 */

import { contributionRegistry, type PageDeclaration } from '@/services/schema/ContributionRegistry'
import { WebviewWidget } from '@/components/schema/widgets/WebviewWidget'

/** 消息 metadata 中携带样式 id 的键（消息卡声明契约） */
export const MESSAGE_STYLE_METADATA_KEY = 'message_style'

/** 声明 props 里指包内 HTML 路径的字段（对齐 WebviewWidget props 契约） */
interface MessageStyleProps {
  pluginId?: unknown
  htmlPath?: unknown
  widgetId?: unknown
}

/**
 * 按样式 id 解析消息卡声明与容器 props。
 *
 * 命中契约：chat 空间 slot=message-style 且 id 相等的页面声明；容器渲染坐标
 * props.pluginId ?? 声明归属插件、props.htmlPath（未声明 htmlPath 时 undefined，
 * WebviewWidget 以缺省 /webview 端点加载并在失败态显式报错，不静默）。
 * 无声明（未声明/禁用）返回 null——调用方回退默认渲染。
 */
export function resolveMessageStyle(
  styleId: unknown,
): { page: PageDeclaration; pluginId: string; htmlPath?: string; widgetId?: string } | null {
  if (typeof styleId !== 'string' || styleId === '') return null
  const page = contributionRegistry
    .getPagesBySpace('chat')
    .find((p) => p.slot === 'message-style' && p.id === styleId)
  if (!page) return null
  const props = (page.props ?? {}) as MessageStyleProps
  return {
    page,
    pluginId: typeof props.pluginId === 'string' && props.pluginId !== '' ? props.pluginId : (page.pluginId ?? ''),
    htmlPath: typeof props.htmlPath === 'string' ? props.htmlPath : undefined,
    widgetId: typeof props.widgetId === 'string' ? props.widgetId : undefined,
  }
}

export interface PluginMessageCardProps {
  /** 消息卡实例标识（下游 widget 事件订阅坐标；缺省由消息 id + 样式 id 合成） */
  instanceKey: string
  /** 样式 id（message.metadata.message_style） */
  styleId: string
  /**
   * 宿主数据注入（消息卡宿主桥）：消息的 content + metadata（含 compression_ref）
   * 原样下行给卡（web/cards/compression.html 输入契约 message.data 信封）。
   * 缺省不下发——通用 widget 卡自行经 widget.event 通道取数。
   */
  message?: { content: string; metadata?: Record<string, unknown> | null }
}

/** 消息卡容器：有界高度内嵌插件 webview（沙箱安全模型同 WebviewWidget） */
export function PluginMessageCard({ instanceKey, styleId, message }: PluginMessageCardProps) {
  const resolved = resolveMessageStyle(styleId)
  if (!resolved) return null
  return (
    <div
      className="border-border/40 h-64 w-full overflow-hidden rounded-xl border"
      data-testid="plugin-message-card"
      data-message-style={styleId}
    >
      <WebviewWidget
        pluginId={resolved.pluginId}
        htmlPath={resolved.htmlPath}
        widgetId={resolved.widgetId ?? `${instanceKey}:${styleId}`}
        title={resolved.page.title ?? styleId}
        injectMessage={message}
      />
    </div>
  )
}

export default PluginMessageCard
