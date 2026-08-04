/**
 * Schema 降级渲染
 *
 * 移动端/弱客户端的渲染降级策略：
 * - 工作区 → 卡片（聊天内嵌）
 * - 悬浮窗 → 底部抽屉
 * - Dock → 底部工具栏
 * - 全屏 → 新页面
 */

import type { ModuleUISchema, RenderingSpaceConfig } from '@/types/schema'

/** 降级策略 */
interface DegradationStrategy {
  from: string
  to: string
  transform: (config: RenderingSpaceConfig) => RenderingSpaceConfig
}

/** 移动端降级策略列表 */
const MOBILE_STRATEGIES: DegradationStrategy[] = [
  {
    from: 'workspace',
    to: 'chat',
    transform: (config) => ({
      ...config,
      space: 'chat',
      props: { ...config.props, compact: true },
    }),
  },
  {
    from: 'floating',
    to: 'chat',
    transform: (config) => ({
      ...config,
      space: 'chat',
      props: { ...config.props, drawer: true, position: 'bottom' },
    }),
  },
  {
    from: 'dock',
    to: 'chat',
    transform: (config) => ({
      ...config,
      space: 'chat',
      props: { ...config.props, toolbar: true },
    }),
  },
  {
    from: 'fullscreen',
    to: 'chat',
    transform: (config) => ({
      ...config,
      space: 'chat',
      props: { ...config.props, fullpage: true },
    }),
  },
]

/**
 * 对 Schema 应用降级策略
 */
export function applyDegradation(
  schema: ModuleUISchema,
  availableSpaces: string[],
): ModuleUISchema {
  const degradedSpaces = schema.rendering.spaces.map((space) => {
    if (availableSpaces.includes(space.space)) return space

    const strategy = MOBILE_STRATEGIES.find((s) => s.from === space.space)
    if (strategy) return strategy.transform(space)

    return {
      ...space,
      space: 'chat' as const,
      props: { ...space.props, degraded: true },
    }
  })

  return {
    ...schema,
    rendering: {
      ...schema.rendering,
      spaces: degradedSpaces,
    },
  }
}
