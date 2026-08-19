/**
 * 调试中心入口页面
 *
 * 卡片网格链接到各调试子页面
 * 统一外壳走 shared/PageShell。
 */

import { PageShell } from '@/components/shared/PageShell'

/** 调试子页面配置 */
interface DebugCard {
  title: string
  description: string
  href: string
  icon: string
}

/** 所有调试子页面 */
const DEBUG_CARDS: DebugCard[] = [
  {
    title: '执行记录',
    description: '查看所有执行记录和调用链路',
    href: '/debug/execution-records',
    icon: '📋',
  },
  {
    title: '会话',
    description: '查看和调试会话数据',
    href: '/debug/sessions',
    icon: '💬',
  },
  {
    title: '任务',
    description: '查看任务执行状态和历史',
    href: '/debug/tasks',
    icon: '⚙️',
  },
  {
    title: '评估指标',
    description: '查看系统评估指标和得分',
    href: '/debug/evaluation-metrics',
    icon: '📊',
  },
  {
    title: '用户',
    description: '查看用户调试信息和状态',
    href: '/debug/users',
    icon: '👤',
  },
  {
    title: '数据库',
    description: '浏览任意表、筛选/编辑行数据、SQL 调试',
    href: '/debug/db',
    icon: '🗄️',
  },
  {
    title: 'LLM 请求',
    description: '最近发送给大模型的真实请求体快照（messages 逐条渲染）',
    href: '/debug/llm-payload',
    icon: '🧠',
  },
]

/**
 * 调试中心入口页面组件
 */
export function DebugPage() {
  return (
    <PageShell title="调试中心" backHref="/">
      <div className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-3">
        {DEBUG_CARDS.map((card) => (
          <a
            key={card.href}
            href={card.href}
            className="bg-card hover:bg-accent/50 block rounded-lg border p-5 transition-colors"
          >
            <div className="mb-2 text-2xl">{card.icon}</div>
            <h3 className="mb-1 text-sm font-semibold">{card.title}</h3>
            <p className="text-muted-foreground text-xs">{card.description}</p>
          </a>
        ))}
      </div>
    </PageShell>
  )
}
