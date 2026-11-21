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
 *
 * 同锚点并排（2026-11-17 用户裁定：任务现场入口放右上角与待办角标并列）：
 * 锚点归组渲染——一个锚点 = 一行 flex（右锚点右端对齐、左锚点左端对齐），
 * 组内元素依次排开不互相覆盖。宿主传入的 children 排在同锚点组首位（右锚点
 * 下即落在插件挂件左侧），宿主按钮与插件挂件的相对位置由本层统一排布，
 * 谁也不许猜对方宽度去写死偏移。
 */

import { type CSSProperties, type FC, type ReactNode, useSyncExternalStore } from 'react'
import { WebviewWidget } from '@/components/schema/widgets/WebviewWidget'
import { useIsMobileViewport } from '@/hooks/useIsMobileViewport'
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

/** 一条待渲染的悬浮件（声明解析结果 + 宿主按钮共用的行内元素模型） */
interface FloatingItem {
  key: string
  anchor: Anchor
  offset?: { x?: number; y?: number }
  pluginId: string
  htmlPath?: string
  widgetId: string
  title: string
  transparent: boolean
}

/** 宿主 children 默认并入的锚点：右上角（与待办角标同列） */
const HOST_ANCHOR: Anchor = 'top-right'

/**
 * 移动端 top-right 锚点的布局层浮钮让位（视觉 P0-1，MuMu 实测 shots/09）：
 * FiveSpaceLayout 的 workspace-expand-float 浮钮（工作区入口，right-1 + w-7）
 * 占右缘 32px，宿主胶囊缺省 offset 12px 时与其重叠（胶囊 z-20 压浮钮 z-10）。
 * 移动形态下 top-right 行整体右移 = 浮钮带宽 32px + 8px 最小间距；桌面无浮钮
 * 不让位（保持 12px 贴角）。
 */
const MOBILE_RIGHT_FLOAT_CLEARANCE = 40

const isAnchor = (v: unknown): v is Anchor =>
  v === 'top-right' || v === 'top-left' || v === 'bottom-right' || v === 'bottom-left'

function positionOf(
  anchor: Anchor,
  offset: { x?: number; y?: number } | undefined,
  isMobile: boolean,
): CSSProperties {
  const x = offset?.x ?? 12
  const y = offset?.y ?? 12
  const pos: CSSProperties = {}
  if (anchor.startsWith('top')) {
    // top 锚点按所在 DOM 坐标上下文避让安全区：语义=挂件距「安全区底缘」至少
    // y px（offset 是相对可用区顶的间距，与 FiveSpaceLayout 浮钮
    // calc(0.5rem + var(--safe-area-top)) 同语义，不是距屏幕物理顶的间距）。
    // 目标几何：containerTop + top >= safeTop + y
    //   => top = y + max(0px, safeTop - containerTop)
    // --floating-container-top = 本层包含块顶相对视口顶的偏移，由所在容器声明
    // （当前移动端全屏沉浸链 FiveSpaceLayout→ChatContainer 无垫层，包含块顶=
    // 视口顶，缺省 0 精确成立；未来带垫层宿主声明该变量即自动正确，悬浮层
    // 零改动）。声明式交给 CSS 引擎求值：旋转/键盘/滚动重排零监听遗漏；
    // --safe-area-top 是未注册自定义属性 JS 读不到像素值，故不走运行时测量
    // （若未来出现不便声明变量的宿主，再升级 getBoundingClientRect 探针方案）。
    // 桌面 safeTop=0 → max=0 → top=y 行为不变；负值被 max 钳住不倒吸。
    pos.top = `calc(${y}px + max(0px, var(--safe-area-top, 0px) - var(--floating-container-top, 0px)))`
  } else {
    pos.bottom = y
  }
  // top-right 锚点移动形态让位布局层浮钮（P0-1，常量注释见定义处）；
  // bottom-right 无浮钮共存，保持声明值
  if (anchor.endsWith('right')) {
    pos.right = anchor === 'top-right' && isMobile ? x + MOBILE_RIGHT_FLOAT_CLEARANCE : x
  } else {
    pos.left = x
  }
  return pos
}

export interface ChatFloatingLayerProps {
  /** 宿主自带的悬浮控件（本层只负责排位，不解释语义）；缺省零渲染 */
  children?: ReactNode
}

export const ChatFloatingLayer: FC<ChatFloatingLayerProps> = ({ children }) => {
  const isMobile = useIsMobileViewport()
  // registry 响应化订阅：悬浮声明来自 /api/v1/schema 异步装载（bootstrap 完成
  // 晚于本组件挂载），resync 重同步/插件热重载亦经 bumpVersion 通知——缺订阅
  // 则首渲染（空 registry → 零渲染）冻结到下一次无关重渲染才带过，悬浮件表现
  // 为"偶尔才出现"（2026-11-22 todo 角标间歇性不显示根修）。version 快照本身
  // 不消费，订阅仅驱动重渲染后重读 getPagesBySpace（与 DeclaredWidgetLayer/
  // SlotHost 同款模式）。
  useSyncExternalStore(contributionRegistry.subscribe, contributionRegistry.getVersion)
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
        anchor: props.anchor,
        offset,
        pluginId,
        htmlPath: typeof props.htmlPath === 'string' ? props.htmlPath : undefined,
        widgetId:
          typeof props.widgetId === 'string' && props.widgetId !== ''
            ? props.widgetId
            : `float:${pluginId}:${page.id}`,
        title: page.title ?? page.id,
        transparent: props.transparent === true,
      } satisfies FloatingItem,
    ]
  })

  // 锚点归组（Map 保序 = 声明序）：同锚点共用一行，组内偏移取该组首件的声明值
  const groups = new Map<Anchor, FloatingItem[]>()
  for (const item of items) {
    const bucket = groups.get(item.anchor)
    if (bucket) bucket.push(item)
    else groups.set(item.anchor, [item])
  }
  // 只有宿主按钮、该锚点无插件声明时也要成组（否则按钮无处可落）
  if (children && !groups.has(HOST_ANCHOR)) groups.set(HOST_ANCHOR, [])

  if (groups.size === 0) return null

  return (
    <div className="pointer-events-none absolute inset-0 z-20" data-testid="chat-floating-layer">
      {[...groups].map(([anchor, groupItems]) => (
        <div
          key={anchor}
          className="pointer-events-auto absolute flex items-center gap-2"
          // 锚点定位值来自插件声明的动态 offset（任意 px），类名承载不了——
          // 必须内联 style（positionOf 纯计算，无状态副作用）
          // eslint-disable-next-line no-restricted-syntax
          style={positionOf(anchor, groupItems[0]?.offset, isMobile)}
          data-testid={`chat-floating-group-${anchor}`}
        >
          {anchor === HOST_ANCHOR && children}
          {groupItems.map((item) => (
            <div key={item.key} data-testid="chat-floating-item">
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
