/** cost_control 成本卡声明契约测试（B1：预算/用量卡并入监控页，2026-08-18 合并） */
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

describe('cost 卡声明（B1 合并后）', () => {
  it('三张卡存在且并入 monitoring 空间；不独占侧边栏页面（pages 为空）', () => {
    const widgets = contributionRegistry.getAllWidgets().map((w) => w.id)
    expect(widgets).toContain('budget_card')
    expect(widgets).toContain('usage_daily_card')
    expect(widgets).toContain('usage_monthly_card')
    // 合并拍板：成本卡并入监控页（space=monitoring），不再声明独立 activity-bar 页
    const costWidgets = contributionRegistry
      .getAllWidgets()
      .filter((w) => ['budget_card', 'usage_daily_card', 'usage_monthly_card'].includes(w.id))
    expect(costWidgets.every((w) => w.space === 'monitoring')).toBe(true)
    const pages = contributionRegistry.getPages()
    expect(pages.find((p) => p.id === 'cost_dashboard')).toBeUndefined()
  })

  it('预算卡：datasourceUri + valueKey=usage_percent 渲染百分比（monitoring 空间）', async () => {
    apiGet.mockImplementation((url: string) => {
      if (url === '/ext/cost_control/budget/status') {
        return Promise.resolve({ data: { scope: 'global', usage_percent: 70, used: 700, limit: 1000 } })
      }
      if (url === '/ext/cost_control/usage/statistics') {
        return Promise.resolve({ data: { global_stats: { daily_tokens: 13513, monthly_tokens: 25117 } } })
      }
      return Promise.resolve({ data: {} })
    })
    render(<WidgetStage space="monitoring" />)
    await waitFor(() => expect(screen.getByText('70')).toBeInTheDocument())
    await waitFor(() => expect(screen.getByText('13513')).toBeInTheDocument())
    await waitFor(() => expect(screen.getByText('25117')).toBeInTheDocument())
  })

  it('端点不可用 → 降级不崩（后端 budget/status 未上线时）', async () => {
    apiGet.mockRejectedValue(new Error('404'))
    render(<WidgetStage space="monitoring" />)
    await waitFor(() =>
      expect(screen.queryByText('暂无图表数据')).not.toBeInTheDocument(),
    )
  })
})
