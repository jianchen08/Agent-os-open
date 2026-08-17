/**
 * PluginConfigEditor 类型化表单分支测试（widget 化 T1）
 *
 * 验收口径：config_files 带 fields 的 YAML 条目在设置页渲染类型化 RJSF 表单
 * （而非 KV 树）；提交按字段路径写回、未声明键原样保留；无 fields 时 KV 兜底。
 * env target 分支（GAP-4 密钥表单）已有行为不在本测试范围。
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import React from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { PluginConfigEditor } from '../PluginConfigEditor'
import { contributionRegistry } from '@/services/schema/ContributionRegistry'

const getPluginConfigFile = vi.fn()
const savePluginConfigFile = vi.fn()

vi.mock('@/services/api/pluginConfig', () => ({
  getPluginConfigFile: (...args: unknown[]) => getPluginConfigFile(...args),
  savePluginConfigFile: (...args: unknown[]) => savePluginConfigFile(...args),
  isPluginConfigConflict: () => false,
}))

/** 提交动作：直接派发 form submit（jsdom click 不重放 submit 事件） */
const submitForm = () => fireEvent.submit(document.querySelector('form')!)

function seedRegistry(fields?: unknown[]) {
  contributionRegistry.loadFromSchema({
    plugin_configs: [
      {
        plugin_id: 'pipeline_llm_core',
        plugin_name: 'LLM Core',
        config_files: [
          {
            id: 'embedding',
            path: 'config/models/embedding.yaml',
            label: '向量模型配置',
            ...(fields ? { fields } : {}),
          },
        ],
      },
    ],
  })
}

const configYaml = {
  embedding: { enabled: false, default_provider: 'zhipu', default_model: 'embedding-3' },
  // 未声明键：类型化表单提交后必须原样保留
  extra_section: { keep: true },
}

beforeEach(() => {
  getPluginConfigFile.mockReset()
  savePluginConfigFile.mockReset()
  getPluginConfigFile.mockResolvedValue({
    data: { data: structuredClone(configYaml) },
    etag: 'etag-1',
  })
  savePluginConfigFile.mockResolvedValue({ etag: 'etag-2' })
})

describe('T1：fields → 类型化表单', () => {
  beforeEach(() => {
    seedRegistry([
      { name: 'embedding.enabled', type: 'toggle', label: '启用向量模型' },
      { name: 'embedding.default_provider', type: 'string', label: '默认提供商' },
      { name: 'embedding.default_model', type: 'string', label: '默认模型' },
    ])
  })

  it('渲染类型化表单（初值按点号路径抽取），KV 树不出现', async () => {
    render(
      <PluginConfigEditor
        pluginId="pipeline_llm_core"
        fileId="embedding"
        title="向量模型配置"
      />,
    )
    const provider = await screen.findByLabelText('默认提供商')
    expect(provider).toHaveValue('zhipu')
    expect(screen.getByLabelText('默认模型')).toHaveValue('embedding-3')
    expect(screen.getByRole('switch')).toBeInTheDocument()
    // KV 兜底控件不出现
    expect(screen.queryByText('添加自定义字段')).not.toBeInTheDocument()
  })

  it('提交按路径写回，未声明键保留（PUT 全量配置 + ETag）', async () => {
    render(
      <PluginConfigEditor
        pluginId="pipeline_llm_core"
        fileId="embedding"
        title="向量模型配置"
      />,
    )
    fireEvent.change(await screen.findByLabelText('默认提供商'), {
      target: { value: 'openai' },
    })
    submitForm()
    await waitFor(() => expect(savePluginConfigFile).toHaveBeenCalled())
    const [pluginId, fileId, payload, etag] = savePluginConfigFile.mock.calls[0]
    expect(pluginId).toBe('pipeline_llm_core')
    expect(fileId).toBe('embedding')
    expect(etag).toBe('etag-1')
    expect(payload).toEqual({
      embedding: {
        enabled: false,
        default_provider: 'openai',
        default_model: 'embedding-3',
      },
      extra_section: { keep: true },
    })
  })

  it('可切回原始 KV 编辑逃生口', async () => {
    render(
      <PluginConfigEditor
        pluginId="pipeline_llm_core"
        fileId="embedding"
        title="向量模型配置"
      />,
    )
    fireEvent.click(await screen.findByText('原始 KV 编辑（fields 未覆盖的键）'))
    expect(await screen.findByText('添加自定义字段')).toBeInTheDocument()
    expect(screen.getByText('← 返回类型化表单')).toBeInTheDocument()
  })
})

describe('T1：无 fields → KV 兜底（既有行为保持）', () => {
  it('无 fields 声明渲染 KV 树', async () => {
    seedRegistry(undefined)
    render(
      <PluginConfigEditor
        pluginId="pipeline_llm_core"
        fileId="embedding"
        title="向量模型配置"
      />,
    )
    expect(await screen.findByText('添加自定义字段')).toBeInTheDocument()
    expect(screen.queryByText('原始 KV 编辑（fields 未覆盖的键）')).not.toBeInTheDocument()
  })
})
