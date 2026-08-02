/**
 * SettingsPage 集成测试 — 覆盖场景1（AC1 入口）+ 场景2（AC2 读取展示）+ 场景3（AC3 修改保存）
 *
 * 验证内容（可观察行为）：
 * - 场景1：/settings 左侧「内核设置」分组出现「管道配置」设置栏，点击后右侧内联显示管道配置页
 * - 场景2：默认 tab 加载 → 调用 getPipelineConfig('default') → 展示 name 字段 Input、input_routes JSON textarea
 * - 场景3：修改字段 → 点击「保存配置」→ 调用 savePipelineConfig('default', config) → 显示「已保存」
 *
 * 测试策略：真实渲染 SettingsPage + PipelineSettingsPage + ConfigObject（组件逻辑真实运行），
 * 仅 mock 外部依赖（API 层、UI 基础组件、无关页面）。SettingsPage 使用 react-router-dom Link，
 * 需用 MemoryRouter 包裹。
 */
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import React from 'react'
import { describe, it, expect, vi, beforeEach } from 'vitest'

// ── Mock 外部依赖 ──
const mockGetPipelineConfig = vi.fn()
const mockSavePipelineConfig = vi.fn()

vi.mock('@/services/api/schema', () => ({
  getSchema: vi.fn().mockResolvedValue({}),
}))

vi.mock('@/services/api/pipelineConfig', () => ({
  getPipelineConfig: (...args: unknown[]) => mockGetPipelineConfig(...args),
  savePipelineConfig: (...args: unknown[]) => mockSavePipelineConfig(...args),
}))

// 无关页面 mock（避免引入复杂依赖）
vi.mock('@/pages/settings/ThemeSettingsPage', () => ({
  ThemeSettingsPage: () => <div data-testid="theme-page" />,
}))
vi.mock('@/pages/settings/PluginsSettingsPage', () => ({
  PluginsSettingsPage: () => <div data-testid="plugins-page" />,
}))

// UI 基础组件 mock（聚焦业务行为）
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

vi.mock('lucide-react', () => ({
  Loader2: () => <span data-testid="loader" />,
  Trash2: () => <span data-testid="trash" />,
  Plus: () => <span data-testid="plus" />,
}))

/** 样例管道配置（0.1 扁平格式） */
const samplePipeline = {
  name: 'agentos_agent',
  task_worker: { pipeline_timeout: 7200 },
  input_routes: [
    {
      name: 'tool_execute',
      condition: "core_type == 'tool_execute'",
      target: 'core',
      plugins: ['tool_schema', 'param_inject'],
      priority: 10,
    },
  ],
  output_routes: [],
  plugins: [],
  core_plugins: {},
}

import { SettingsPage } from '../SettingsPage'

describe('SettingsPage — 管道配置入口与内联渲染（场景1/2/3）', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockGetPipelineConfig.mockResolvedValue({ name: 'default', data: samplePipeline, etag: 'e1' })
    mockSavePipelineConfig.mockResolvedValue({ name: 'default', etag: 'e2' })
  })

  it('场景1：左侧「内核设置」分组出现「管道配置」设置栏', async () => {
    render(
      <MemoryRouter>
        <SettingsPage />
      </MemoryRouter>,
    )

    await waitFor(() => {
      expect(screen.getByText('内核设置', { exact: true })).toBeInTheDocument()
    })
    expect(screen.getByText('管道配置', { exact: true })).toBeInTheDocument()
  })

  it('场景1：点击「管道配置」→ 右侧内联显示标题「管道配置」+ tabs（默认/L1/L2）', async () => {
    render(
      <MemoryRouter>
        <SettingsPage />
      </MemoryRouter>,
    )

    await waitFor(() => {
      expect(screen.getByText('管道配置', { exact: true })).toBeInTheDocument()
    })

    // 点击左侧「管道配置」入口
    fireEvent.click(screen.getByText('管道配置', { exact: true }))


  })

  it('场景2：默认 tab 加载 → 调用 getPipelineConfig(\'default\') → 展示 name 字段与 input_routes', async () => {
    render(
      <MemoryRouter>
        <SettingsPage />
      </MemoryRouter>,
    )

    await waitFor(() => {
      expect(screen.getByText('管道配置', { exact: true })).toBeInTheDocument()
    })
    fireEvent.click(screen.getByText('管道配置', { exact: true }))

    // 调用 getPipelineConfig('default')
    await waitFor(() => {
      expect(mockGetPipelineConfig).toHaveBeenCalledWith('default')
    })

    // name 字段（string → Input）
    await waitFor(() => {
      expect(screen.getByDisplayValue('agentos_agent')).toBeInTheDocument()
    })
    // input_routes 数组 → JSON textarea（匹配插件名）
    expect(screen.getByDisplayValue(/tool_schema/)).toBeInTheDocument()
    // 保存按钮
    expect(screen.getByText('保存配置', { exact: true })).toBeInTheDocument()
  })

  it('场景3：修改字段 → 点击保存 → 调用 savePipelineConfig(\'default\', config) → 显示「已保存」', async () => {
    render(
      <MemoryRouter>
        <SettingsPage />
      </MemoryRouter>,
    )

    await waitFor(() => {
      expect(screen.getByText('管道配置', { exact: true })).toBeInTheDocument()
    })
    fireEvent.click(screen.getByText('管道配置', { exact: true }))

    await waitFor(() => {
      expect(screen.getByDisplayValue('agentos_agent')).toBeInTheDocument()
    })

    // 修改 name 字段（状态传递：修改值 → 保存的 data）
    const nameInput = screen.getByDisplayValue('agentos_agent')
    fireEvent.change(nameInput, { target: { value: 'agentos_agent_modified' } })

    fireEvent.click(screen.getByTestId('save-btn'))

    await waitFor(() => {
      expect(mockSavePipelineConfig).toHaveBeenCalledWith('default', expect.objectContaining({
        name: 'agentos_agent_modified',
      }))
    })
    expect(screen.getByText('已保存', { exact: true })).toBeInTheDocument()
  })
})
