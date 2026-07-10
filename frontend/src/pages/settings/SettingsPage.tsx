/** 设置中心页面 展示卡片网格链接到各设置子页面，包括专用设置页和通用配置页。 */

import { Link } from 'react-router-dom'
import { CONFIG_GROUPS } from '@/constants/genericConfigs'

/** 设置项配置 */
interface SettingCard {
  title: string
  description: string
  href: string
  icon: string
}

/** 基础设置页（有独立页面的配置） */
const SETTINGS_CARDS: SettingCard[] = [
  {
    title: '模块设置',
    description: '管理已安装模块的配置',
    href: '/settings/modules',
    icon: '🧩',
  },
  {
    title: '主题设置',
    description: '切换界面主题和显示模式',
    href: '/settings/theme',
    icon: '🎨',
  },
  {
    title: 'LLM 配置',
    description: '配置大语言模型参数',
    href: '/settings/llm',
    icon: '🤖',
  },
  {
    title: '插件设置',
    description: '管理插件配置',
    href: '/settings/plugins',
    icon: '🔌',
  },
]

/** REQ-19 补充的配置页面已合并到 CONFIG_GROUPS 通用配置分组中。 原先此处有 EXTENDED_SETTINGS_CARDS 数组，使用 CategoryConfigPage 组件。 */

/** 设置中心页面组件 */
export function SettingsPage() {
  return (
    <div className="bg-background text-foreground flex h-screen flex-col overflow-hidden">
      <header className="flex h-12 shrink-0 items-center border-b px-4">
        <Link to="/" className="text-muted-foreground hover:text-foreground text-sm">
          &larr; 返回
        </Link>
        <h1 className="ml-4 text-base font-semibold">设置中心</h1>
      </header>
      <main className="flex-1 overflow-y-auto p-3 sm:p-6">
        {/* 基础设置 */}
        <section className="mb-8">
          <h2 className="text-foreground mb-4 text-sm font-semibold">基础设置</h2>
          <div className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-3">
            {SETTINGS_CARDS.map((card) => (
              <SettingCardLink key={card.href} card={card} />
            ))}
          </div>
        </section>

        {/* 通用配置分组 */}
        {CONFIG_GROUPS.map((group) => (
          <section key={group.name} className="mb-8">
            <h2 className="text-foreground mb-4 text-sm font-semibold">{group.name}</h2>
            <div className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-3">
              {group.items.map((item) => (
                <SettingCardLink
                  key={item.configPath}
                  card={{
                    title: item.title,
                    description: item.description,
                    href: `/settings/generic/${item.configPath}`,
                    icon: item.icon,
                  }}
                />
              ))}
            </div>
          </section>
        ))}
      </main>
    </div>
  )
}

/** 设置卡片链接 */
function SettingCardLink({ card }: { card: SettingCard }) {
  return (
    <Link
      to={card.href}
      className="bg-card hover:bg-accent/50 block rounded-lg border p-5 transition-colors"
    >
      <div className="mb-2 text-2xl">{card.icon}</div>
      <h3 className="mb-1 text-sm font-semibold">{card.title}</h3>
      <p className="text-muted-foreground text-xs">{card.description}</p>
    </Link>
  )
}
