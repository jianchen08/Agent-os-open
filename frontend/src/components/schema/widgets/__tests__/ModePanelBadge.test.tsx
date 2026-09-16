/** @feature FP-T12 前端适配 | @ci: frontend-test */
/**
 * modePanel 聚合 + ModePanelBadge 组件测试（模式体系 §5.0 Wave2 件3）
 *
 * 取数面：registry 聚合（workspace/tab 面板页声明携 mode 扩展字段 → 按 mode
 * 配对）。徽标名/图标与选择器选项同源（模式插件 select-option 追加声明）。
 * 渲染→交互链路：徽标点击 openPluginPage 打开面板页签；无声明/禁用
 * （§5.3 查不到映射）一律不渲染。
 */
import { fireEvent, render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it } from 'vitest'
import { contributionRegistry } from '@/services/schema/ContributionRegistry'
import { useLayoutModeStore } from '@/stores/layoutModeStore'
import { getModePanelIcon, getModePanelLabel, getModePanelTarget } from '@/services/schema/modePanel'
import { ModePanelBadge } from '../ModePanelBadge'

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
      ui_schema: {
        widgets: [
          {
            id: `mode_opt_${s.mode}`,
            type: 'select-option',
            space: 'chat-input',
            order: 10,
            props: { target: 'task_mode', value: s.mode, label: s.label, icon: '⭐' },
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
    expect(getModePanelLabel('beta')).toBe('贝塔')
    expect(getModePanelIcon('beta')).toBe('⭐')
  })

  it('未声明 / 禁用同源消失 / 仅选项无面板页 → undefined（查不到映射=不渲染）', () => {
    seedModePlugin('plug_x', 'alpha', '阿尔法', 'alpha_desk')
    flushSeeds()
    // 仅 select-option 选项声明、无 workspace/tab 页 → 无面板可导航
    contributionRegistry.loadFromSchema({
      plugin_contributes: [
        {
          plugin_id: 'plug_g',
          plugin_name: 'plug_g',
          ui_schema: {
            widgets: [
              {
                id: 'mode_opt_gamma',
                type: 'select-option',
                space: 'chat-input',
                order: 10,
                props: { target: 'task_mode', value: 'gamma', label: '伽马', icon: '⭐' },
              },
            ],
          },
        },
      ],
      plugin_configs: [],
    })

    expect(getModePanelTarget('no_such_mode')).toBeUndefined()
    expect(getModePanelTarget('')).toBeUndefined()
    expect(getModePanelTarget('gamma')).toBeUndefined()
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

  it('有映射 → 渲染徽标（图标+模式名）；点击打开面板页签且不触发行点击', () => {
    seedModePlugin('plug_x', 'alpha', '阿尔法', 'alpha_desk')
    flushSeeds()
    render(<ModePanelBadge mode="alpha" />)

    const badge = screen.getByTestId('mode-badge-alpha')
    expect(badge).toHaveTextContent('阿尔法')
    expect(badge).toHaveTextContent('⭐')

    fireEvent.click(badge)
    const tabs = useLayoutModeStore.getState().workspaceTabs
    expect(tabs).toHaveLength(1)
    expect(tabs[0].id).toBe('ws-plugin-alpha_desk')
  })

  it('无 mode 键消费方守卫之外的兜底：查不到映射 → 不渲染（零 mode 零渲染）', () => {
    render(<ModePanelBadge mode="ghost" />)
    expect(screen.queryByTestId('mode-badge-ghost')).not.toBeInTheDocument()
  })
})
