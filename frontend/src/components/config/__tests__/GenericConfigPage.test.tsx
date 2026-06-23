/**
 * GenericConfigPage 组件测试
 *
 * 验证通用配置页面的核心功能：
 * - 加载中状态显示 spinner
 * - 配置加载成功后渲染表单
 * - 配置加载失败显示错误提示
 * - 字段类型自动映射（布尔/数字/字符串/对象/数组）
 * - 保存流程（saving → saved → idle）
 * - 保存失败显示错误
 * - 字段变更更新状态
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import React from 'react'
import { GenericConfigPage } from '../GenericConfigPage'

// ── Mock API ──
const mockGetGenericConfig = vi.fn()
const mockSaveGenericConfig = vi.fn()

vi.mock('@/services/api/config', () => ({
  getGenericConfig: (...args: unknown[]) => mockGetGenericConfig(...args),
  saveGenericConfig: (...args: unknown[]) => mockSaveGenericConfig(...args),
}))

// ── Mock UI 组件（简化依赖）──
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

describe('GenericConfigPage', () => {
  const defaultProps = {
    configPath: 'system/test_config',
    title: '测试配置页',
    description: '用于测试的配置页面',
  }

  const sampleConfig = {
    enabled: true,
    max_retries: 3,
    api_endpoint: 'http://localhost:8080',
    nested: {
      timeout: 30,
      name: 'inner',
    },
    tags: ['alpha', 'beta'],
  }

  beforeEach(() => {
    vi.useFakeTimers()
    mockGetGenericConfig.mockReset()
    mockSaveGenericConfig.mockReset()
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  describe('加载状态', () => {
    it('初始渲染显示加载中', () => {
      mockGetGenericConfig.mockReturnValue(new Promise(() => {})) // 永不 resolve
      render(<GenericConfigPage {...defaultProps} />)

      expect(screen.getByText(/加载配置/)).toBeInTheDocument()
    })

    it('显示页面标题', () => {
      mockGetGenericConfig.mockReturnValue(new Promise(() => {}))
      render(<GenericConfigPage {...defaultProps} />)

      expect(screen.getByText('测试配置页')).toBeInTheDocument()
    })
  })

  describe('加载成功', () => {
    it('配置加载后渲染表单', async () => {
      mockGetGenericConfig.mockResolvedValue(sampleConfig)
      render(<GenericConfigPage {...defaultProps} />)

      await waitFor(() => {
        expect(screen.getByText('保存配置')).toBeInTheDocument()
      })
    })

    it('渲染布尔字段为 checkbox', async () => {
      mockGetGenericConfig.mockResolvedValue({ enabled: true })
      render(<GenericConfigPage {...defaultProps} />)

      await waitFor(() => {
        const checkbox = screen.getByRole('checkbox')
        expect(checkbox).toBeInTheDocument()
        expect(checkbox).toBeChecked()
      })
    })

    it('渲染数字字段为 number input', async () => {
      mockGetGenericConfig.mockResolvedValue({ max_retries: 3 })
      render(<GenericConfigPage {...defaultProps} />)

      await waitFor(() => {
        const input = screen.getByDisplayValue('3')
        expect(input).toBeInTheDocument()
        expect(input).toHaveAttribute('type', 'number')
      })
    })

    it('渲染字符串字段为 text input', async () => {
      mockGetGenericConfig.mockResolvedValue({ api_endpoint: 'http://localhost:8080' })
      render(<GenericConfigPage {...defaultProps} />)

      await waitFor(() => {
        const input = screen.getByDisplayValue('http://localhost:8080')
        expect(input).toBeInTheDocument()
      })
    })

    it('渲染嵌套对象为分组 Section', async () => {
      mockGetGenericConfig.mockResolvedValue({ nested: { timeout: 30 } })
      render(<GenericConfigPage {...defaultProps} />)

      await waitFor(() => {
        expect(screen.getByText('Nested')).toBeInTheDocument()
      })
    })

    it('渲染数组字段带添加按钮', async () => {
      mockGetGenericConfig.mockResolvedValue({ tags: ['alpha'] })
      render(<GenericConfigPage {...defaultProps} />)

      await waitFor(() => {
        expect(screen.getByText('+ 添加项')).toBeInTheDocument()
      })
    })

    it('snake_case 键名转为可读标签', async () => {
      mockGetGenericConfig.mockResolvedValue({ max_retries: 3 })
      render(<GenericConfigPage {...defaultProps} />)

      await waitFor(() => {
        expect(screen.getByText('Max retries')).toBeInTheDocument()
      })
    })

    it('空对象不渲染任何内容', async () => {
      mockGetGenericConfig.mockResolvedValue({})
      render(<GenericConfigPage {...defaultProps} />)

      await waitFor(() => {
        expect(screen.getByText('保存配置')).toBeInTheDocument()
      })

      // 没有字段渲染，只有保存按钮
      expect(screen.queryByTestId('input')).not.toBeInTheDocument()
    })
  })

  describe('加载失败', () => {
    it('显示错误提示', async () => {
      mockGetGenericConfig.mockRejectedValue(new Error('Network error'))
      render(<GenericConfigPage {...defaultProps} />)

      await waitFor(() => {
        expect(screen.getByText('无法加载配置')).toBeInTheDocument()
      })
    })

    it('加载失败后仍可看到保存按钮（因为 config 设为空对象）', async () => {
      mockGetGenericConfig.mockRejectedValue(new Error('Network error'))
      render(<GenericConfigPage {...defaultProps} />)

      await waitFor(() => {
        // 显示了错误提示且页面可交互
        expect(screen.getByRole('form')).toBeInTheDocument()
      })
    })
  })

  describe('保存流程', () => {
    it('点击保存调用 API', async () => {
      mockGetGenericConfig.mockResolvedValue(sampleConfig)
      const savedConfig = { ...sampleConfig, max_retries: 5 }
      mockSaveGenericConfig.mockResolvedValue(savedConfig)
      render(<GenericConfigPage {...defaultProps} />)

      await waitFor(() => {
        expect(screen.getByText('保存配置')).toBeInTheDocument()
      })

      const saveBtn = screen.getByTestId('save-btn')
      fireEvent.click(saveBtn)

      expect(mockSaveGenericConfig).toHaveBeenCalledWith(
        'system/test_config',
        sampleConfig,
      )
    })

    it('保存中显示加载状态', async () => {
      mockGetGenericConfig.mockResolvedValue(sampleConfig)
      mockSaveGenericConfig.mockReturnValue(new Promise(() => {}))
      render(<GenericConfigPage {...defaultProps} />)

      await waitFor(() => {
        expect(screen.getByText('保存配置')).toBeInTheDocument()
      })

      fireEvent.click(screen.getByTestId('save-btn'))

      await waitFor(() => {
        expect(screen.getByText(/保存中/)).toBeInTheDocument()
      })
      expect(screen.getByTestId('save-btn')).toBeDisabled()
    })

    it('保存成功显示已保存提示', async () => {
      mockGetGenericConfig.mockResolvedValue(sampleConfig)
      mockSaveGenericConfig.mockResolvedValue(sampleConfig)
      render(<GenericConfigPage {...defaultProps} />)

      await waitFor(() => {
        expect(screen.getByText('保存配置')).toBeInTheDocument()
      })

      fireEvent.click(screen.getByTestId('save-btn'))

      await waitFor(() => {
        expect(screen.getByText('已保存')).toBeInTheDocument()
      })
    })

    it('保存成功提示 2 秒后消失', async () => {
      mockGetGenericConfig.mockResolvedValue(sampleConfig)
      mockSaveGenericConfig.mockResolvedValue(sampleConfig)
      render(<GenericConfigPage {...defaultProps} />)

      await waitFor(() => {
        expect(screen.getByText('保存配置')).toBeInTheDocument()
      })

      fireEvent.click(screen.getByTestId('save-btn'))

      await waitFor(() => {
        expect(screen.getByText('已保存')).toBeInTheDocument()
      })

      vi.advanceTimersByTime(2000)

      expect(screen.queryByText('已保存')).not.toBeInTheDocument()
    })

    it('保存失败显示错误提示', async () => {
      mockGetGenericConfig.mockResolvedValue(sampleConfig)
      mockSaveGenericConfig.mockRejectedValue(new Error('Save failed'))
      render(<GenericConfigPage {...defaultProps} />)

      await waitFor(() => {
        expect(screen.getByText('保存配置')).toBeInTheDocument()
      })

      fireEvent.click(screen.getByTestId('save-btn'))

      await waitFor(() => {
        expect(screen.getByText('保存失败')).toBeInTheDocument()
      })
    })
  })

  describe('字段变更', () => {
    it('修改字符串字段更新本地状态', async () => {
      mockGetGenericConfig.mockResolvedValue({ name: '原始值' })
      mockSaveGenericConfig.mockResolvedValue({ name: '新值' })
      render(<GenericConfigPage {...defaultProps} />)

      await waitFor(() => {
        expect(screen.getByDisplayValue('原始值')).toBeInTheDocument()
      })

      const input = screen.getByDisplayValue('原始值')
      fireEvent.change(input, { target: { value: '新值' } })

      expect(screen.getByDisplayValue('新值')).toBeInTheDocument()
    })

    it('修改布尔字段切换状态', async () => {
      mockGetGenericConfig.mockResolvedValue({ enabled: true })
      render(<GenericConfigPage {...defaultProps} />)

      await waitFor(() => {
        const checkbox = screen.getByRole('checkbox')
        expect(checkbox).toBeChecked()
      })

      fireEvent.click(screen.getByRole('checkbox'))

      expect(screen.getByRole('checkbox')).not.toBeChecked()
    })
  })
})
