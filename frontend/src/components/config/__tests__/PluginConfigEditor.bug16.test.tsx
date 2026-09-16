/** @feature FP-T12 前端适配 | @ci: frontend-test */
/**
 * BUG-16 回归 — 现象A 页面级：cost_control 类型化表单保存链路
 *
 * 复刻真实页面数据链：contributionRegistry(seed cost fields) → GET 配置
 * （90/100/70/1000000 等实值）→ 改「日 token 上限」→ 点保存 → PUT 出网带新值。
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import React from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { contributionRegistry } from '@/services/schema/ContributionRegistry'
import { PluginConfigEditor } from '../PluginConfigEditor'

const getPluginConfigFile = vi.fn()
const savePluginConfigFile = vi.fn()

vi.mock('@/services/api/pluginConfig', () => ({
  getPluginConfigFile: (...args: unknown[]) => getPluginConfigFile(...args),
  savePluginConfigFile: (...args: unknown[]) => savePluginConfigFile(...args),
  isPluginConfigConflict: () => false,
}))

/** cost_control plugin.json cost 配置文件 fields（alerts 三个 slider 无 step） */
const COST_FIELDS = [
  { name: 'enabled', type: 'toggle', label: '启用成本控制' },
  { name: 'global_config.daily_token_limit', type: 'number', label: '日 token 上限', min: 0 },
  { name: 'global_config.monthly_token_limit', type: 'number', label: '月 token 上限', min: 0 },
  { name: 'global_config.per_task_token_limit', type: 'number', label: '单任务 token 上限', min: 0 },
  { name: 'global_config.per_session_token_limit', type: 'number', label: '单会话 token 上限', min: 0 },
  { name: 'alerts.warning_threshold', type: 'slider', label: '预警阈值（%）', min: 0, max: 100 },
  { name: 'alerts.critical_threshold', type: 'slider', label: '严重阈值（%）', min: 0, max: 100 },
  { name: 'alerts.exhausted_threshold', type: 'slider', label: '耗尽阈值（%）', min: 0, max: 100 },
  { name: 'protection.auto_save_at_warning', type: 'toggle', label: '预警时自动保存' },
  { name: 'protection.auto_pause_at_critical', type: 'toggle', label: '严重时自动暂停' },
  { name: 'protection.auto_stop_at_exhausted', type: 'toggle', label: '耗尽时自动停止' },
]

/** 磁盘实值（缺陷单实测：90/100/70/1000000/30000000/500000/200000） */
const COST_CONFIG = {
  enabled: true,
  global_config: {
    daily_token_limit: 1000000,
    monthly_token_limit: 30000000,
    per_task_token_limit: 500000,
    per_session_token_limit: 200000,
  },
  alerts: { warning_threshold: 90, critical_threshold: 100, exhausted_threshold: 70 },
  protection: { auto_save_at_warning: true, auto_pause_at_critical: true, auto_stop_at_exhausted: false },
}

beforeEach(() => {
  getPluginConfigFile.mockReset()
  savePluginConfigFile.mockReset()
  getPluginConfigFile.mockResolvedValue({
    data: { data: structuredClone(COST_CONFIG) },
    etag: 'etag-cost-1',
  })
  savePluginConfigFile.mockResolvedValue({ etag: 'etag-cost-2' })
  contributionRegistry.loadFromSchema({
    plugin_configs: [
      {
        plugin_id: 'cost_control',
        plugin_name: 'Cost Control Service',
        config_files: [
          { id: 'cost', path: 'config/plugins/cost_control/cost_control.yaml', label: '成本控制配置', fields: COST_FIELDS },
        ],
      },
    ],
  })
})

describe('BUG-16 现象A — cost 页类型化表单保存链路', () => {
  it('改日 token 上限后保存：PUT 出网带新值 + 未声明键外全量写回', async () => {
    render(<PluginConfigEditor pluginId="cost_control" fileId="cost" title="成本控制配置" />)
    const limitInput = await screen.findByLabelText('日 token 上限')
    expect(limitInput).toHaveValue('1000000')
    fireEvent.change(limitInput, { target: { value: '1000001' } })
    fireEvent.submit(document.querySelector('form')!)
    await waitFor(() => expect(savePluginConfigFile).toHaveBeenCalled())
    const [, , payload] = savePluginConfigFile.mock.calls[0]
    expect(payload).toMatchObject({
      enabled: true,
      global_config: {
        daily_token_limit: 1000001,
        monthly_token_limit: 30000000,
        per_task_token_limit: 500000,
        per_session_token_limit: 200000,
      },
      alerts: { warning_threshold: 90, critical_threshold: 100, exhausted_threshold: 70 },
    })
    // 「已保存」反馈可见
    expect(await screen.findByText('已保存')).toBeInTheDocument()
  })
})
