/**
 * Dashboard 仪表板测试页面
 *
 * 展示一个精美的深色主题仪表板界面，包含统计卡片、图表、活动列表和快速操作面板。
 * 所有组件自包含在此文件中，不依赖外部组件库。
 */

import { useState } from 'react'
import {
  Users,
  Activity,
  CheckCircle2,
  Cpu,
  Search,
  Bell,
  Settings,
  Plus,
  Upload,
  Download,
  RefreshCw,
  TrendingUp,
  TrendingDown,
  Clock,
  ArrowRight,
  Zap,
  BarChart3,
} from 'lucide-react'

// ---------------------------------------------------------------------------
// 类型定义
// ---------------------------------------------------------------------------

/** 统计卡片数据项 */
interface StatCard {
  id: string
  label: string
  value: string
  change: string
  trend: 'up' | 'down'
  icon: React.ReactNode
  color: string
}

/** 活动记录数据项 */
interface ActivityItem {
  id: string
  user: string
  action: string
  time: string
  status: 'success' | 'warning' | 'error' | 'info'
}

/** 柱状图数据项 */
interface BarData {
  label: string
  value: number
  maxValue: number
}

// ---------------------------------------------------------------------------
// 模拟数据
// ---------------------------------------------------------------------------

/** 统计卡片数据 */
const STATS: StatCard[] = [
  {
    id: 'users',
    label: '总用户数',
    value: '12,847',
    change: '+12.5%',
    trend: 'up',
    icon: <Users className="h-5 w-5" />,
    color: 'from-blue-500 to-blue-600',
  },
  {
    id: 'sessions',
    label: '活跃会话',
    value: '3,429',
    change: '+8.2%',
    trend: 'up',
    icon: <Activity className="h-5 w-5" />,
    color: 'from-emerald-500 to-emerald-600',
  },
  {
    id: 'tasks',
    label: '完成任务数',
    value: '8,921',
    change: '+23.1%',
    trend: 'up',
    icon: <CheckCircle2 className="h-5 w-5" />,
    color: 'from-violet-500 to-violet-600',
  },
  {
    id: 'load',
    label: '系统负载',
    value: '67.3%',
    change: '-3.1%',
    trend: 'down',
    icon: <Cpu className="h-5 w-5" />,
    color: 'from-amber-500 to-amber-600',
  },
]

/** 柱状图数据 -- 周活跃用户 */
const BAR_DATA: BarData[] = [
  { label: '周一', value: 420, maxValue: 600 },
  { label: '周二', value: 380, maxValue: 600 },
  { label: '周三', value: 510, maxValue: 600 },
  { label: '周四', value: 470, maxValue: 600 },
  { label: '周五', value: 590, maxValue: 600 },
  { label: '周六', value: 320, maxValue: 600 },
  { label: '周日', value: 280, maxValue: 600 },
]

/** 进度条数据 -- 服务健康度 */
const PROGRESS_DATA = [
  { label: 'API 服务', value: 96, color: 'bg-emerald-500' },
  { label: '数据库', value: 82, color: 'bg-blue-500' },
  { label: '消息队列', value: 73, color: 'bg-violet-500' },
  { label: '缓存服务', value: 91, color: 'bg-amber-500' },
]

/** 最近活动列表数据 */
const ACTIVITIES: ActivityItem[] = [
  {
    id: '1',
    user: '张明',
    action: '部署了 v2.4.1 版本到生产环境',
    time: '3 分钟前',
    status: 'success',
  },
  {
    id: '2',
    user: '李华',
    action: '创建了新的自动化测试任务',
    time: '15 分钟前',
    status: 'info',
  },
  {
    id: '3',
    user: '王芳',
    action: '数据库连接池告警已触发',
    time: '28 分钟前',
    status: 'warning',
  },
  {
    id: '4',
    user: '赵磊',
    action: '修复了用户权限校验异常',
    time: '1 小时前',
    status: 'success',
  },
  {
    id: '5',
    user: '陈静',
    action: 'CDN 节点响应超时',
    time: '2 小时前',
    status: 'error',
  },
  {
    id: '6',
    user: '刘洋',
    action: '更新了系统监控阈值配置',
    time: '3 小时前',
    status: 'info',
  },
]

// ---------------------------------------------------------------------------
// 辅助函数
// ---------------------------------------------------------------------------

/**
 * 获取活动状态对应的标签样式
 * @param status - 活动状态类型
 * @returns TailwindCSS 类名字符串
 */
function getStatusStyle(status: ActivityItem['status']): string {
  const map: Record<ActivityItem['status'], string> = {
    success: 'bg-emerald-500/15 text-emerald-400',
    warning: 'bg-amber-500/15 text-amber-400',
    error: 'bg-red-500/15 text-red-400',
    info: 'bg-blue-500/15 text-blue-400',
  }
  return map[status]
}

/**
 * 获取活动状态对应的中文标签
 * @param status - 活动状态类型
 * @returns 状态中文名称
 */
function getStatusLabel(status: ActivityItem['status']): string {
  const map: Record<ActivityItem['status'], string> = {
    success: '成功',
    warning: '警告',
    error: '错误',
    info: '信息',
  }
  return map[status]
}

// ---------------------------------------------------------------------------
// 子组件
// ---------------------------------------------------------------------------

/**
 * 顶部导航栏组件
 *
 * 包含 Logo、搜索框、通知图标和用户头像
 */
function TopNavBar() {
  return (
    <header className="sticky top-0 z-30 border-b border-slate-700/50 bg-slate-900/80 backdrop-blur-xl">
      <nav className="mx-auto flex h-16 max-w-7xl items-center justify-between px-4 sm:px-6 lg:px-8">
        {/* Logo 区域 */}
        <div className="flex items-center gap-3">
          <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-gradient-to-br from-blue-500 to-blue-600 shadow-lg shadow-blue-500/25">
            <Zap className="h-5 w-5 text-white" />
          </div>
          <span className="text-lg font-bold tracking-tight text-white">
            Agent<span className="text-blue-400">OS</span>
          </span>
        </div>

        {/* 搜索框 */}
        <div className="hidden max-w-md flex-1 px-8 md:block">
          <div className="relative">
            <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-400" />
            <input
              type="text"
              placeholder="搜索功能、任务、设置..."
              className="w-full rounded-lg border border-slate-700 bg-slate-800/50 py-2 pr-4 pl-10 text-sm text-slate-200 placeholder-slate-500 transition-all duration-200 focus:border-blue-500 focus:ring-2 focus:ring-blue-500/20 focus:outline-none"
            />
            <kbd className="absolute top-1/2 right-3 -translate-y-1/2 rounded border border-slate-600 bg-slate-700 px-1.5 py-0.5 text-[10px] text-slate-400">
              Ctrl+K
            </kbd>
          </div>
        </div>

        {/* 右侧操作区 */}
        <div className="flex items-center gap-2">
          <button
            className="relative rounded-lg p-2 text-slate-400 transition-colors duration-200 hover:bg-slate-800 hover:text-white"
            aria-label="通知"
          >
            <Bell className="h-5 w-5" />
            <span className="absolute top-1.5 right-1.5 h-2 w-2 rounded-full bg-blue-500 ring-2 ring-slate-900" />
          </button>
          <button
            className="rounded-lg p-2 text-slate-400 transition-colors duration-200 hover:bg-slate-800 hover:text-white"
            aria-label="设置"
          >
            <Settings className="h-5 w-5" />
          </button>
          <div className="ml-2 h-8 w-px bg-slate-700" />
          <button className="group ml-2 flex items-center gap-2 rounded-lg p-1 transition-colors duration-200 hover:bg-slate-800">
            <div className="flex h-8 w-8 items-center justify-center rounded-full bg-gradient-to-br from-blue-400 to-violet-500 text-sm font-semibold text-white">
              A
            </div>
            <span className="hidden text-sm font-medium text-slate-300 group-hover:text-white sm:inline">
              Admin
            </span>
          </button>
        </div>
      </nav>
    </header>
  )
}

/**
 * 统计卡片组件
 *
 * @param stat - 统计数据项
 */
function StatCard({ stat }: { stat: StatCard }) {
  return (
    <article className="group relative overflow-hidden rounded-xl border border-slate-700/50 bg-slate-800/50 p-5 transition-all duration-300 hover:border-slate-600 hover:bg-slate-800/80 hover:shadow-lg hover:shadow-slate-900/50">
      {/* 背景渐变装饰 */}
      <div
        className={`absolute -right-6 -top-6 h-24 w-24 rounded-full bg-gradient-to-br ${stat.color} opacity-10 blur-2xl transition-opacity duration-300 group-hover:opacity-20`}
      />

      <div className="relative">
        <div className="flex items-center justify-between">
          <span className="text-sm font-medium text-slate-400">{stat.label}</span>
          <div
            className={`flex h-10 w-10 items-center justify-center rounded-lg bg-gradient-to-br ${stat.color} shadow-lg`}
          >
            <span className="text-white">{stat.icon}</span>
          </div>
        </div>
        <div className="mt-3 flex items-end justify-between">
          <span className="text-2xl font-bold tracking-tight text-white">{stat.value}</span>
          <span
            className={`flex items-center gap-0.5 text-xs font-medium ${
              stat.trend === 'up' ? 'text-emerald-400' : 'text-red-400'
            }`}
          >
            {stat.trend === 'up' ? (
              <TrendingUp className="h-3 w-3" />
            ) : (
              <TrendingDown className="h-3 w-3" />
            )}
            {stat.change}
          </span>
        </div>
      </div>
    </article>
  )
}

/**
 * 柱状图组件
 *
 * 使用纯 CSS 绘制的简单柱状图，展示周活跃用户数据
 */
function BarChart() {
  const maxVal = Math.max(...BAR_DATA.map((d) => d.value))

  return (
    <section className="rounded-xl border border-slate-700/50 bg-slate-800/50 p-5">
      <div className="mb-5 flex items-center justify-between">
        <div>
          <h3 className="text-sm font-semibold text-white">周活跃用户</h3>
          <p className="mt-0.5 text-xs text-slate-400">过去 7 天的用户活动趋势</p>
        </div>
        <div className="flex items-center gap-1 rounded-lg bg-blue-500/10 px-2.5 py-1 text-xs font-medium text-blue-400">
          <BarChart3 className="h-3.5 w-3.5" />
          图表
        </div>
      </div>

      {/* 柱状图区域 */}
      <div className="flex items-end justify-between gap-3" style={{ height: '180px' }}>
        {BAR_DATA.map((bar) => {
          const heightPercent = maxVal > 0 ? (bar.value / maxVal) * 100 : 0
          return (
            <div key={bar.label} className="flex flex-1 flex-col items-center gap-2">
              <span className="text-[10px] font-medium text-slate-400">{bar.value}</span>
              <div className="relative w-full" style={{ height: '140px' }}>
                <div
                  className="absolute right-0 bottom-0 left-0 rounded-t-md bg-gradient-to-t from-blue-600 to-blue-400 transition-all duration-500 hover:from-blue-500 hover:to-blue-300"
                  style={{ height: `${heightPercent}%` }}
                />
              </div>
              <span className="text-[11px] text-slate-500">{bar.label}</span>
            </div>
          )
        })}
      </div>
    </section>
  )
}

/**
 * 进度条图表组件
 *
 * 展示各服务的健康度百分比
 */
function ProgressChart() {
  return (
    <section className="rounded-xl border border-slate-700/50 bg-slate-800/50 p-5">
      <div className="mb-5 flex items-center justify-between">
        <div>
          <h3 className="text-sm font-semibold text-white">服务健康度</h3>
          <p className="mt-0.5 text-xs text-slate-400">各核心服务的运行状态</p>
        </div>
        <div className="flex items-center gap-1 rounded-lg bg-emerald-500/10 px-2.5 py-1 text-xs font-medium text-emerald-400">
          <Activity className="h-3.5 w-3.5" />
          正常
        </div>
      </div>

      <div className="space-y-5">
        {PROGRESS_DATA.map((item) => (
          <div key={item.label}>
            <div className="mb-1.5 flex items-center justify-between">
              <span className="text-sm text-slate-300">{item.label}</span>
              <span className="text-sm font-semibold text-white">{item.value}%</span>
            </div>
            <div className="h-2 overflow-hidden rounded-full bg-slate-700/50">
              <div
                className={`h-full rounded-full ${item.color} transition-all duration-700 ease-out`}
                style={{ width: `${item.value}%` }}
              />
            </div>
          </div>
        ))}
      </div>
    </section>
  )
}

/**
 * 最近活动列表组件
 *
 * 展示带时间戳和状态标签的活动记录
 */
function ActivityList() {
  return (
    <section className="rounded-xl border border-slate-700/50 bg-slate-800/50 p-5">
      <div className="mb-4 flex items-center justify-between">
        <div>
          <h3 className="text-sm font-semibold text-white">最近活动</h3>
          <p className="mt-0.5 text-xs text-slate-400">团队成员的操作记录</p>
        </div>
        <button className="flex items-center gap-1 text-xs font-medium text-blue-400 transition-colors hover:text-blue-300">
          查看全部
          <ArrowRight className="h-3 w-3" />
        </button>
      </div>

      <ul className="divide-y divide-slate-700/50">
        {ACTIVITIES.map((activity) => (
          <li key={activity.id} className="group flex items-start gap-3 py-3.5 first:pt-0 last:pb-0">
            {/* 用户头像 */}
            <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-slate-700 text-xs font-bold text-slate-300 transition-colors group-hover:bg-slate-600">
              {activity.user.charAt(0)}
            </div>

            {/* 活动内容 */}
            <div className="min-w-0 flex-1">
              <div className="flex items-center gap-2">
                <span className="text-sm font-medium text-slate-200">{activity.user}</span>
                <span
                  className={`rounded-full px-2 py-0.5 text-[10px] font-medium ${getStatusStyle(activity.status)}`}
                >
                  {getStatusLabel(activity.status)}
                </span>
              </div>
              <p className="mt-0.5 truncate text-sm text-slate-400">{activity.action}</p>
            </div>

            {/* 时间戳 */}
            <div className="flex shrink-0 items-center gap-1 text-xs text-slate-500">
              <Clock className="h-3 w-3" />
              {activity.time}
            </div>
          </li>
        ))}
      </ul>
    </section>
  )
}

/**
 * 快速操作面板组件
 *
 * 包含 4 个常用操作的快捷按钮
 */
function QuickActions() {
  const [loadingId, setLoadingId] = useState<string | null>(null)

  /** 模拟操作点击的加载效果 */
  const handleAction = (id: string) => {
    setLoadingId(id)
    setTimeout(() => setLoadingId(null), 1200)
  }

  const actions = [
    {
      id: 'new-task',
      label: '新建任务',
      description: '创建自动化执行任务',
      icon: <Plus className="h-5 w-5" />,
      gradient: 'from-blue-500 to-blue-600',
      shadow: 'shadow-blue-500/25',
    },
    {
      id: 'upload',
      label: '上传文件',
      description: '批量导入配置文件',
      icon: <Upload className="h-5 w-5" />,
      gradient: 'from-emerald-500 to-emerald-600',
      shadow: 'shadow-emerald-500/25',
    },
    {
      id: 'download',
      label: '导出报告',
      description: '下载系统运行报告',
      icon: <Download className="h-5 w-5" />,
      gradient: 'from-violet-500 to-violet-600',
      shadow: 'shadow-violet-500/25',
    },
    {
      id: 'refresh',
      label: '刷新数据',
      description: '同步最新系统状态',
      icon: <RefreshCw className={`h-5 w-5 ${loadingId === 'refresh' ? 'animate-spin' : ''}`} />,
      gradient: 'from-amber-500 to-amber-600',
      shadow: 'shadow-amber-500/25',
    },
  ]

  return (
    <section className="rounded-xl border border-slate-700/50 bg-slate-800/50 p-5">
      <div className="mb-4">
        <h3 className="text-sm font-semibold text-white">快速操作</h3>
        <p className="mt-0.5 text-xs text-slate-400">常用功能快捷入口</p>
      </div>

      <div className="grid grid-cols-2 gap-3">
        {actions.map((action) => (
          <button
            key={action.id}
            onClick={() => handleAction(action.id)}
            disabled={loadingId === action.id}
            className="group relative flex items-center gap-3 rounded-lg border border-slate-700/50 bg-slate-800 p-3.5 text-left transition-all duration-200 hover:border-slate-600 hover:bg-slate-750 hover:shadow-md disabled:cursor-wait disabled:opacity-70"
          >
            <div
              className={`flex h-10 w-10 shrink-0 items-center justify-center rounded-lg bg-gradient-to-br ${action.gradient} ${action.shadow} shadow-lg`}
            >
              <span className="text-white">{action.icon}</span>
            </div>
            <div className="min-w-0">
              <div className="text-sm font-medium text-slate-200">{action.label}</div>
              <div className="truncate text-[11px] text-slate-500">{action.description}</div>
            </div>
          </button>
        ))}
      </div>
    </section>
  )
}

// ---------------------------------------------------------------------------
// 主页面组件
// ---------------------------------------------------------------------------

/**
 * Dashboard 仪表板测试页面
 *
 * 自包含的仪表板演示页面，展示统计卡片、图表、活动列表和快速操作面板。
 * 使用深色主题（slate-900），蓝色调强调色（blue-500）。
 */
export default function DashboardTest() {
  return (
    <div className="min-h-screen bg-slate-900 text-slate-100">
      {/* 顶部导航栏 */}
      <TopNavBar />

      {/* 主要内容区域 */}
      <main className="mx-auto max-w-7xl px-4 py-6 sm:px-6 lg:px-8">
        {/* 页面标题 */}
        <div className="mb-6">
          <h1 className="text-xl font-bold text-white sm:text-2xl">仪表板概览</h1>
          <p className="mt-1 text-sm text-slate-400">
            欢迎回来，Admin。以下是系统当前的运行概况。
          </p>
        </div>

        {/* 统计卡片区域 */}
        <div className="mb-6 grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
          {STATS.map((stat) => (
            <StatCard key={stat.id} stat={stat} />
          ))}
        </div>

        {/* 图表区域：柱状图 + 进度条 */}
        <div className="mb-6 grid grid-cols-1 gap-4 lg:grid-cols-2">
          <BarChart />
          <ProgressChart />
        </div>

        {/* 底部区域：活动列表 + 快速操作 */}
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
          <div className="lg:col-span-2">
            <ActivityList />
          </div>
          <div>
            <QuickActions />
          </div>
        </div>
      </main>
    </div>
  )
}
