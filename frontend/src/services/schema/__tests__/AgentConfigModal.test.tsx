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

// ── Mock API 模块 ──
vi.mock('@/services/api/agents', () => ({
  getAgentSchema: vi.fn(),
  getAgentConfig: vi.fn(),
  putAgentConfig: vi.fn(),
}))

vi.mock('@/services/api/client', () => ({
  default: { get: vi.fn() },
}))

import { getAgentConfig, getAgentSchema, putAgentConfig } from '@/services/api/agents'

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
    vi.mocked(getAgentSchema).mockResolvedValue({ fields: SCHEMA_FIELDS })
    vi.mocked(getAgentConfig).mockResolvedValue({
      config_id: 'code_reviewer_agent',
      yaml: SAMPLE_YAML,
    })
    vi.mocked(putAgentConfig).mockResolvedValue({
      config_id: 'code_reviewer_agent',
      success: true,
      backup: 'code_reviewer_agent.yaml.bak',
    })
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
      expect(getAgentSchema).toHaveBeenCalledTimes(1)
      expect(getAgentConfig).toHaveBeenCalledWith('code_reviewer_agent')
    })

    // 字段渲染（抽样）
    expect(await screen.findByLabelText(/^名称/)).toHaveValue('代码审查专家')
    expect(screen.getByLabelText(/配置ID/)).toHaveValue('code_reviewer_agent')
    expect(screen.getByLabelText(/最大迭代/)).toHaveValue(30)
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
    fireEvent.click(screen.getByRole('button', { name: /保存/ }))

    await waitFor(() => {
      expect(putAgentConfig).toHaveBeenCalledTimes(1)
    })
    const [agentId, yaml] = vi.mocked(putAgentConfig).mock.calls[0]
    expect(agentId).toBe('code_reviewer_agent')
    expect(yaml).toContain('审查专家 v2')
    expect(yaml).toContain('config_id: code_reviewer_agent')
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
    expect(putAgentConfig).not.toHaveBeenCalled()
  })
})
