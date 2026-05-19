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
    <div className="bg-background text-foreground flex h-screen flex-col overflow-hidden">
      <header className="flex h-12 shrink-0 items-center border-b px-4">
        <a href="/" className="text-muted-foreground hover:text-foreground text-sm">
          &larr; 返回
        </a>
        <h1 className="ml-4 text-base font-semibold">调试中心</h1>
      </header>
      <main className="flex-1 overflow-y-auto p-3 sm:p-6">
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
      </main>
    </div>
  )
}
