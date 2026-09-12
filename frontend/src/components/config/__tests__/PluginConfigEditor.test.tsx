/** @ci: frontend-test */
/**
 * PluginConfigEditor 主链测试（KV 模式）：加载态/失败态/保存链/409 冲突/
 * 添加自定义字段。contributionRegistry 返回无 mapping → 恒走原始 KV 编辑。
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const mockGet = vi.fn()
const mockSave = vi.fn()
const mockIsConflict = vi.fn()
const toastError = vi.fn()
const toastSuccess = vi.fn()

vi.mock('@/services/api/pluginConfig', () => ({
  getPluginConfigFile: (...args: unknown[]) => mockGet(...args),
  savePluginConfigFile: (...args: unknown[]) => mockSave(...args),
  isPluginConfigConflict: (e: unknown) => mockIsConflict(e),
}))
vi.mock('@/services/schema/ContributionRegistry', () => ({
  contributionRegistry: { getPluginConfigFiles: vi.fn(() => []) },
}))
vi.mock('@/services/schema/RjsfForm', () => ({
  RjsfForm: () => null,
}))
vi.mock('@/components/ui/sonner', () => ({
  toast: { error: (...a: unknown[]) => toastError(...a), success: (...a: unknown[]) => toastSuccess(...a) },
}))

import { PluginConfigEditor } from '../PluginConfigEditor'

const CONFIG = { max_tokens: 4096, model_name: 'glm-5' }

beforeEach(() => {
  vi.clearAllMocks()
  mockIsConflict.mockReturnValue(false)
  mockGet.mockResolvedValue({ data: { data: CONFIG }, etag: 'W/"v1"' })
  mockSave.mockResolvedValue({ etag: 'W/"v2"' })
})

describe('加载态', () => {
  it('加载中显示 loading；成功后渲染键值表单', async () => {
    render(<PluginConfigEditor pluginId="p1" fileId="main" title="插件配置" />)

    expect(screen.getByText('加载配置...')).toBeInTheDocument()
    expect(await screen.findByDisplayValue('4096')).toBeInTheDocument()
    expect(screen.getByDisplayValue('glm-5')).toBeInTheDocument()
  })

  it('加载失败：错误条 + toast，不出编辑区', async () => {
    mockGet.mockRejectedValue(new Error('network down'))

    render(<PluginConfigEditor pluginId="p1" fileId="main" title="插件配置" />)

    expect(await screen.findByText('network down')).toBeInTheDocument()
    expect(toastError).toHaveBeenCalledWith('配置加载失败', { description: 'network down' })
    expect(screen.queryByRole('button', { name: /保存配置/ })).not.toBeInTheDocument()
  })

  it('embedded=false 渲染标题与描述', async () => {
    render(
      <PluginConfigEditor
        pluginId="p1"
        fileId="main"
        title="插件配置"
        description="描述文本"
      />,
    )

    expect(screen.getByText('插件配置')).toBeInTheDocument()
    expect(screen.getByText('描述文本')).toBeInTheDocument()
    expect(await screen.findByDisplayValue('4096')).toBeInTheDocument()
  })
})

describe('保存链', () => {
  it('修改值后保存：携带新值与 etag，成功显示已保存', async () => {
    render(<PluginConfigEditor pluginId="p1" fileId="main" title="t" />)
    const input = await screen.findByDisplayValue('4096')

    fireEvent.change(input, { target: { value: '8192' } })
    fireEvent.click(screen.getByRole('button', { name: /保存配置/ }))

    await waitFor(() =>
      expect(mockSave).toHaveBeenCalledWith(
        'p1',
        'main',
        { max_tokens: 8192, model_name: 'glm-5' },
        'W/"v1"',
      ),
    )
    expect(await screen.findByText('已保存')).toBeInTheDocument()
    expect(toastSuccess).toHaveBeenCalledWith('配置已保存')
  })

  it('保存失败：显示保存失败 + toast', async () => {
    mockSave.mockRejectedValue(new Error('boom'))

    render(<PluginConfigEditor pluginId="p1" fileId="main" title="t" />)
    fireEvent.click(await screen.findByRole('button', { name: /保存配置/ }))

    expect(await screen.findByText('保存失败')).toBeInTheDocument()
    expect(toastError).toHaveBeenCalledWith('配置保存失败', { description: 'boom' })
  })

  it('409 冲突：提示刷新重试并采纳服务端新 etag', async () => {
    const conflict = { currentEtag: 'W/"server-v7"' }
    mockIsConflict.mockReturnValue(true)
    mockSave.mockRejectedValue(conflict)

    render(<PluginConfigEditor pluginId="p1" fileId="main" title="t" />)
    fireEvent.click(await screen.findByRole('button', { name: /保存配置/ }))

    expect(await screen.findByText('保存失败')).toBeInTheDocument()
    expect(toastError).toHaveBeenCalledWith('配置冲突', {
      description: '配置已被他人修改，请刷新后重试',
    })
    // 冲突后再次保存携带服务端新 etag
    fireEvent.click(screen.getByRole('button', { name: /保存配置/ }))
    await waitFor(() =>
      expect(mockSave).toHaveBeenLastCalledWith(
        'p1',
        'main',
        CONFIG,
        'W/"server-v7"',
      ),
    )
  })
})

describe('自定义字段', () => {
  it('添加字段入口展开输入；空名确认不生效', async () => {
    render(<PluginConfigEditor pluginId="p1" fileId="main" title="t" />)

    fireEvent.click(await screen.findByText('添加自定义字段'))
    const keyInput = screen.getByPlaceholderText('字段名（如 max_tokens）')
    fireEvent.change(keyInput, { target: { value: '  ' } })
    fireEvent.click(screen.getByRole('button', { name: '添加' }))

    // 空名：不新增输入框、无 toast
    expect(toastSuccess).not.toHaveBeenCalledWith('已添加字段: ', expect.anything())
  })

  it('输入新字段名确认 → 空值字段出现在表单', async () => {
    render(<PluginConfigEditor pluginId="p1" fileId="main" title="t" />)

    fireEvent.click(await screen.findByText('添加自定义字段'))
    fireEvent.change(screen.getByPlaceholderText('字段名（如 max_tokens）'), {
      target: { value: 'temperature' },
    })
    fireEvent.click(screen.getByRole('button', { name: '添加' }))

    await waitFor(() =>
      expect(toastSuccess).toHaveBeenCalledWith('已添加字段: temperature'),
    )
    expect(screen.getByDisplayValue('')).toBeInTheDocument()
  })
})
