/**
 * PageShell 统一页面外壳
 *
 * 提供标准化的页面布局：header（返回 + 标题 + 描述 + 操作区）+ 可滚动内容区，
 * 内置 loading/error/empty 三态插槽。替代各页面重复的手写外壳。
 *
 * - 返回用 react-router Link（SPA 导航，不整页刷新——修统一审查 N1）
 * - 三态：loading → LoadingState；error → ErrorState 或自定义节点；empty → 自定义节点
 * - embedded：嵌套于 settings 标签页等场景，不渲染自身 header/back
 * - density：comfortable（页密度，header 40px）/ compact（壳密度，header 36px）
 *
 * 关联：docs/working/design/frontend-design-unification-execution-plan.md §四 M1.1
 */

import { Link } from 'react-router-dom'
import { ErrorState } from './ErrorState'
import { LoadingState } from './LoadingState'
import type { ReactNode } from 'react'

type Density = 'comfortable' | 'compact'

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
  /** 加载态：为 true 时渲染骨架占位，不渲染 children。
   *  适用纯内容页（整页只有 loading/error/empty/data 四态）。
   *  含常驻控件（搜索栏/统计卡）的页面应在 children 内用 inline LoadingState/ErrorState/EmptyState，
   *  使常驻区在三态下仍可见——这类页面不用本 slot。 */
  loading?: boolean
  /** 自定义骨架内容（loading 为 true 时优先于此） */
  skeleton?: ReactNode
  /** 错误态：字符串走 ErrorState，或传自定义节点。同 loading 的适用说明。 */
  error?: ReactNode
  /** 空态节点。同 loading 的适用说明。 */
  empty?: ReactNode
  /** 是否为空数据（触发 empty 渲染） */
  isEmpty?: boolean
  /** 嵌入模式：不渲染 header/back，用于 settings 子页等 */
  embedded?: boolean
  /** 密度档位，默认 'comfortable'（页密度） */
  density?: Density
  /** main 区域的可访问名（读屏区分各页面/表单区域），如 "API 配置表单" */
  mainLabel?: string
  /** 页面内容 */
  children?: ReactNode
}

const DENSITY_HEADER: Record<Density, string> = {
  comfortable: 'h-10 px-6',
  compact: 'h-9 px-4',
}

/**
 * 统一页面外壳组件
 *
 * 三态优先级：loading > error > empty > children（任一前置态命中即不渲染 children）。
 */
export function PageShell({
  title,
  description,
  backHref = '/',
  backLabel = '返回',
  actions,
  maxWidth,
  loading,
  skeleton,
  error,
  empty,
  isEmpty,
  embedded = false,
  density = 'comfortable',
  mainLabel,
  children,
}: PageShellProps) {
  const body = (
    <>
      {loading
        ? (skeleton ?? <LoadingState variant="skeleton" />)
        : error
          ? (typeof error === 'string' ? <ErrorState message={error} /> : error)
          : isEmpty && empty
            ? empty
            : children}
    </>
  )

  return (
    <div className="bg-background text-foreground flex h-screen flex-col overflow-hidden">
      {!embedded ? (
        <header
          className={`flex ${DENSITY_HEADER[density]} shrink-0 items-center gap-3 border-b`}
        >
          <Link
            to={backHref}
            className="text-muted-foreground hover:text-foreground text-sm"
          >
            ← {backLabel}
          </Link>
          <h1 className="text-base font-semibold">{title}</h1>
          {description && (
            <span className="text-muted-foreground text-xs">{description}</span>
          )}
          {actions && <div className="ml-auto flex items-center gap-2">{actions}</div>}
        </header>
      ) : (
        // embedded 无自身 header，但 actions（如清空/刷新等页面级操作）不能随
        // header 一起消失——有 actions 时渲染轻量右对齐工具行，无 actions 零渲染
        actions && (
          <div
            className={`flex ${DENSITY_HEADER[density]} shrink-0 items-center justify-end gap-2 border-b px-4`}
          >
            {actions}
          </div>
        )
      )}
      <main
        aria-label={mainLabel}
        className={`flex-1 space-y-4 overflow-y-auto p-6${maxWidth ? ` ${maxWidth}` : ''}`}
      >
        {body}
      </main>
    </div>
  )
}
