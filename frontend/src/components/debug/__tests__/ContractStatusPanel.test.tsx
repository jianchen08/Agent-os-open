/** 契约状态面板测试（闸2·观测前端，契约校验方案配合） */
import { render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import React from 'react'
import { ContractStatusPanel, contractRedLight, parseContractStatus } from '../ContractStatusPanel'

const apiGet = vi.fn()
vi.mock('@/services/api/client', () => ({
  default: Object.assign(
    () => Promise.resolve({ data: {} }),
    { get: (...args: unknown[]) => apiGet(...args) },
  ),
}))

const okStatus = {
  plugin_id: 'approval_service',
  enabled: true,
  gates: {
    manifest_schema_valid: true,
    dep_ok: true,
    g2_consistency: 'ok',
    smoke_result: 'ok',
    render_decl_valid: 'ok',
    runtime_input_violations: 0,
    runtime_output_violations: 0,
  },
}

const sanitizedStatus = {
  plugin_id: 'task_manage_tool',
  enabled: true,
  gates: {
    manifest_schema_valid: true,
    dep_ok: true,
    g2_consistency: 'sanitized',
    smoke_result: 'skipped',
    render_decl_valid: 'n/a',
    runtime_input_violations: 0,
    runtime_output_violations: 0,
    last_error: '声明与实现不一致，剔除工具（需修改插件）: task_manage',
    rejected_tools: ['task_manage'],
    sanitized: {
      rejected_tools: ['task_manage'],
      tools_before: 1,
      tools_after: 0,
      reason: 'G2 声明↔实现一致性复核失败，剔除工具后按净化 manifest 重注册',
      sanitized_ts: 1756339200000,
    },
  },
}

beforeEach(() => {
  apiGet.mockReset()
})

describe('contractRedLight / parseContractStatus', () => {
  it('红灯判定：drift/failed/invalid/false 任一即红', () => {
    expect(contractRedLight({ plugin_id: 'a', gates: { g2_consistency: 'drift' } })).toBe(true)
    expect(contractRedLight({ plugin_id: 'a', gates: { smoke_result: 'failed' } })).toBe(true)
    expect(contractRedLight({ plugin_id: 'a', gates: { manifest_schema_valid: false } })).toBe(true)
    expect(contractRedLight(okStatus)).toBe(false)
  })
  it('红灯判定含 sanitized（净化=契约异常，ADR 2026-08-28）', () => {
    expect(contractRedLight({ plugin_id: 'a', gates: { g2_consistency: 'sanitized' } })).toBe(true)
  })
  it('解析兼容 数组 / {plugins} / {items}', () => {
    expect(parseContractStatus([okStatus])).toHaveLength(1)
    expect(parseContractStatus({ plugins: [okStatus] })).toHaveLength(1)
    expect(parseContractStatus({ items: [okStatus] })).toHaveLength(1)
    expect(parseContractStatus(null)).toHaveLength(0)
  })
})

describe('ContractStatusPanel', () => {
  it('端点返回 → 渲染绿灯/红灯行与关键闸', async () => {
    apiGet.mockResolvedValue({
      data: [okStatus, { plugin_id: 'bad', gates: { g2_consistency: 'drift', runtime_output_violations: 2 } }],
    })
    render(<ContractStatusPanel />)
    await waitFor(() => expect(screen.getByTestId('contract-status-approval_service')).toBeInTheDocument())
    expect(screen.getByTestId('contract-light-approval_service').className).toContain('status-success')
    expect(screen.getByTestId('contract-light-bad').className).toContain('status-error')
    expect(screen.getByText('出参:2')).toBeInTheDocument()
  })
  it('端点不可用 → 降级提示不崩', async () => {
    apiGet.mockRejectedValue(new Error('404'))
    render(<ContractStatusPanel />)
    await waitFor(() => expect(screen.getByRole('alert')).toBeInTheDocument())
  })

  it('sanitized 行内显式展示净化证据（红灯 + 工具数前后 + 被剔除工具）', async () => {
    apiGet.mockResolvedValue({ data: [sanitizedStatus] })
    render(<ContractStatusPanel />)
    await waitFor(() =>
      expect(screen.getByTestId('contract-status-task_manage_tool')).toBeInTheDocument(),
    )
    expect(screen.getByTestId('contract-light-task_manage_tool').className).toContain('status-error')
    const detail = screen.getByTestId('contract-detail-task_manage_tool')
    expect(detail).toHaveTextContent('已净化/工具被剔除')
    expect(detail).toHaveTextContent('1→0')
    expect(detail).toHaveTextContent('task_manage')
  })

  it('注册表↔磁盘清单差异显式展示（ADR 2026-08-28 决策3）', async () => {
    apiGet.mockResolvedValue({
      data: [
        {
          plugin_id: 'hot_edit_tool',
          gates: {
            g2_consistency: 'not_covered',
            registry_disk_diffs: [
              { kind: 'missing_tool', tool: 't1', detail: '注册表 manifest 缺少磁盘声明的工具' },
            ],
          },
        },
      ],
    })
    render(<ContractStatusPanel />)
    await waitFor(() =>
      expect(screen.getByTestId('contract-detail-hot_edit_tool')).toBeInTheDocument(),
    )
    const detail = screen.getByTestId('contract-detail-hot_edit_tool')
    expect(detail).toHaveTextContent('注册表与磁盘清单不一致')
    expect(detail).toHaveTextContent('missing_tool')
    expect(detail).toHaveTextContent('t1')
  })

  it('ok 且无差异的插件不渲染明细块', async () => {
    apiGet.mockResolvedValue({ data: [okStatus] })
    render(<ContractStatusPanel />)
    await waitFor(() =>
      expect(screen.getByTestId('contract-status-approval_service')).toBeInTheDocument(),
    )
    expect(screen.queryByTestId('contract-detail-approval_service')).not.toBeInTheDocument()
  })
})
