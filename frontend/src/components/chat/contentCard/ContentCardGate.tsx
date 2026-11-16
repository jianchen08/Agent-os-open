/**
 * ContentCardGate · markdown pre 门（content_cards 分发单点，ADR
 * 2026-10-14-content-card-registry §3）
 *
 * 类型零知识：pre(children=code 元素) → 提取 fence language → 内容卡注册表查表
 * （matchContentCard）→ 未命中 / 流式门未过 → 回投 lobehub 默认 CodeBlock（其
 * 默认 memoPre 的同参复刻，现状渲染保真锚）→ 命中 → ContentCardRenderer。
 * 分发路径无任何卡型 if——html/csv 内置首选卡与插件卡走完全同一查表路径。
 *
 * 接入点 = 覆写 `components.pre`（lobehub CodeBlock 吞掉 code 子元素直读渲染，
 * components.code 覆写无效——ADR Alternatives A 实测事实）；消费方
 * LobeChatMarkdown 以闭包携带 isStreaming（流式门，消息级粒度 §7）。
 */

import { CodeBlock } from '@lobehub/ui/es/Markdown/components/CodeBlock'
import { useMarkdownContext } from '@lobehub/ui/es/Markdown/components/MarkdownProvider'
import { type ReactNode } from 'react'
import {
  extractFenceLanguage,
  extractTaskListText,
  matchContentCard,
  matchContentCardForBlock,
} from '@/services/schema/contentCard'
import { contributionRegistry } from '@/services/schema/ContributionRegistry'
import { ContentCardRenderer } from './ContentCardRenderer'

export interface ContentCardGateProps {
  /** react-markdown 注入的 hast 节点（不透传给 CodeBlock——memoPre 同款剥离） */
  node?: unknown
  /** 流式门（消息级粒度）：流式中命中也不出卡，保持源码态（§7） */
  isStreaming?: boolean
  children?: ReactNode
  [key: string]: unknown
}

/** pre children → code 元素 props（className/内容文本；lobehub useCode 同构提取） */
export function extractCodeElement(children: ReactNode): { className?: string; text: string } | null {
  const el = (Array.isArray(children) ? children[0] : children) as
    | { props?: { className?: unknown; children?: unknown } }
    | null
  if (!el || typeof el !== 'object' || !el.props) return null
  const raw = el.props.children
  const text =
    typeof raw === 'string'
      ? raw
      : Array.isArray(raw) && typeof raw[0] === 'string'
        ? (raw[0] as string)
        : ''
  return {
    className: typeof el.props.className === 'string' ? el.props.className : undefined,
    text,
  }
}

/** pre 门组件（LobeChatMarkdown components.pre 覆写消费） */
export function ContentCardGate({ node: _node, isStreaming = false, children, ...rest }: ContentCardGateProps) {
  const ctx = useMarkdownContext()
  // 未命中回投（默认 memoPre 同参复刻）：ctx 装配（highlight/html/mermaid 主题
  // 三键 + componentProps.pre 覆盖）+ pre 元素自身 props 透传
  const preComponentProps = ctx.componentProps
  const defaultPre = (
    <CodeBlock
      animated={ctx.animated}
      enableHtmlPreview={ctx.enableHtmlPreview}
      enableMermaid={ctx.enableMermaid}
      fullFeatured={ctx.fullFeaturedCodeBlock}
      {...(preComponentProps?.highlight ? { highlight: preComponentProps.highlight } : {})}
      {...(preComponentProps?.html ? { html: preComponentProps.html } : {})}
      {...(preComponentProps?.mermaid ? { mermaid: preComponentProps.mermaid } : {})}
      {...preComponentProps?.pre}
      {...rest}
    >
      {children}
    </CodeBlock>
  )
  const codeEl = extractCodeElement(children)
  const fence = extractFenceLanguage(codeEl?.className)
  // 注册表非响应式读（ADR §8）：插件热注册新卡随消息自然重渲染周期生效；
  // 围栏内容原文参与特征判定（声明 fence_content_pattern，2026-10-25 裸
  // HTML 任务卡根修——仅凭 language 会把普通 html 代码块一并劫持）
  const card = matchContentCard(
    contributionRegistry.getAllContentCards(),
    fence,
    codeEl?.text,
  )
  if (!card || isStreaming || !codeEl) return defaultPre
  return <ContentCardRenderer card={card} content={codeEl.text} defaultView={defaultPre} />
}


/**
 * TaskListGate · markdown task-list 块的声明路由（2026-10-24 用户裁定：
 * "聊天框里的单"（待办清单等）渲染归业务插件声明，前端只提供通用消费机制）。
 *
 * ul(children=li 数组) → 声明表查 match.blocks 含 'task-list' 的内容卡 →
 * 命中：extractTaskListText 提取清单行文本交 ContentCardRenderer（form=
 * template/webview 由声明插件决定，本 gate 零业务知识）；未命中：默认 ul
 * 原样渲染。GFM 语法产物判定 = 任一 li 首子元素为 input[type=checkbox]。
 *
 */
export function TaskListGate(props: React.ComponentProps<'ul'>) {
  const { children, ...rest } = props
  const items = Array.isArray(children) ? children : [children]
  const isTaskList = items.some((li) => {
    if (typeof li !== 'object' || li === null || !('props' in li)) return false
    const kids = (li as { props?: { children?: unknown } }).props?.children
    const arr = Array.isArray(kids) ? kids : [kids]
    return arr.some(
      (c) =>
        typeof c === 'object' && c !== null && 'props' in c &&
        (c as { props?: { type?: string } }).props?.type === 'checkbox',
    )
  })
  const card = isTaskList
    ? matchContentCardForBlock(contributionRegistry.getAllContentCards(), 'task-list')
    : null
  const listText = card ? extractTaskListText(children) : ''

  const defaultView = <ul {...rest}>{children}</ul>
  if (!isTaskList || !card) return defaultView
  return <ContentCardRenderer card={card} content={listText} defaultView={defaultView} />
}
