/** 设置中心页面 展示卡片网格链接到各设置子页面，包括专用设置页和通用配置页。 */

import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { contributionRegistry } from '@/services/schema/ContributionRegistry'
import { getSchema } from '@/services/api/schema'
import type { SettingsPanelEntry } from '@/services/schema/ContributionRegistry'

/** 设置项配置 */
interface SettingCard {
  title: string
  description: string
  href: string
  icon: string
}

/** 基础设置页（有独立页面的配置，内核独占） */
const KERNEL_SETTINGS_CARDS: SettingCard[] = [
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

/** 设置中心页面组件 */
export function SettingsPage() {
  const [settingsPanels, setSettingsPanels] = useState<SettingsPanelEntry[]>([])
  const [isLoading, setIsLoading] = useState(true)

  // 加载 schema 并注册到 ContributionRegistry
  useEffect(() => {
    let cancelled = false
    setIsLoading(true)

    getSchema()
      .then((schema) => {
        if (!cancelled) {
          contributionRegistry.loadFromSchema(schema as unknown as Record<string, unknown>)
          setSettingsPanels(contributionRegistry.getSettingsPanels())
        }
      })
      .catch(() => {
        // schema 加载失败时静默降级：只显示内核设置
      })
      .finally(() => {
        if (!cancelled) setIsLoading(false)
      })

    return () => { cancelled = true }
  }, [])

  return (
    <div className="bg-background text-foreground flex h-screen flex-col overflow-hidden">
      <header className="flex h-12 shrink-0 items-center border-b px-4">
        <Link to="/" className="text-muted-foreground hover:text-foreground text-sm">
          &larr; 返回
        </Link>
        <h1 className="ml-4 text-base font-semibold">设置中心</h1>
      </header>
      <main className="flex-1 overflow-y-auto p-3 sm:p-6">
        {/* 内核设置（内核独占，少数几项） */}
        <section className="mb-8">
          <h2 className="text-foreground mb-4 text-sm font-semibold">内核设置</h2>
          <div className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-3">
            {KERNEL_SETTINGS_CARDS.map((card) => (
              <SettingCardLink key={card.href} card={card} />
            ))}
          </div>
        </section>

        {/* 插件配置（按插件聚合，来自 ContributionRegistry） */}
        {!isLoading && settingsPanels.length > 0 && (
          <section className="mb-8">
            <h2 className="text-foreground mb-4 text-sm font-semibold">插件配置</h2>
            <div className="space-y-6">
              {settingsPanels.map((panel) => (
                <PluginConfigSection key={panel.pluginId} panel={panel} />
              ))}
            </div>
          </section>
        )}

        {/* 加载中提示 */}
        {isLoading && (
          <div className="text-muted-foreground flex items-center justify-center py-8">
            <span className="text-sm">加载插件配置...</span>
          </div>
        )}
      </main>
    </div>
  )
}

/** 插件配置分区（按插件聚合展示） */
function PluginConfigSection({ panel }: { panel: SettingsPanelEntry }) {
  return (
    <div>
      <div className="mb-3 flex items-center gap-2">
        <span className="text-lg">{panel.pluginIcon || '🔧'}</span>
        <h3 className="text-sm font-medium">{panel.pluginName}</h3>
      </div>
      <div className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-3">
        {panel.configFiles.map((file) => (
          <SettingCardLink
            key={file.id}
            card={{
              title: file.label,
              description: file.path,
              href: `/settings/plugin/${panel.pluginId}/${file.id}`,
              icon: '⚙️',
            }}
          />
        ))}
      </div>
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
