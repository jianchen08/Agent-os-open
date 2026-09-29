/** @feature FP-T12 前端适配 | @ci: frontend-test */
/**
 * modePanel 聚合 + ModePanelBadge 组件测试（模式体系 §5.0 Wave2 件3）
 *
 * 取数面：面板页配对 = registry 聚合（workspace/tab 面板页声明携 mode 扩展字段
 * → 按 mode 配对）；徽标名/图标与选择器选项同源 = modes registry 声明派生
 * （mode.yaml name/icon，批 G⑦ 起单一真值 registry，select-option 声明退役）。
 * 渲染→交互链路：徽标点击 openPluginPage 打开面板页签；无声明/禁用
 * （§5.3 查不到映射）一律不渲染。
 */
import { fireEvent, render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { contributionRegistry } from '@/services/schema/ContributionRegistry'
import { getModePanelIcon, getModePanelLabel, getModePanelTarget } from '@/services/schema/modePanel'
import { useLayoutModeStore } from '@/stores/layoutModeStore'
import { ModePanelBadge } from '../ModePanelBadge'

/** 合成 modes registry（徽标名/图标数据源）：beta 带 icon，alpha 无 icon */
const REGISTRY = {
  modes: [
    {
      mode: 'alpha',
      name: '阿尔法模式',
      pipelines: [],
      presenter: { source: 'none' },
      tool_card: 'native',
      icon: null,
      plugin_id: 'plug_x',
    },
    {
      mode: 'beta',
      name: '贝塔模式',
      pipelines: [],
      presenter: { source: 'none' },
      tool_card: 'native',
      icon: '⭐',
      plugin_id: 'plug_y',
    },
  ],
  total: 2,
  errors: [],
}

// 组件经 useModesRegistry 取声明（网络边界 mock：合成 registry 直返）
vi.mock('@/services/api/modes', async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  useModesRegistry: () => REGISTRY,
}))

/** 模式插件声明种子（收集后一次性 loadFromSchema——装载幂等清空 registry） */
interface ModeSeed {
  pluginId: string
  mode: string
  label: string
  pageId: string
}
const seeds: ModeSeed[] = []

function seedModePlugin(pluginId: string, mode: string, label: string, pageId: string): void {
  seeds.push({ pluginId, mode, label, pageId })
}

function flushSeeds(): void {
  contributionRegistry.loadFromSchema({
    plugin_contributes: seeds.map((s) => ({
      plugin_id: s.pluginId,
      plugin_name: s.pluginId,
      contributes: {
        pages: [
          {
            id: s.pageId,
            title: `${s.label}面板`,
            space: 'workspace',
            slot: 'tab',
            widget: 'webview',
            mode: s.mode,
          },
        ],
      },
    })),
    plugin_configs: [],
  })
}

describe('modePanel — mode → 面板页聚合', () => {
  beforeEach(() => {
    contributionRegistry.clear()
  })

  it('mode 命中声明 → 返回同插件 workspace/tab 面板页（panel_page_id 同源）', () => {
    seedModePlugin('plug_x', 'alpha', '阿尔法', 'alpha_desk')
    seedModePlugin('plug_y', 'beta', '贝塔', 'beta_desk')
    flushSeeds()

    const target = getModePanelTarget('beta')
    expect(target?.id).toBe('beta_desk')
    expect(target?.pluginId).toBe('plug_y')
    // 徽标名/图标 = registry 声明派生（decl.name/decl.icon；未声明 icon → undefined）
    expect(getModePanelLabel('beta', REGISTRY)).toBe('贝塔模式')
    expect(getModePanelIcon('beta', REGISTRY)).toBe('⭐')
    expect(getModePanelLabel('alpha', REGISTRY)).toBe('阿尔法模式')
    expect(getModePanelIcon('alpha', REGISTRY)).toBeUndefined()
  })

  it('registry 未就绪/未收录 → label 回退 mode 值、icon 缺省（查不到=不发明）', () => {
    expect(getModePanelLabel('ghost', undefined)).toBe('ghost')
    expect(getModePanelIcon('ghost', REGISTRY)).toBeUndefined()
    expect(getModePanelLabel('ghost', REGISTRY)).toBe('ghost')
  })

  it('未声明 / 禁用同源消失 → undefined（查不到映射=不渲染）', () => {
    seedModePlugin('plug_x', 'alpha', '阿尔法', 'alpha_desk')
    flushSeeds()

    expect(getModePanelTarget('no_such_mode')).toBeUndefined()
    expect(getModePanelTarget('')).toBeUndefined()
    // 全部声明清空（模拟插件禁用）→ 同源消失
    contributionRegistry.clear()
    expect(getModePanelTarget('alpha')).toBeUndefined()
  })
})

describe('ModePanelBadge — 徽标渲染与点击导航', () => {
  beforeEach(() => {
    contributionRegistry.clear()
    useLayoutModeStore.setState({ workspaceTabs: [] })
  })

  it('有映射 → 渲染徽标（registry 声明图标+模式名）；点击打开面板页签且不触发行点击', () => {
    seedModePlugin('plug_x', 'alpha', '阿尔法', 'alpha_desk')
    seedModePlugin('plug_y', 'beta', '贝塔', 'beta_desk')
    flushSeeds()
    render(<ModePanelBadge mode="beta" />)

    const badge = screen.getByTestId('mode-badge-beta')
    expect(badge).toHaveTextContent('贝塔模式')
    expect(badge).toHaveTextContent('⭐')

    fireEvent.click(badge)
    const tabs = useLayoutModeStore.getState().workspaceTabs
    expect(tabs).toHaveLength(1)
    expect(tabs[0].id).toBe('ws-plugin-beta_desk')
  })

  it('registry 未声明 icon → 徽标仅模式名（缺省不渲染图标位）', () => {
    seedModePlugin('plug_x', 'alpha', '阿尔法', 'alpha_desk')
    flushSeeds()
    render(<ModePanelBadge mode="alpha" />)
    expect(screen.getByTestId('mode-badge-alpha')).toHaveTextContent('阿尔法模式')
  })

  it('查不到映射 → 不渲染（零 mode 零渲染）', () => {
    render(<ModePanelBadge mode="ghost" />)
    expect(screen.queryByTestId('mode-badge-ghost')).not.toBeInTheDocument()
  })
})
