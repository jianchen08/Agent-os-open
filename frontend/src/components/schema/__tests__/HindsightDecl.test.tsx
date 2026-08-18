/** hindsight_memory 记忆页声明契约测试（B3 收口：前端接成熟 Hindsight 数据） */
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
    path.resolve(__dirname, '../../../../../plugins/shared/system/hindsight_memory/plugin.json'),
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
      { plugin_id: PLUGIN.id, plugin_name: 'hindsight', contributes: PLUGIN.contributes },
    ],
  })
})

describe('hindsight 记忆页声明（B3）', () => {
  it('声明存在：memory 页 → widget_stage + 表/状态卡', () => {
    const page = contributionRegistry.getPages().find((p) => p.id === 'memory')
    expect(page?.widget).toBe('widget_stage')
    const ids = contributionRegistry.getAllWidgets().map((w) => w.id)
    expect(ids).toContain('memory_table')
    expect(ids).toContain('memory_stats')
  })

  it('渲染：Hindsight recall 结果进表 + 状态卡后端名', async () => {
    apiGet.mockImplementation((url: string) => {
      if (url.includes('/recall')) {
        return Promise.resolve({
          data: { results: [{ id: 'm1', content: '用户偏好：简洁回复' }, { id: 'm2', content: '项目约定：Rust 微内核' }], total: 2 },
        })
      }
      if (url.includes('/stats')) {
        return Promise.resolve({ data: { backend: 'hindsight', bank_id: 'default' } })
      }
      return Promise.resolve({ data: {} })
    })
    render(<WidgetStage space="memory" />)
    await waitFor(() => expect(screen.getByText('用户偏好：简洁回复')).toBeInTheDocument())
    expect(screen.getByText('项目约定：Rust 微内核')).toBeInTheDocument()
    await waitFor(() => expect(screen.getByText('hindsight')).toBeInTheDocument())
  })
})
