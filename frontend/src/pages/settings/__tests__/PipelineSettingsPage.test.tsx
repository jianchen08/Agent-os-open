/** @feature FP-0.2.四/五 fallback-audit FE项 管道配置加载失败禁存 @ci frontend-test */
/**
 * PipelineSettingsPage 组件测试（0.2 多循环体可视化版）
 *
 * 覆盖管道配置设置页核心功能：
 * - 加载：getPipelineConfig('autonomous') + fetchPipelinePluginCatalog()
 * - 可视化渲染：循环体卡片 / step 卡片 / 插件组合 chips / 动态模板 chip / 路由规则
 * - 编辑：移除插件 chip、添加插件（选择弹窗）、保存 PUT 透传 raw data
 * - 视图切换：可视化 ↔ 源码；非 0.2 格式自动落源码视图
 * - 加载失败 / 保存失败反馈、embedded 模式
 *
 * 测试策略：Mock 仅外部依赖（API 层 + UI 基础组件），组件真实渲染。
 */

import { screen, waitFor, fireEvent, within } from '@testing-library/react'
import React from 'react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { renderWithProviders } from '@/test/renderWithProviders'
import { PipelineSettingsPage } from '../PipelineSettingsPage'

// ── Mock API 层 ──
const mockGetPipelineConfig = vi.fn()
const mockSavePipelineConfig = vi.fn()
const mockFetchCatalog = vi.fn()

vi.mock('@/services/api/pipelineConfig', () => ({
  getPipelineConfig: (...args: unknown[]) => mockGetPipelineConfig(...args),
  savePipelineConfig: (...args: unknown[]) => mockSavePipelineConfig(...args),
}))

vi.mock('@/services/api/pipelines', () => ({
  fetchPipelinePluginCatalog: (...args: unknown[]) => mockFetchCatalog(...args),
}))

// ── Mock UI 基础组件（简化依赖，聚焦业务行为）──
vi.mock('@/components/ui/button', () => ({
  Button: ({ children, onClick, disabled, ...props }: any) => (
    <button onClick={onClick} disabled={disabled} data-testid="save-btn" {...props}>
      {children}
    </button>
  ),
}))

vi.mock('@/components/ui/input', () => ({
  Input: ({ value, onChange, type, ...props }: any) => (
    <input value={value ?? ''} onChange={onChange} type={type} data-testid="input" {...props} />
  ),
}))

vi.mock('@/components/ui/sonner', () => ({
  toast: {
    error: vi.fn(),
    success: vi.fn(),
  },
}))

/** 样例管道配置（0.2 多循环体格式，结构对齐 autonomous.yaml） */
const sampleV2 = {
  name: 'autonomous',
  loop_bodies: [
    { id: 'init', steps: [{ id: 'init', steps: ['pipeline_workspace_lifecycle'] }] },
    {
      id: 'main',
      loop_config: { enabled: true, max_iterations: -1 },
      steps: [
        {
          id: 'prepare',
          steps: ['pipeline_tool_schema', 'pipeline_param_inject'],
          context: { agent_id: '{{state.agent_id}}' },
        },
        {
          id: 'core',
          steps: ['{{state.core_plugin}}', 'pipeline_spill_guard'],
          context: { temperature: 0.7 },
        },
        {
          id: 'post',
          steps: ['pipeline_track', 'pipeline_result_format'],
          routes: [
            {
              when: 'raw_tool_calls != [] and raw_tool_calls != None',
              then: {
                next: 'loop',
                set: { core_type: 'tool_execute', core_plugin: 'pipeline_tool_core' },
              },
            },
            { when: 'True', then: { next: 'end' } },
          ],
        },
      ],
    },
    {
      id: 'exit',
      run_on_error: true,
      steps: [{ id: 'exit', steps: ['pipeline_workspace_lifecycle'] }],
    },
  ],
}

/** 样例插件目录（join 产物） */
const sampleCatalog = [
  {
    id: 'pipeline_tool_schema',
    name: 'Tool Schema',
    role: 'input',
    hostType: 'sidecar',
    version: '1.0.0',
    enabled: true,
    configFiles: [],
  },
  {
    id: 'pipeline_param_inject',
    name: 'Param Inject',
    role: 'input',
    hostType: 'sidecar',
    version: '1.0.0',
    enabled: true,
    configFiles: [],
  },
  {
    id: 'pipeline_spill_guard',
    name: 'Spill Guard',
    role: 'core',
    hostType: 'in_process',
    version: '1.0.0',
    enabled: true,
    configFiles: [],
  },
  {
    id: 'pipeline_track',
    name: 'Track',
    role: 'output',
    hostType: 'sidecar',
    version: '1.0.0',
    enabled: true,
    configFiles: [],
  },
  {
    id: 'pipeline_result_format',
    name: 'Result Format',
    role: 'output',
    hostType: 'sidecar',
    version: '1.0.0',
    enabled: false,
    configFiles: [{ id: 'config', label: '配置', path: 'result_format.yaml' }],
  },
]

/** 0.1 扁平格式（非 0.2，用于自动落源码视图断言） */
const sampleV1 = {
  name: 'agentos_agent',
  task_worker: { pipeline_timeout: 7200 },
  input_routes: [{ name: 'tool_execute', plugins: ['tool_schema'] }],
  output_routes: [],
}

async function renderLoaded(
  data: Record<string, unknown> = sampleV2,
  catalog: unknown[] = sampleCatalog,
) {
  mockGetPipelineConfig.mockResolvedValue({ name: 'autonomous', data, etag: 'e1' })
  mockFetchCatalog.mockResolvedValue(catalog)
  renderWithProviders(<PipelineSettingsPage />)
  // 等加载占位消失（isLoading=false 与 config 就位同批 effect，query 版比直连
  // promise 多一轮状态机周期，等按钮会提前命中）
  await waitFor(() => {
    expect(screen.queryByText(/加载配置/)).not.toBeInTheDocument()
  })
}

describe('PipelineSettingsPage', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockFetchCatalog.mockResolvedValue([])
  })

  describe('加载', () => {
    it('初始渲染显示加载中', () => {
      mockGetPipelineConfig.mockReturnValue(new Promise(() => {}))
      renderWithProviders(<PipelineSettingsPage />)

      expect(screen.getByText(/加载配置/)).toBeInTheDocument()
    })

    it('加载 autonomous 配置与插件目录', () => {
      mockGetPipelineConfig.mockReturnValue(new Promise(() => {}))
      renderWithProviders(<PipelineSettingsPage />)

      expect(mockGetPipelineConfig).toHaveBeenCalledWith('autonomous')
      expect(mockFetchCatalog).toHaveBeenCalled()
    })

    it('插件目录获取失败不阻塞编辑（显示降级提示）', async () => {
      mockGetPipelineConfig.mockResolvedValue({ name: 'autonomous', data: sampleV2, etag: 'e1' })
      mockFetchCatalog.mockRejectedValue(new Error('catalog down'))
      renderWithProviders(<PipelineSettingsPage />)

      await waitFor(() => {
        expect(screen.getByText(/插件目录获取失败/)).toBeInTheDocument()
      })
      expect(screen.getByText('保存配置')).toBeInTheDocument()
    })
  })

  describe('可视化渲染', () => {
    it('渲染循环体卡片（init/main/exit）与语义徽标', async () => {
      await renderLoaded()

      expect(screen.getByTestId('loop-body-init')).toBeInTheDocument()
      expect(screen.getByTestId('loop-body-main')).toBeInTheDocument()
      expect(screen.getByTestId('loop-body-exit')).toBeInTheDocument()
      // main 循环体（∞=无限迭代）/ exit 错误必经
      expect(screen.getByText('循环体', { exact: true })).toBeInTheDocument()
      expect(screen.getByText('∞', { exact: true })).toBeInTheDocument()
      expect(screen.getByText('错误必经')).toBeInTheDocument()
      expect(screen.getAllByText(/顺序推进/).length).toBeGreaterThan(0)
    })

    it('渲染 step 卡片与插件组合 chips（短名 + role + 动态模板）', async () => {
      await renderLoaded()

      expect(screen.getByTestId('step-node-prepare')).toBeInTheDocument()
      expect(screen.getByTestId('step-node-core')).toBeInTheDocument()
      expect(screen.getByTestId('step-node-post')).toBeInTheDocument()

      // 插件短名（去 pipeline_ 前缀）
      expect(screen.getByText('tool_schema')).toBeInTheDocument()
      expect(screen.getByText('param_inject')).toBeInTheDocument()
      expect(screen.getByText('spill_guard')).toBeInTheDocument()
      // 动态模板引用原样展示
      expect(screen.getByText('{{state.core_plugin}}')).toBeInTheDocument()
    })

    it('渲染路由规则（when 条件与 set 字段值）', async () => {
      await renderLoaded()

      expect(
        screen.getByLabelText('规则 1 when 条件'),
      ).toHaveValue('raw_tool_calls != [] and raw_tool_calls != None')
      expect(screen.getByDisplayValue('tool_execute')).toBeInTheDocument()
      expect(screen.getByDisplayValue('pipeline_tool_core')).toBeInTheDocument()
    })

    it('context 键值渲染', async () => {
      await renderLoaded()

      expect(screen.getByLabelText('agent_id 键名')).toBeInTheDocument()
      expect(screen.getByDisplayValue('{{state.agent_id}}')).toBeInTheDocument()
    })
  })

  describe('编辑与保存', () => {
    it('移除插件 chip 后保存透传更新后的 data', async () => {
      await renderLoaded()

      // prepare step 组合的第一个 chip（tool_schema）移除
      const prepareNode = screen.getByTestId('step-node-prepare')
      const removeButtons = within(prepareNode).getAllByLabelText('移除')
      fireEvent.click(removeButtons[0])

      fireEvent.click(screen.getByTestId('save-btn'))

      await waitFor(() => {
        expect(mockSavePipelineConfig).toHaveBeenCalledWith(
          'autonomous',
          expect.objectContaining({
            loop_bodies: expect.any(Array),
          }),
        )
      })
      const saved = mockSavePipelineConfig.mock.calls[0][1] as typeof sampleV2
      const prepare = saved.loop_bodies[1].steps[0]
      expect(prepare.steps).toEqual(['pipeline_param_inject'])
    })

    it('通过选择弹窗向 step 添加插件', async () => {
      await renderLoaded()

      fireEvent.click(screen.getByLabelText('向 step prepare 添加插件'))
      // 弹窗内点击目录中未被排除的插件
      fireEvent.click(await screen.findByLabelText('添加 pipeline_track'))

      // prepare 组合出现新 chip（短名 track）
      await waitFor(() => {
        expect(screen.getByTestId('step-node-prepare').textContent).toContain('track')
      })

      // 保存后 data 中 prepare.steps 追加
      fireEvent.click(screen.getByTestId('save-btn'))
      await waitFor(() => {
        expect(mockSavePipelineConfig).toHaveBeenCalled()
      })
      const saved = mockSavePipelineConfig.mock.calls[0][1] as typeof sampleV2
      expect(saved.loop_bodies[1].steps[0].steps).toEqual([
        'pipeline_tool_schema',
        'pipeline_param_inject',
        'pipeline_track',
      ])
    })

    it('保存成功显示已保存；失败显示保存失败', async () => {
      mockSavePipelineConfig.mockResolvedValue({ name: 'autonomous', etag: 'e2' })
      await renderLoaded()

      fireEvent.click(screen.getByTestId('save-btn'))
      await waitFor(() => {
        expect(screen.getByText('已保存')).toBeInTheDocument()
      })

      mockSavePipelineConfig.mockRejectedValue(new Error('disk full'))
      fireEvent.click(screen.getByTestId('save-btn'))
      await waitFor(() => {
        expect(screen.getByText('保存失败')).toBeInTheDocument()
      })
    })
  })

  describe('视图切换', () => {
    it('切换到源码视图渲染 ConfigObject 通用表单', async () => {
      await renderLoaded()

      fireEvent.click(screen.getByRole('tab', { name: /源码/ }))

      // loop_bodies 数组渲染为 JSON textarea（内容含插件引用）
      expect(await screen.findByDisplayValue(/pipeline_tool_schema/)).toBeInTheDocument()
      // 可视化编辑器已卸载
      expect(screen.queryByTestId('pipeline-flow-editor')).not.toBeInTheDocument()
    })

    it('非 0.2 格式配置自动落源码视图并提示', async () => {
      await renderLoaded(sampleV1)

      expect(screen.getByText(/非 0\.2 多循环体格式/)).toBeInTheDocument()
      expect(screen.queryByTestId('pipeline-flow-editor')).not.toBeInTheDocument()
      expect(screen.getByDisplayValue(/tool_schema/)).toBeInTheDocument()
    })
  })

  describe('加载失败', () => {
    it('显示错误提示且禁存（保存禁用、编辑区不渲染）——防止空对象写回 autonomous.yaml', async () => {
      mockGetPipelineConfig.mockRejectedValue(new Error('Network error'))
      renderWithProviders(<PipelineSettingsPage />)

      await waitFor(() => {
        expect(screen.getByText('无法加载配置')).toBeInTheDocument()
      })
      // 加载失败 = 只读：保存按钮禁用，可视化/源码编辑区均不渲染
      expect(screen.getByTestId('save-btn')).toBeDisabled()
      expect(mockSavePipelineConfig).not.toHaveBeenCalled()
      expect(screen.queryByTestId('pipeline-flow-editor')).not.toBeInTheDocument()
      expect(screen.queryByRole('form', { name: '管道配置表单' })).not.toBeInTheDocument()
    })
  })

  describe('embedded 模式', () => {
    it('embedded 时不渲染独立页面头（返回链接）', async () => {
      mockGetPipelineConfig.mockResolvedValue({ name: 'autonomous', data: sampleV2, etag: 'e1' })
      renderWithProviders(<PipelineSettingsPage embedded />)

      await waitFor(() => {
        expect(screen.getByText('保存配置')).toBeInTheDocument()
      })

      expect(screen.queryByText('← 返回设置')).not.toBeInTheDocument()
    })
  })
})
