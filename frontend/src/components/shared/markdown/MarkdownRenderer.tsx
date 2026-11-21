/**
 * Markdown 渲染组件
 *
 * 使用 Streamdown 实现 Markdown 渲染
 * Streamdown 本身支持流式渲染和不完整 Markdown 自动补全
 *
 * 双链：经 wikilinkAdapter 挂载 remark-obsidian（strict preset，最小语法增量只开
 * wikilinks）解析 `[[目标]]`/`[[目标|别名]]`，桥接为 a[href^="/wiki/"]；components.a
 * 组合覆写（streamdown 官方模式）：/wiki/ 前缀渲染真实 <a>，其余转发内置 MarkdownA——
 * linkSafety 默认启用时内置组件把链接渲染成安全按钮（外链现有行为，零变化），双链是
 * 内部跳转无需外链安全弹窗。容器级点击拦截解码后委托 fileOpener.openFile 打开工作区
 * 页签（任务工作区优先、_local 项目根回退），不发生整窗导航。
 */

import { memo, useCallback, type ComponentPropsWithoutRef, type FC, type MouseEvent } from 'react'
import {
  defaultComponents,
  defaultRemarkPlugins,
  type Components,
  type ExtraProps,
  Streamdown,
} from 'streamdown'
import { cn } from '@/lib/utils'
import { reportError } from '@/services/errorReporting'
import { openFile } from '@/services/fileOpener'
import { resolveDocIdPath } from './backlinksProjection'
import { markdownMemoComparator } from './shared'
import { parseWikilinkTarget, WIKI_URL_PREFIX, wikilinkRemarkPlugins } from './wikilinkAdapter'
import type { PluggableList } from 'unified'

export interface MarkdownRendererProps {
  /** Markdown 内容 */
  content: string
  /** 是否正在流式输出 */
  isStreaming?: boolean
  /** 自定义类名 */
  className?: string
}

type AnchorProps = ComponentPropsWithoutRef<'a'> & ExtraProps

/**
 * 双链锚点（components.a 组合覆写）：/wiki/ 前缀 = 内部文档跳转，渲染真实 <a>
 * （href 供中键/复制语义，点击走容器级拦截）；其余转发内置 MarkdownA，外链
 * linkSafety 安全按钮行为零变化。
 */
function MarkdownAnchor({ node, href, children, ...rest }: AnchorProps) {
  if (href && href.startsWith(WIKI_URL_PREFIX)) {
    return (
      <a
        {...rest}
        href={href}
        data-wikilink="true"
        className={cn('text-primary underline', rest.className)}
      >
        {children}
      </a>
    )
  }
  const BuiltIn = defaultComponents.a
  return (
    <BuiltIn href={href} node={node} {...rest}>
      {children}
    </BuiltIn>
  )
}

/** components 覆写：模块级稳定对象（组件身份稳定，避免流式重渲染整树重挂） */
const WIKI_COMPONENTS: Components = { a: MarkdownAnchor }

/** /wiki/ 目标打开（模块级 helper）：doc_id 形态先经反链索引反查真实路径
 *  （索引驱动，渲染层不发明目录约定——审查 MF-1 修复面）；打开失败经 reportError
 *  通知中心如实上报（openFile 不自行通知——fileOpener 契约，main.tsx 同口径，
 *  审查 MF-3 修复面）。 */
async function openWikiTarget(target: string): Promise<void> {
  const parsed = parseWikilinkTarget(target)
  if (parsed?.isDocId) {
    let resolved: string | null
    try {
      resolved = await resolveDocIdPath(parsed.wikiPath)
    } catch (err) {
      reportError(err instanceof Error ? err.message : '反向链接索引不可用', {
        component: 'MarkdownRenderer',
        action: 'openWikiTarget',
        source: 'frontend',
      })
      return
    }
    if (!resolved) {
      reportError(`双链目标未收录：${parsed.wikiPath}（悬空引用或索引过期）`, {
        component: 'MarkdownRenderer',
        action: 'openWikiTarget',
        source: 'frontend',
      })
      return
    }
    target = resolved
  }
  const result = await openFile(target)
  if (!result.success) {
    reportError(result.message || '打开文件失败', {
      component: 'MarkdownRenderer',
      action: 'openWikiTarget',
      source: 'frontend',
    })
  }
}

/** 挂载链（模块级稳定）：streamdown 的 remarkPlugins 为替换语义（dist：用户传入即
 *  整体取代默认链 gfm+codeMeta——实测传自定义链后表格退化为纯文本），故必须自带
 *  默认件再追加双链；obsidian 在 gfm 之后跑（README 推荐顺序），bridge 在其后消费
 *  wikilink 节点。 */
const REMARK_PLUGINS: PluggableList = [
  ...(Object.values(defaultRemarkPlugins) as PluggableList),
  ...wikilinkRemarkPlugins,
]

/**
 * Markdown 渲染组件
 *
 * 统一渲染器：流式和非流式使用完全相同的渲染逻辑
 */
export const MarkdownRenderer: FC<MarkdownRendererProps> = memo(
  ({ content, isStreaming = false, className }) => {
    /** 容器级 /wiki/ 锚点点击拦截：preventDefault 后委托 openWikiTarget
     *  （解码目标原文，doc_id 经反链索引反查后打开，失败上报通知中心） */
    const handleContainerClick = useCallback((event: MouseEvent<HTMLDivElement>) => {
      const anchor = (event.target as HTMLElement | null)?.closest?.(
        'a[href^="/wiki/"]',
      ) as HTMLAnchorElement | null
      if (!anchor) return
      event.preventDefault()
      const encoded = anchor.getAttribute('href')?.slice(WIKI_URL_PREFIX.length) ?? ''
      if (!encoded) return
      void openWikiTarget(decodeURIComponent(encoded))
    }, [])

    return (
      <div
        className={cn('markdown-content', className)}
        data-testid="message-markdown"
        onClick={handleContainerClick}
      >
        <Streamdown
          mode="static"
          parseIncompleteMarkdown={true}
          // 0 = 关闭 Streamdown 内嵌限高（默认 code 400px / table 300px +
          // overflow-y-auto）：那会在消息流里制造嵌套竖向滚动容器，滚轮悬停其上
          // 被内层吃掉（页面不动，"代码块上滚不动"根因），流式重解析时内层
          // scrollTop 重置即窗口内闪烁。关闭后消息列表是唯一竖向滚动面。
          codeBlockMaxHeight={0}
          tableMaxHeight={0}
          remarkPlugins={REMARK_PLUGINS}
          components={WIKI_COMPONENTS}
        >
          {content}
        </Streamdown>
        {isStreaming && <span className="md-cursor" />}
      </div>
    )
  },
  markdownMemoComparator,
)

MarkdownRenderer.displayName = 'MarkdownRenderer'
