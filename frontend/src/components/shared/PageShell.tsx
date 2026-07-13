/**
 * PageShell 统一页面外壳
 *
 * 提供标准化的页面布局结构：header（返回按钮 + 标题 + 描述 + 右侧操作区）+ 可滚动内容区。
 * 替代各页面中重复的页面外壳实现。
 */

import type { ReactNode } from 'react'

/** PageShell 组件属性 */
interface PageShellProps {
  /** 页面标题 */
  title: string
  /** 标题旁的描述文字 */
  description?: string
  /** 返回链接地址，默认 '/' */
  backHref?: string
  /** 返回按钮文字，默认 '返回' */
  backLabel?: string
  /** header 右侧操作区 */
  actions?: ReactNode
  /** 内容区最大宽度 CSS 类名，如 'max-w-3xl'，默认无限制 */
  maxWidth?: string
  /** 页面内容 */
  children: ReactNode
}

/**
 * 统一页面外壳组件
 *
 * 包含标准化的 header（h-12, border-b）和可滚动的 main 内容区。
 * 遵循项目中 AgentsPage、AdminPage、ApiSettingsPage 等页面的共同布局模式。
 */
export function PageShell({
  title,
  description,
  backHref = '/',
  backLabel = '返回',
  actions,
  maxWidth,
  children,
}: PageShellProps) {
  return (
    <div className="bg-background text-foreground flex h-screen flex-col overflow-hidden">
      <header className="flex h-12 shrink-0 items-center border-b px-4">
        <a
          href={backHref}
          className="text-muted-foreground hover:text-foreground text-sm"
        >
          ← {backLabel}
        </a>
        <h1 className="ml-4 text-base font-semibold">{title}</h1>
        {description && (
          <span className="text-muted-foreground ml-2 text-xs">{description}</span>
        )}
        {actions && <div className="ml-auto flex items-center gap-2">{actions}</div>}
      </header>
      <main
        className={`flex-1 space-y-4 overflow-y-auto p-6${maxWidth ? ` ${maxWidth}` : ''}`}
      >
        {children}
      </main>
    </div>
  )
}
