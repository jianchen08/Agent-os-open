/**
 * 调试中心面板（debug_center 插件单入口 → 工作区面板）
 *
 * 侧边栏只暴露一个「调试」入口（插件声明 when: user.role == 'admin'，仅管理员可见），
 * 本面板内部切换 6 个调试页面：
 * 数据库管理 / 执行记录 / 会话 / 任务 / 用户 / 评估指标。
 * 各子页面以 embedded 模式渲染（PageShell 不渲染返回头，适配工作区面板；
 * 数据库管理页内部保留 admin 守卫，非 admin 打开时显示无权限提示）。
 */

import { useState } from 'react'
import { cn } from '@/lib/utils'
import { DbAdminPage } from '@/pages/debug/DbAdminPage'
import { DebugEvaluationMetricsPage } from '@/pages/debug/DebugEvaluationMetricsPage'
import { DebugExecutionRecordsPage } from '@/pages/debug/DebugExecutionRecordsPage'
import { DebugSessionsPage } from '@/pages/debug/DebugSessionsPage'
import { DebugTasksPage } from '@/pages/debug/DebugTasksPage'
import { DebugUsersPage } from '@/pages/debug/DebugUsersPage'
import { ContractStatusPanel } from '@/components/debug/ContractStatusPanel'

interface DebugPageItem {
  id: string
  title: string
  icon: string
  component: () => React.JSX.Element
}

/** 调试中心子页面清单（与 /debug/* 内置路由同组件，embedded 模式复用） */
const DEBUG_PAGES: DebugPageItem[] = [
  { id: 'db_admin', title: '数据库管理', icon: '🗄️', component: () => <DbAdminPage embedded /> },
  { id: 'execution_records', title: '执行记录', icon: '📋', component: () => <DebugExecutionRecordsPage embedded /> },
  { id: 'sessions', title: '会话', icon: '💬', component: () => <DebugSessionsPage embedded /> },
  { id: 'tasks', title: '任务', icon: '⚙️', component: () => <DebugTasksPage embedded /> },
  { id: 'users', title: '用户', icon: '👤', component: () => <DebugUsersPage embedded /> },
  { id: 'evaluation', title: '评估指标', icon: '📊', component: () => <DebugEvaluationMetricsPage embedded /> },
  { id: 'contract_status', title: '插件契约', icon: '🛡️', component: () => <ContractStatusPanel /> },
]

/** 调试中心面板组件（widget: debug_center_hub） */
export function DebugCenterHubWidget() {
  const [activeId, setActiveId] = useState(DEBUG_PAGES[0].id)
  const active = DEBUG_PAGES.find((p) => p.id === activeId) ?? DEBUG_PAGES[0]

  return (
    <div className="flex h-full min-h-0 flex-col">
      {/* 子页面切换栏 */}
      <div className="flex flex-wrap gap-1 border-b px-2 py-1.5">
        {DEBUG_PAGES.map((p) => (
          <button
            key={p.id}
            type="button"
            onClick={() => setActiveId(p.id)}
            data-testid={`debug-hub-tab-${p.id}`}
            className={cn(
              'rounded-md px-2.5 py-1 text-xs transition-colors',
              activeId === p.id ? 'bg-primary text-primary-foreground' : 'text-muted-foreground hover:bg-accent/50',
            )}
          >
            <span aria-hidden="true">{p.icon}</span> {p.title}
          </button>
        ))}
      </div>
      {/* 子页面内容 */}
      <div className="min-h-0 flex-1 overflow-auto">{active.component()}</div>
    </div>
  )
}

export default DebugCenterHubWidget
