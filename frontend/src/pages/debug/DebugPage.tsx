/**
 * 调试中心入口页面
 *
 * 卡片网格链接到各调试子页面
 */

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
]

/**
 * 调试中心入口页面组件
 */
export function DebugPage() {
  return (
    <div className="h-screen flex flex-col bg-background text-foreground overflow-hidden">
      <header className="h-12 border-b flex items-center px-4 shrink-0">
        <a href="/" className="text-sm text-muted-foreground hover:text-foreground">
          &larr; 返回
        </a>
        <h1 className="ml-4 text-base font-semibold">调试中心</h1>
      </header>
      <main className="flex-1 overflow-y-auto p-6">
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
          {DEBUG_CARDS.map(card => (
            <a
              key={card.href}
              href={card.href}
              className="block p-5 rounded-lg border bg-card hover:bg-accent/50 transition-colors"
            >
              <div className="text-2xl mb-2">{card.icon}</div>
              <h3 className="text-sm font-semibold mb-1">{card.title}</h3>
              <p className="text-xs text-muted-foreground">{card.description}</p>
            </a>
          ))}
        </div>
      </main>
    </div>
  )
}
