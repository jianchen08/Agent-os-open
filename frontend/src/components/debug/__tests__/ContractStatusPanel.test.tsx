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
})
