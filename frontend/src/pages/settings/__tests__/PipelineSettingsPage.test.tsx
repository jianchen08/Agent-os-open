/**
 * PipelineSettingsPage 组件测试
 *
 * 覆盖管道配置设置页核心功能：
 * - 加载中状态显示
 * - 加载成功渲染配置表单（tabs + 字段）
 * - 加载失败显示错误提示
 * - 修改字段后保存（PUT）成功/失败反馈
 * - 切换 tab 加载不同管道配置
 * - embedded 模式（嵌入设置页右侧，无独立全屏头）
 *
 * 测试策略：Mock 仅外部依赖（API 层 + UI 基础组件），组件真实渲染。
 */

import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import React from 'react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { PipelineSettingsPage } from '../PipelineSettingsPage'

// ── Mock API 层 ──
const mockGetPipelineConfig = vi.fn()
const mockSavePipelineConfig = vi.fn()

vi.mock('@/services/api/pipelineConfig', () => ({
  getPipelineConfig: (...args: unknown[]) => mockGetPipelineConfig(...args),
  savePipelineConfig: (...args: unknown[]) => mockSavePipelineConfig(...args),
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

vi.mock('lucide-react', () => ({
  Loader2: () => <span data-testid="loader" />,
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

describe('PipelineSettingsPage', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  describe('加载状态', () => {
    it('初始渲染显示加载中', () => {
      mockGetPipelineConfig.mockReturnValue(new Promise(() => {}))
      render(<PipelineSettingsPage />)

      expect(screen.getByText(/加载配置/)).toBeInTheDocument()
    })

    it('加载时调用 getPipelineConfig 读取默认管道', () => {
      mockGetPipelineConfig.mockReturnValue(new Promise(() => {}))
      render(<PipelineSettingsPage />)

      expect(mockGetPipelineConfig).toHaveBeenCalledWith('default')
    })
  })

  describe('加载成功', () => {
    it('渲染管道 tabs（默认/L1/L2 等）', async () => {
      mockGetPipelineConfig.mockResolvedValue({ name: 'default', data: samplePipeline, etag: 'e1' })
      render(<PipelineSettingsPage />)

      await waitFor(() => {
        expect(screen.getByText('默认')).toBeInTheDocument()
        expect(screen.getByText('L1 主 Agent')).toBeInTheDocument()
      })
    })

    it('渲染配置字段（管道名称、input_routes 等）', async () => {
      mockGetPipelineConfig.mockResolvedValue({ name: 'default', data: samplePipeline, etag: 'e1' })
      render(<PipelineSettingsPage />)

      await waitFor(() => {
        // name 字段（string → Input）
        expect(screen.getByDisplayValue('agentos_agent')).toBeInTheDocument()
        // input_routes 数组（渲染为 JSON textarea，用正则匹配插件名）
        expect(screen.getByDisplayValue(/tool_schema/)).toBeInTheDocument()
      })
    })

    it('显示保存按钮', async () => {
      mockGetPipelineConfig.mockResolvedValue({ name: 'default', data: samplePipeline, etag: 'e1' })
      render(<PipelineSettingsPage />)

      await waitFor(() => {
        expect(screen.getByText('保存配置')).toBeInTheDocument()
      })
    })
  })

  describe('加载失败', () => {
    it('显示错误提示', async () => {
      mockGetPipelineConfig.mockRejectedValue(new Error('Network error'))
      render(<PipelineSettingsPage />)

      await waitFor(() => {
        expect(screen.getByText('无法加载配置')).toBeInTheDocument()
      })
    })

    it('空配置显示「该配置暂无字段」且保存按钮可用', async () => {
      mockGetPipelineConfig.mockResolvedValue({ name: 'default', data: {}, etag: 'e-empty' })
      render(<PipelineSettingsPage />)

      await waitFor(() => {
        expect(screen.getByText('该配置暂无字段')).toBeInTheDocument()
      })
      expect(screen.getByText('保存配置')).toBeInTheDocument()
      const saveBtn = screen.getByTestId('save-btn')
      expect(saveBtn).toBeEnabled()
    })
  })



  describe('保存流程', () => {
    it('点击保存调用 savePipelineConfig 并显示已保存', async () => {
      mockGetPipelineConfig.mockResolvedValue({ name: 'default', data: samplePipeline, etag: 'e1' })
      mockSavePipelineConfig.mockResolvedValue({ name: 'default', etag: 'e2' })
      render(<PipelineSettingsPage />)

      await waitFor(() => {
        expect(screen.getByText('保存配置')).toBeInTheDocument()
      })

      fireEvent.click(screen.getByTestId('save-btn'))

      expect(mockSavePipelineConfig).toHaveBeenCalledWith('default', samplePipeline)

      await waitFor(() => {
        expect(screen.getByText('已保存')).toBeInTheDocument()
      })
    })

    it('保存失败显示错误提示', async () => {
      mockGetPipelineConfig.mockResolvedValue({ name: 'default', data: samplePipeline, etag: 'e1' })
      mockSavePipelineConfig.mockRejectedValue(new Error('Save failed'))
      render(<PipelineSettingsPage />)

      await waitFor(() => {
        expect(screen.getByText('保存配置')).toBeInTheDocument()
      })

      fireEvent.click(screen.getByTestId('save-btn'))

      await waitFor(() => {
        expect(screen.getByText('保存失败')).toBeInTheDocument()
      })
    })
  })

  describe('切换 tab', () => {
    it('切换 tab 后加载对应管道配置', async () => {
      mockGetPipelineConfig.mockResolvedValue({ name: 'default', data: samplePipeline, etag: 'e1' })
      render(<PipelineSettingsPage />)

      await waitFor(() => {
        expect(screen.getByText('L1 主 Agent')).toBeInTheDocument()
      })

      fireEvent.click(screen.getByText('L1 主 Agent'))

      await waitFor(() => {
        expect(mockGetPipelineConfig).toHaveBeenCalledWith('l1-main')
      })
    })
  })

  describe('embedded 模式', () => {
    it('embedded 时不渲染独立页面头（返回链接）', async () => {
      mockGetPipelineConfig.mockResolvedValue({ name: 'default', data: samplePipeline, etag: 'e1' })
      render(<PipelineSettingsPage embedded />)

      await waitFor(() => {
        expect(screen.getByText('保存配置')).toBeInTheDocument()
      })

      expect(screen.queryByText('← 返回设置')).not.toBeInTheDocument()
    })
  })
})
