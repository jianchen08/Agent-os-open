/** LobeChat Markdown 渲染组件 使用 @lobehub/ui 的 Markdown 组件，专为 AI 聊天设计 */

// @lobehub/ui 子路径 import（必须带 /index 后缀，es/Markdown 形式会
// ERR_PACKAGE_PATH_NOT_EXPORTED）：把 lobehub 从整体 vendor 入口拆为按组件消费，
// 避免主入口把未用组件链拉进首屏 chunk。
import ConfigProvider from '@lobehub/ui/es/ConfigProvider/index'
import Markdown from '@lobehub/ui/es/Markdown/index'
import { motion } from 'motion/react'
import {
  createContext,
  useContext,
  useMemo,
  type ComponentProps,
  type FC,
  type MouseEvent,
  type ReactNode,
} from 'react'
import { AttachmentImage } from '@/components/shared/markdown/AttachmentImage'
import { markdownUrlTransform, preprocessSvgCodeBlocks } from '@/components/shared/markdown/shared'
import { ContentCardGate, TaskListGate } from './contentCard/ContentCardGate'
import './LobeChatMarkdown.css'

/**
 * 流式门上下文：pre 覆写必须是模块级稳定组件身份。
 *
 * react-markdown 按组件类型（引用）做元素协调，components.pre 若是每次渲染新造的
 * 内联箭头函数，流式 token 到达即整棵代码块重挂：@lobehub/ui useHighlight 的高亮
 * 状态随卸载清零，新内容（新缓存键）还要走异步高亮——每 token 一帧无高亮纯文本
 * fallback，代码块肉眼可见闪烁。isStreaming 因此改由 context 下发。
 */
const MarkdownStreamingContext = createContext(false)

/** 消息流 pre 门（稳定身份）：isStreaming 从 context 取，内容卡分发逻辑仍归 ContentCardGate */
const MarkdownPreGate: FC<ComponentProps<'pre'>> = (preProps) => {
  const isStreaming = useContext(MarkdownStreamingContext)
  return <ContentCardGate {...preProps} isStreaming={isStreaming} />
}

/**
 * react-markdown 覆写参数（模块级稳定对象）：@lobehub/ui 未传 urlTransform，
 * 默认实现会把 preprocessSvgCodeBlocks 产出的 data URI 图片清空 → 消息流 SVG
 * 代码块闭合即消失。放行策略见 shared.markdownUrlTransform。
 */
const MARKDOWN_REACT_PROPS = { urlTransform: markdownUrlTransform }

interface LobeChatMarkdownProps {
  content: string
  isStreaming?: boolean
  onDoubleClick?: () => void
  /**
   * 链接点击拦截（通用契约，组件对链接策略零知识）：返回 true = 已处理，
   * 组件阻止默认导航（防止相对路径链接把整窗导航进裸资源页）；
   * 返回 false / 未传 = 默认行为。策略消费方见 fileLoaderRegistry.markdownLinkInterceptor。
   */
  onLinkClick?: (link: { href: string; text: string }) => boolean
  children?: ReactNode
}

/** LobeChat Markdown 渲染组件 */
export const LobeChatMarkdown: FC<LobeChatMarkdownProps> = ({
  content,
  isStreaming = false,
  onDoubleClick,
  onLinkClick,
  children,
}) => {
  const processedContent = useMemo(
    () => preprocessSvgCodeBlocks(content),
    [content],
  )

  // components 只在流式门翻转时换新（pre 恒为模块级 MarkdownPreGate）：
  // 内容变化不改变组件身份，代码块不重挂
  const markdownComponents = useMemo(
    () => ({
      img: AttachmentImage,
      pre: MarkdownPreGate,
      // task-list 块声明路由（2026-10-24 用户裁定：渲染归插件声明，
      // 本组件只接通用 gate，零业务知识）；流式原样。
      // 流式必须整键缺席而非值 undefined：react-markdown v10 组件解析
      // 按键存在性取值（hast-util-to-jsx-runtime own.call 命中即用值），
      // undefined 值会渲染成 undefined 组件 → React 错误码 130 整页崩
      // （2026-10-25 装机实证：流式输出含列表消息必崩）。
      ...(isStreaming ? {} : { ul: TaskListGate }),
    }),
    [isStreaming],
  )

  /** 容器级链接点击委托：命中 onLinkClick 即阻止默认导航 */
  const handleContainerClick = (e: MouseEvent<HTMLDivElement>) => {
    if (!onLinkClick) return
    const anchor = (e.target as HTMLElement).closest('a')
    if (!anchor) return
    const href = anchor.getAttribute('href')
    if (!href) return
    if (onLinkClick({ href, text: anchor.textContent ?? '' })) {
      e.preventDefault()
    }
  }

  // 深色主题适配说明（@lobehub/ui 5.32.2）：
  // Markdown 内部的 Shiki 高亮用内置 "lobe-theme"，其颜色全部引用 antd-style 的
  // --ant-color-* CSS 变量（如 --ant-color-bg-container），随作用域内变量值自适应明暗。
  // 本项目刻意不挂 lobehub 的 ThemeProvider（避免注入全局 antd 主题/样式），
  // 明暗跟随由 LobeChatMarkdown.css 中 .lobe-chat-isolated 的 token 桥接实现——
  // 把 lobehub 用到的 --ant-color-* 重映射到项目主题变量（深色时 --foreground 为亮色）。
  // 注意：ConfigProvider 在该版本只负责 i18n/CDN/motion，类型与实现均无 appearance
  // 通道（appearance prop 已废弃，传入会被静默丢弃），不要再往上传主题明暗。

  return (
    <ConfigProvider motion={motion}>
      <div className="lobe-chat-isolated" data-testid="message-markdown" onDoubleClick={onDoubleClick} onClick={handleContainerClick}>
        {children ?? (
          // 流式门下发经 context（不是闭包）：pre 组件身份恒定，代码块不随 token 重挂
          <MarkdownStreamingContext.Provider value={isStreaming}>
            <Markdown
              variant="chat"
              enableStream={false}
              enableMermaid={true}
              reactMarkdownProps={MARKDOWN_REACT_PROPS}
              // 附件图片引用（![f](/uploads/x.png)）加载失败 → 显式失败卡
              // （2026-09-08 附件解析失败显式化：前端要说明，禁无文字占位图）
              // pre 覆写 = content_cards 分发门（ADR 2026-10-14-content-card-registry）：
              // 按 fence language 查表换渲染，未命中/流式中回投默认代码块（现状原样）
              components={markdownComponents}
            >
              {processedContent}
            </Markdown>
          </MarkdownStreamingContext.Provider>
        )}
        {isStreaming && <span className="md-cursor" />}
      </div>
    </ConfigProvider>
  )
}
