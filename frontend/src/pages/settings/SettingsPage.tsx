/**
 * 设置中心页面
 *
 * 展示卡片网格链接到各设置子页面
 */

/** 设置项配置 */
interface SettingCard {
  title: string
  description: string
  href: string
  icon: string
}

/** 所有设置项 */
const SETTINGS_CARDS: SettingCard[] = [
  {
    title: '模块设置',
    description: '管理已安装模块的配置',
    href: '/settings/modules',
    icon: '🧩',
  },
  {
    title: 'API 配置',
    description: '管理外部 API 密钥和端点',
    href: '/settings/api',
    icon: '🔑',
  },
  {
    title: 'LLM 配置',
    description: '配置大语言模型参数',
    href: '/settings/llm',
    icon: '🤖',
  },
  {
    title: '上下文窗口',
    description: '管理上下文窗口大小和策略',
    href: '/settings/context',
    icon: '📐',
  },
  {
    title: '并发配置',
    description: '设置任务并发数和队列参数',
    href: '/settings/concurrency',
    icon: '⚡',
  },
  {
    title: '成本控制',
    description: 'Token 用量限制和预算管理',
    href: '/settings/cost',
    icon: '💰',
  },
]

/**
 * 设置中心页面组件
 */
export function SettingsPage() {
  return (
    <div className="h-screen flex flex-col bg-background text-foreground overflow-hidden">
      <header className="h-12 border-b flex items-center px-4 shrink-0">
        <a href="/" className="text-sm text-muted-foreground hover:text-foreground">
          &larr; 返回
        </a>
        <h1 className="ml-4 text-base font-semibold">设置中心</h1>
      </header>
      <main className="flex-1 overflow-y-auto p-6">
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
          {SETTINGS_CARDS.map(card => (
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
