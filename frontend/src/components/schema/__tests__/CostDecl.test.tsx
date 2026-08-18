/** cost_control 成本看板声明契约测试（B1：预算/用量卡替代 CostDashboardWidget） */
import { render, screen, waitFor } from '@testing-library/react'
import fs from 'node:fs'
import path from 'node:path'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import React from 'react'

import { WidgetStage } from '@/components/schema/widgets/WidgetStage'
import { contributionRegistry } from '@/services/schema/ContributionRegistry'
import { initializeWidgets } from '@/services/schema/registerWidgets'

const apiGet = vi.fn()
vi.mock('@/services/api/client', () => ({
  default: Object.assign(
    () => Promise.resolve({ data: {} }),
    { get: (...args: unknown[]) => apiGet(...args) },
  ),
}))

const PLUGIN = JSON.parse(
  fs.readFileSync(
    path.resolve(__dirname, '../../../../../plugins/shared/system/cost_control/plugin.json'),
    'utf-8',
  ),
) as { id: string; ui_schema?: unknown; contributes?: unknown }

beforeEach(() => {
  vi.clearAllMocks()
  apiGet.mockReset()
  apiGet.mockResolvedValue({ data: {} })
  initializeWidgets()
  contributionRegistry.loadFromSchema({
    agents: [{ id: PLUGIN.id, ui_schema: PLUGIN.ui_schema }],
    plugin_configs: [],
    plugin_contributes: [
      { plugin_id: PLUGIN.id, plugin_name: 'cost', contributes: PLUGIN.contributes },
    ],
  })
})

describe('cost 看板声明（B1）', () => {
  it('声明存在：三张卡 + /cost 页指向 widget_stage', () => {
    const ids = contributionRegistry.getAllWidgets().map((w) => w.id)
    expect(ids).toContain('budget_card')
    expect(ids).toContain('usage_daily_card')
    expect(ids).toContain('usage_monthly_card')
    const page = contributionRegistry.getPages().find((p) => p.id === 'cost_dashboard')
    expect(page?.widget).toBe('widget_stage')
  })

  it('预算卡：datasourceUri + valueKey=usage_percent 渲染百分比', async () => {
    apiGet.mockImplementation((url: string) => {
      if (url === '/ext/cost_control/budget/status') {
        return Promise.resolve({ data: { scope: 'global', usage_percent: 70, used: 700, limit: 1000 } })
      }
      if (url === '/ext/cost_control/usage/statistics') {
        return Promise.resolve({ data: { global_stats: { daily_usage_percent: 62, monthly_usage_percent: 41 } } })
      }
      return Promise.resolve({ data: {} })
    })
    render(<WidgetStage space="cost" />)
    await waitFor(() => expect(screen.getByText('70%')).toBeInTheDocument())
    await waitFor(() => expect(screen.getByText('62%')).toBeInTheDocument())
    await waitFor(() => expect(screen.getByText('41%')).toBeInTheDocument())
  })

  it('端点不可用 → 降级不崩（后端 budget/status 未上线时）', async () => {
    apiGet.mockRejectedValue(new Error('404'))
    render(<WidgetStage space="cost" />)
    await waitFor(() =>
      expect(screen.queryByText('暂无图表数据')).not.toBeInTheDocument(),
    )
  })
})
