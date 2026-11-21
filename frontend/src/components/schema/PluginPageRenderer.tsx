/**
 * PluginPageRenderer — 插件 page 独立路由渲染器（阶段2 遗留 — react-router 路由动态化）
 *
 * 用途：让插件 page（contributionRegistry.pages 中声明了 path 的项）能作为
 * **真实 URL 路由**被访问（可分享/刷新/浏览器前进后退），与 workspacePanelOpener
 * 的"伪路由"（工作区 Tab）机制并存。
 *
 * 方案：react-router v7 的 createBrowserRouter 创建后无法动态加路由，故采用
 * **通配路由 `/p/:pageId`** ——
 * - router.tsx 静态路由数组中插入 `{ path: '/p/:pageId', element: <PluginPageRenderer/> }`
 * - 本组件用 useParams 取 pageId → contributionRegistry.getPage(pageId) → renderPageContent
 * - 插件 page 声明 path 时用 `/p/<pageId>` 形式即可命中（无需运行时加路由）
 *
 * schema 异步加载时序：直接 URL 访问时，GrowthLoop 的 schema 异步拉取可能尚未完成，
 * getPage 返回 undefined。此时显示 loading，用 useEffect 轮询重试（schema 加载完后自动渲染），
 * 而非立即 404（避免"刷新后插件页消失"的体验）。
 *
 * 渲染逻辑全部委托 renderPageContent（与 PageRenderer 共用，widget/schema/dock 分发一致）。
 */

import { useEffect, useState } from 'react'
import { useParams } from 'react-router-dom'
import { contributionRegistry } from '@/services/schema/ContributionRegistry'
import { renderPageContent } from './PageRenderer'
import type { ReactNode } from 'react'

/** 404 占位：pageId 在 contributionRegistry 中未注册（且 schema 已加载完） */
function NotFound({ pageId }: { pageId: string }): ReactNode {
  return (
    <div
      data-testid="plugin-page-not-found"
      className="text-muted-foreground flex min-h-screen flex-col items-center justify-center gap-2 p-8 text-center"
    >
      <div className="text-2xl font-bold">404</div>
      <div className="text-sm">
        插件页面 <code className="bg-muted px-1.5 py-0.5 rounded text-xs">{pageId}</code> 不存在
      </div>
    </div>
  )
}

/** Loading 占位：schema 异步加载中 */
function Loading(): ReactNode {
  return (
    <div
      data-testid="plugin-page-loading"
      className="text-muted-foreground flex min-h-screen items-center justify-center text-sm"
    >
      正在加载插件页面...
    </div>
  )
}

/**
 * 插件 page 独立路由渲染器
 *
 * 从 URL `/p/:pageId` 取 pageId，到 contributionRegistry 查 page，委托 renderPageContent
 * 整页渲染（widget/schema/dock 分发）。
 *
 * schema 加载时序处理：如果 registry 未初始化（schema 异步拉取未完成），显示 loading 并
 * 轮询重试（每 300ms，最多 5s）；schema 加载完后自动渲染 page。超过 5s 仍未找到 → 404。
 */
export function PluginPageRenderer(): ReactNode {
  const params = useParams<{ pageId: string }>()
  const pageId = params.pageId ?? ''
  // tick 只用于强制重渲染；expired 记录轮询窗口是否耗尽（决定 404）
  const [tick, setTick] = useState(0)
  const [expired, setExpired] = useState(false)

  // 页面未就绪时轮询重试（每 300ms 触发重渲染，最多 ~5s = 16 次）。
  // 用 interval 而非逐次 setTimeout：页面可能被无关状态频繁重渲染，逐次
  // setTimeout 会被 effect 清理反复重置导致永远等不到 tick 推进。
  // 不按 isInitialized 提前收口：页面除 schema 声明外还可能是**schema 之后**
  // 动态注册的（DSH 槽位页按已装插件清单注册），schema 就绪不代表页面集合终态。
  useEffect(() => {
    if (contributionRegistry.getPage(pageId)) return
    setExpired(false)
    let attempts = 0
    const timer = setInterval(() => {
      attempts += 1
      if (contributionRegistry.getPage(pageId)) {
        clearInterval(timer)
        setTick((t) => t + 1)
        return
      }
      if (attempts >= 16) {
        clearInterval(timer)
        setExpired(true)
        return
      }
      setTick((t) => t + 1)
    }, 300)
    return () => clearInterval(timer)
  }, [pageId])

  const page = contributionRegistry.getPage(pageId)
  void tick

  // page 找到 → 渲染
  if (page) {
    return (
      <div data-testid="plugin-page-root" className="min-h-screen w-full">
        {renderPageContent(page)}
      </div>
    )
  }

  // 轮询窗口内 → loading（动态注册页可能在 schema 之后到达）
  if (!expired) {
    return <Loading />
  }

  // 轮询耗尽仍未找到 → 真正的 404
  return <NotFound pageId={pageId} />
}

export default PluginPageRenderer
