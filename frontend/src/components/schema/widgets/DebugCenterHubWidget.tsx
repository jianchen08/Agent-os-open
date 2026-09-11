/**
 * 调试中心面板（debug_center 插件单入口 → 工作区面板）
 *
 * 侧边栏只暴露一个「调试」入口（插件声明 when: user.role == 'admin'，仅管理员可见）。
 * 子页清单由 debug_center 插件 contributes.pages（space=debug_center, slot=tab）声明
 * 驱动：本面板按声明渲染 tab 栏 + 内容（renderPageContent widget 分支解析注册名），
 * 插件增删子页无需动前端。子页组件是预置 widget（embedded 模式，PageShell 不渲染
 * 返回头，适配工作区面板；数据库管理页内部保留 admin 守卫，非 admin 打开时显示
 * 无权限提示）。
 */

import { useEffect, useMemo, useState } from 'react'
import { renderPageContent } from '@/components/schema/PageRenderer'
import { cn } from '@/lib/utils'
import { contributionRegistry } from '@/services/schema/ContributionRegistry'

/** 调试中心面板组件（widget: debug_center_hub） */
export function DebugCenterHubWidget() {
  // 声明消费与侧栏同模式：registry 由 GrowthLoop 全局装载（登录后初始化 +
  // schema_updated 事件刷新），此处轮询条目数捕获迟到的装载结果
  const [contribTick, setContribTick] = useState(0)
  useEffect(() => {
    const id = window.setInterval(() => {
      const n = contributionRegistry.getPagesBySpace('debug_center').length
      setContribTick((prev) => (prev === n ? prev : n))
    }, 1500)
    return () => window.clearInterval(id)
  }, [])

  const subPages = useMemo(() => {
    void contribTick
    return contributionRegistry
      .getPagesBySpace('debug_center')
      .filter((p) => p.slot === 'tab')
      .slice()
      .sort((a, b) => (a.order ?? 50) - (b.order ?? 50))
  }, [contribTick])

  const [activeId, setActiveId] = useState<string | null>(null)
  const active = subPages.find((p) => p.id === activeId) ?? subPages[0]

  return (
    <div className="flex h-full min-h-0 flex-col">
      {/* 子页面切换栏 */}
      <div className="flex flex-wrap gap-1 border-b px-2 py-1.5">
        {subPages.map((p) => (
          <button
            key={p.id}
            type="button"
            onClick={() => setActiveId(p.id)}
            data-testid={`debug-hub-tab-${p.id}`}
            className={cn(
              'rounded-md px-2.5 py-1 text-xs transition-colors',
              active?.id === p.id ? 'bg-primary text-primary-foreground' : 'text-muted-foreground hover:bg-accent/50',
            )}
          >
            {p.icon ? <span aria-hidden="true">{p.icon}</span> : null} {p.title ?? p.id}
          </button>
        ))}
      </div>
      {/* 子页面内容（声明页经 PageRenderer widget 分支解析预置注册名） */}
      <div className="min-h-0 flex-1 overflow-auto">
        {active ? (
          renderPageContent(active)
        ) : (
          <div className="text-muted-foreground flex h-full items-center justify-center p-4 text-sm">
            暂无声明子页（debug_center 插件 space=debug_center 页声明为空或未装载）
          </div>
        )}
      </div>
    </div>
  )
}

export default DebugCenterHubWidget
