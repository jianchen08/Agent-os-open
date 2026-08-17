/**
 * AgentConfigModal 测试
 *
 * 验证「加载 → 编辑 → 保存」闭环（对接后端 /api/v1/agents/{id}/config）：
 * - AC-1: 打开 Modal 后并行加载 schema 字段 + yaml 配置，字段以 yaml 值预填
 * - AC-2: 修改字段后保存 → PUT /api/v1/agents/{id}/config 收到新 yaml，onSaved 触发
 * - AC-3: 取消按钮关闭 Modal（不调 PUT）
 */

import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import React from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { UIInputFormField } from '@/types/schema'

// ── Mock API 模块（widget 化 T12：AgentConfigModal 走 FormWidget datasource，
//    apiClient.get 拉 schema/config，apiClient(...) 调用 PUT 写回）──
const apiGet = vi.fn()
const apiCall = vi.fn()
vi.mock('@/services/api/client', () => ({
  default: Object.assign(
    (...args: unknown[]) => apiCall(...args),
    { get: (...args: unknown[]) => apiGet(...args) },
  ),
}))

import { AgentConfigModal } from '@/components/agent/AgentConfigModal'

/** 模拟后端 GET /api/v1/agents/schema 返回的 12 字段 */
const SCHEMA_FIELDS: UIInputFormField[] = [
  { name: 'config_id', type: 'string', label: '配置ID', required: true },
  { name: 'name', type: 'string', label: '名称', required: true },
  { name: 'display_name', type: 'string', label: '显示名称' },
  { name: 'description', type: 'textarea', label: '描述' },
  {
    name: 'agent_type',
    type: 'select',
    label: '类型',
    options: [
      { label: '主控', value: 'main' },
      { label: '编排', value: 'orchestrator' },
      { label: '专用', value: 'specialized' },
      { label: '原子', value: 'atomic' },
      { label: '系统', value: 'system' },
    ],
  },
  {
    name: 'level',
    type: 'select',
    label: '层级',
    options: [
      { label: 'L1', value: 'L1' },
      { label: 'L2', value: 'L2' },
      { label: 'L3', value: 'L3' },
    ],
  },
  { name: 'model_tier', type: 'string', label: '模型档位' },
  { name: 'system_prompt', type: 'textarea', label: '系统提示词' },
  { name: 'tool_ids', type: 'multiselect', label: '工具' },
  { name: 'max_iterations', type: 'number', label: '最大迭代' },
  { name: 'timeout_seconds', type: 'number', label: '超时秒' },
  { name: 'tags', type: 'multiselect', label: '标签' },
]

/** 模拟后端 GET /api/v1/agents/{id}/config 返回的 yaml 原文 */
const SAMPLE_YAML = [
  '# 代码审查Agent',
  'config_id: code_reviewer_agent',
  'name: 代码审查专家',
  'agent_type: specialized',
  'level: L3',
  'max_iterations: 30',
  'timeout_seconds: 600',
  'tool_ids:',
  '- file_read',
  '- bash_execute',
  'tags:',
  '- code_review',
  '- quality',
  '',
].join('\n')

describe('AgentConfigModal', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    apiGet.mockImplementation((url: string) => {
      if (url.endsWith('/schema')) return Promise.resolve({ data: { fields: SCHEMA_FIELDS } })
      if (url.includes('/config')) return Promise.resolve({ data: { yaml: SAMPLE_YAML } })
      return Promise.reject(new Error(`unexpected: ${url}`))
    })
    apiCall.mockResolvedValue({ data: { success: true } })
  })

  it('AC-1: 打开后加载 schema 字段并以 yaml 值预填表单', async () => {
    render(
      <AgentConfigModal
        agent={{ id: 'code_reviewer_agent', name: '代码审查专家' }}
        isOpen
        onClose={vi.fn()}
      />,
    )

    await waitFor(() => {
      expect(apiGet).toHaveBeenCalledWith('/api/v1/agents/schema')
      expect(apiGet).toHaveBeenCalledWith('/api/v1/agents/code_reviewer_agent/config')
    })

    // 字段渲染（抽样）
    expect(await screen.findByLabelText(/^名称/)).toHaveValue('代码审查专家')
    expect(screen.getByLabelText(/配置ID/)).toHaveValue('code_reviewer_agent')
    expect(screen.getByLabelText(/最大迭代/)).toHaveValue('30')
  })

  it('AC-2: 编辑后保存 → PUT config 收到新 yaml，onSaved 触发', async () => {
    const onClose = vi.fn()
    const onSaved = vi.fn()
    render(
      <AgentConfigModal
        agent={{ id: 'code_reviewer_agent', name: '代码审查专家' }}
        isOpen
        onClose={onClose}
        onSaved={onSaved}
      />,
    )

    const nameInput = await screen.findByLabelText(/^名称/)
    fireEvent.change(nameInput, { target: { value: '审查专家 v2' } })
    // 模拟表单提交（等价点击保存按钮在真实浏览器触发的 submit；jsdom click 激活路径不重放）
    fireEvent.submit(document.querySelector('form')!)

    await waitFor(() => {
      expect(apiCall).toHaveBeenCalledTimes(1)
    })
    const cfg = apiCall.mock.calls[0][0] as { method: string; url: string; data: { yaml: string } }
    expect(cfg.method).toBe('PUT')
    expect(cfg.url).toBe('/api/v1/agents/code_reviewer_agent/config')
    expect(cfg.data.yaml).toContain('审查专家 v2')
    expect(cfg.data.yaml).toContain('config_id: code_reviewer_agent')
    expect(onSaved).toHaveBeenCalledTimes(1)
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('AC-3: 取消关闭 Modal，不触发 PUT', async () => {
    const onClose = vi.fn()
    render(
      <AgentConfigModal
        agent={{ id: 'code_reviewer_agent', name: '代码审查专家' }}
        isOpen
        onClose={onClose}
      />,
    )

    await screen.findByLabelText(/^名称/)
    fireEvent.click(screen.getByRole('button', { name: /取消/ }))

    expect(onClose).toHaveBeenCalledTimes(1)
    expect(apiCall).not.toHaveBeenCalled()
  })
})
