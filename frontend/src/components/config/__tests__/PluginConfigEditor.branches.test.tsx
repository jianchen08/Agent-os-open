// @feature: FP-T12 PluginConfigEditor 补测 | @ci: frontend-test
/**
 * PluginConfigEditor 分支补测
 *
 * 既有 PluginConfigEditor.test.tsx（KV 主链）与 PluginConfigEditor.typed.test.tsx
 * （T1 类型化表单）已覆盖：加载/失败态、保存链、409 冲突、自定义字段增删、
 * typed↔raw 单向切换。本文件补齐其余分支：
 * - env target 密钥表单（GAP-4）：掩码/required 徽标、留空提交 *** 哨兵、
 *   新值提交、保存失败/冲突/加载失败
 * - 原始 KV 编辑：空配置、布尔/密文/数组/嵌套对象/字段删除、Enter 添加、重复字段
 * - dict-of-dicts：展开切换、条目删除、添加对话框（空/重复校验、模板克隆、取消）
 * - typed↔raw 往返（返回类型化表单）
 *
 * mock 仅限外部服务（pluginConfig API）与 registry/sonner/RjsfForm 边界；
 * 表单交互走真实 testing-library 事件，断言可观察行为（渲染输出 / API 入参 / toast）。
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const mockGet = vi.fn()
const mockSave = vi.fn()
const mockIsConflict = vi.fn()
const toastError = vi.fn()
const toastSuccess = vi.fn()

type MappingFile = { id: string; path: string; label: string; target?: string; fields?: unknown[] }
const mockGetMappings = vi.fn<(pluginId: string) => MappingFile[]>(() => [])

vi.mock('@/services/api/pluginConfig', () => ({
  getPluginConfigFile: (...args: unknown[]) => mockGet(...args),
  savePluginConfigFile: (...args: unknown[]) => mockSave(...args),
  isPluginConfigConflict: (e: unknown) => mockIsConflict(e),
}))
vi.mock('@/services/schema/ContributionRegistry', () => ({
  contributionRegistry: { getPluginConfigFiles: (pluginId: string) => mockGetMappings(pluginId) },
}))
vi.mock('@/services/schema/RjsfForm', () => ({
  RjsfForm: () => null,
}))
vi.mock('@/components/ui/sonner', () => ({
  toast: {
    error: (...a: unknown[]) => toastError(...a),
    success: (...a: unknown[]) => toastSuccess(...a),
  },
}))

import { PluginConfigEditor } from '../PluginConfigEditor'

const CONFIG = { max_tokens: 4096, model_name: 'glm-5' }

/** registry 返回值须为稳定引用：mapping 对象随渲染重复求值，引用漂移会导致 env 表单 effect 重跑 */
const ENV_MAPPING: MappingFile = {
  id: 'mcp_env',
  path: '.env',
  label: '外部源密钥',
  target: 'env',
  fields: [
    { name: 'GITHUB_TOKEN', label: 'GitHub 令牌', required: true },
    { name: 'SLACK_KEY', label: 'Slack 密钥', required: true },
    { name: 'MISC_KEY', label: '杂项密钥' },
  ],
}

const DICT_CONFIG = {
  models: {
    'glm-5': { enabled: true, max_tokens: 4096 },
    'dp-4': { enabled: false, max_tokens: 2048 },
  },
}

function renderEditor(fileId = 'main'): void {
  render(<PluginConfigEditor pluginId="p1" fileId={fileId} title="插件配置" />)
}

beforeEach(() => {
  vi.clearAllMocks()
  mockIsConflict.mockReturnValue(false)
  mockGetMappings.mockReturnValue([])
  mockGet.mockResolvedValue({ data: { data: structuredClone(CONFIG) }, etag: 'W/"v1"' })
  mockSave.mockResolvedValue({ etag: 'W/"v2"' })
})

describe('env target：密钥表单（GAP-4）', () => {
  const ENV_DATA = { GITHUB_TOKEN: '***', SLACK_KEY: '', MISC_KEY: '' }
  const etagOf = (data: unknown, etag: string): { data: { data: unknown }; etag: string } => ({
    data: { data },
    etag,
  })

  function renderEnv(): void {
    mockGetMappings.mockReturnValue([ENV_MAPPING])
    render(<PluginConfigEditor pluginId="mcp_source" fileId="mcp_env" title="外部源密钥" />)
  }

  it('按掩码与 required 区分徽标：已配置 / 未配置（必填）/ 未配置（可选），且不渲染 KV 树', async () => {
    mockGet.mockResolvedValue(etagOf(ENV_DATA, 'e1'))
    renderEnv()

    expect(await screen.findByText('已配置')).toBeInTheDocument()
    expect(screen.getByText('未配置（必填）')).toBeInTheDocument()
    expect(screen.getByText('未配置（可选）')).toBeInTheDocument()
    expect(screen.getByPlaceholderText('已保存——输入新值以更换，留空保留')).toBeInTheDocument()
    expect(screen.getAllByPlaceholderText('输入 API Key')).toHaveLength(2)
    expect(screen.queryByText('添加自定义字段')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '保存配置' })).not.toBeInTheDocument()
  })

  it('保存：留空字段提交 *** 哨兵，成功后清空输入并提示', async () => {
    mockGet.mockResolvedValue(etagOf(ENV_DATA, 'e1'))
    renderEnv()

    fireEvent.click(await screen.findByRole('button', { name: '保存密钥' }))

    await waitFor(() =>
      expect(mockSave).toHaveBeenCalledWith(
        'mcp_source',
        'mcp_env',
        { GITHUB_TOKEN: '***', SLACK_KEY: '***', MISC_KEY: '***' },
        'e1',
      ),
    )
    expect(toastSuccess).toHaveBeenCalledWith('密钥已保存', { description: '无需重启内核，立即生效' })
    await waitFor(() =>
      expect(screen.getByPlaceholderText('已保存——输入新值以更换，留空保留')).toHaveValue(''),
    )
  })

  it('保存：输入新值提交新值（覆盖掩码），留空字段仍为哨兵', async () => {
    mockGet.mockResolvedValue(etagOf(ENV_DATA, 'e1'))
    renderEnv()

    fireEvent.change(await screen.findByPlaceholderText('已保存——输入新值以更换，留空保留'), {
      target: { value: 'gh-new' },
    })
    fireEvent.click(screen.getByRole('button', { name: '保存密钥' }))

    await waitFor(() =>
      expect(mockSave).toHaveBeenCalledWith(
        'mcp_source',
        'mcp_env',
        { GITHUB_TOKEN: 'gh-new', SLACK_KEY: '***', MISC_KEY: '***' },
        'e1',
      ),
    )
  })

  it('保存失败：toast 透传后端消息', async () => {
    mockGet.mockResolvedValue(etagOf(ENV_DATA, 'e1'))
    mockSave.mockRejectedValue(new Error('kaboom'))
    renderEnv()

    fireEvent.click(await screen.findByRole('button', { name: '保存密钥' }))

    expect(await screen.findByRole('button', { name: '保存密钥' })).toBeInTheDocument()
    expect(toastError).toHaveBeenCalledWith('密钥保存失败', { description: 'kaboom' })
  })

  it('409 冲突：冲突提示并采纳服务端新 etag', async () => {
    mockGet.mockResolvedValue(etagOf(ENV_DATA, 'e1'))
    mockIsConflict.mockReturnValue(true)
    mockSave.mockRejectedValue({ currentEtag: 'e9' })
    renderEnv()

    fireEvent.click(await screen.findByRole('button', { name: '保存密钥' }))
    await waitFor(() =>
      expect(toastError).toHaveBeenCalledWith('配置冲突', {
        description: '密钥状态已被他人修改，请刷新后重试',
      }),
    )

    fireEvent.click(screen.getByRole('button', { name: '保存密钥' }))
    await waitFor(() => expect(mockSave).toHaveBeenLastCalledWith('mcp_source', 'mcp_env', expect.anything(), 'e9'))
  })

  it('密钥配置加载失败：toast 且表单仍可交互', async () => {
    // 调用顺序：外层编辑器先取配置（成功放行渲染），随后密钥表单自身取数失败
    mockGet.mockResolvedValueOnce(etagOf(ENV_DATA, 'e1')).mockRejectedValueOnce(new Error('env down'))
    renderEnv()

    expect(await screen.findByRole('button', { name: '保存密钥' })).toBeInTheDocument()
    expect(toastError).toHaveBeenCalledWith('密钥配置加载失败', { description: 'env down' })
  })
})

describe('原始 KV 编辑分支', () => {
  it('空配置：渲染空态占位且仍可保存空对象', async () => {
    mockGet.mockResolvedValue({ data: { data: undefined }, etag: 'W/"v1"' })
    renderEditor()

    expect(await screen.findByText('该配置暂无字段')).toBeInTheDocument()
    const saveBtn = screen.getByRole('button', { name: '保存配置' })
    expect(saveBtn).toBeEnabled()
    fireEvent.click(saveBtn)
    await waitFor(() => expect(mockSave).toHaveBeenCalledWith('p1', 'main', {}, 'W/"v1"'))
  })

  it('布尔字段：勾选切换状态文案并写回 true', async () => {
    mockGet.mockResolvedValue({ data: { data: { enabled: false } }, etag: 'W/"v1"' })
    renderEditor()

    const checkbox = await screen.findByRole('checkbox')
    expect(checkbox).not.toBeChecked()
    expect(screen.getByText('已禁用')).toBeInTheDocument()

    fireEvent.click(checkbox)
    expect(await screen.findByText('已启用')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: '保存配置' }))
    await waitFor(() => expect(mockSave).toHaveBeenCalledWith('p1', 'main', { enabled: true }, 'W/"v1"'))
  })

  it.each([
    ['字段名命中密钥词', 'api_key', 'sk-secret', 'password'],
    ['普通字段', 'note', 'plain text', 'text'],
    ['值含 *** 掩码', 'display_name', '***', 'password'],
  ])('字符串字段密文探测（%s）：输入类型为 %s', async (_name, key, value, expectedType) => {
    mockGet.mockResolvedValue({ data: { data: { [key]: value } }, etag: 'W/"v1"' })
    renderEditor()

    expect(await screen.findByDisplayValue(value)).toHaveAttribute('type', expectedType)
  })

  it('数组字段：合法 JSON 写回数组', async () => {
    mockGet.mockResolvedValue({ data: { data: { tags: ['a', 'b'] } }, etag: 'W/"v1"' })
    renderEditor()

    const textarea = await screen.findByLabelText('Tags')
    if (!(textarea instanceof HTMLTextAreaElement)) {
      throw new Error('Tags 控件不是 textarea')
    }
    expect(textarea.value).toContain('"a"')

    fireEvent.change(textarea, { target: { value: '["a", "b", "c"]' } })
    fireEvent.click(screen.getByRole('button', { name: '保存配置' }))

    await waitFor(() =>
      expect(mockSave).toHaveBeenCalledWith('p1', 'main', { tags: ['a', 'b', 'c'] }, 'W/"v1"'),
    )
  })

  it('数组字段：输入中非法 JSON 不写回，保存仍是原数组', async () => {
    mockGet.mockResolvedValue({ data: { data: { tags: ['a', 'b'] } }, etag: 'W/"v1"' })
    renderEditor()

    const textarea = await screen.findByLabelText('Tags')
    fireEvent.change(textarea, { target: { value: '[1,2' } })
    fireEvent.click(screen.getByRole('button', { name: '保存配置' }))

    await waitFor(() =>
      expect(mockSave).toHaveBeenCalledWith('p1', 'main', { tags: ['a', 'b'] }, 'W/"v1"'),
    )
  })

  it('嵌套对象：按路径写回深层值', async () => {
    mockGet.mockResolvedValue({ data: { data: { defaults: { temperature: 0.7 } } }, etag: 'W/"v1"' })
    renderEditor()

    fireEvent.change(await screen.findByLabelText('Temperature'), { target: { value: '0.9' } })
    fireEvent.click(screen.getByRole('button', { name: '保存配置' }))

    await waitFor(() =>
      expect(mockSave).toHaveBeenCalledWith(
        'p1',
        'main',
        { defaults: { temperature: 0.9 } },
        'W/"v1"',
      ),
    )
  })

  it('嵌套对象：点标题折叠/再展开子字段', async () => {
    mockGet.mockResolvedValue({ data: { data: { defaults: { temperature: 0.7 } } }, etag: 'W/"v1"' })
    renderEditor()

    const header = await screen.findByRole('button', { name: /Defaults/ })
    fireEvent.click(header)
    expect(screen.queryByLabelText('Temperature')).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: /Defaults/ }))
    expect(await screen.findByLabelText('Temperature')).toBeInTheDocument()
  })

  it('删除字段按钮：字段移除且保存不含该键', async () => {
    mockGet.mockResolvedValue({ data: { data: { keep: 'x', drop: 'y' } }, etag: 'W/"v1"' })
    renderEditor()

    fireEvent.click(await screen.findByTitle('删除 drop'))
    expect(screen.queryByDisplayValue('y')).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: '保存配置' }))
    await waitFor(() => expect(mockSave).toHaveBeenCalledWith('p1', 'main', { keep: 'x' }, 'W/"v1"'))
  })

  it('添加自定义字段：重复字段名提示已存在且不重复渲染', async () => {
    renderEditor()

    fireEvent.click(await screen.findByText('添加自定义字段'))
    fireEvent.change(screen.getByPlaceholderText('字段名（如 max_tokens）'), {
      target: { value: 'max_tokens' },
    })
    fireEvent.click(screen.getByRole('button', { name: '添加' }))

    expect(toastError).toHaveBeenCalledWith('字段已存在', { description: 'max_tokens' })
    expect(toastSuccess).not.toHaveBeenCalled()
    expect(screen.getAllByLabelText('Max Tokens')).toHaveLength(1)
  })

  it('添加自定义字段：Enter 键提交', async () => {
    mockGet.mockResolvedValue({ data: { data: { alpha: 1 } }, etag: 'W/"v1"' })
    renderEditor()

    fireEvent.click(await screen.findByText('添加自定义字段'))
    const keyInput = screen.getByPlaceholderText('字段名（如 max_tokens）')
    fireEvent.change(keyInput, { target: { value: 'beta' } })
    fireEvent.keyDown(keyInput, { key: 'Enter' })

    expect(toastSuccess).toHaveBeenCalledWith('已添加字段: beta')
    expect(await screen.findByLabelText('Beta')).toBeInTheDocument()
  })
})

describe('dict-of-dicts（models 形态）', () => {
  beforeEach(() => {
    mockGet.mockResolvedValue({ data: { data: structuredClone(DICT_CONFIG) }, etag: 'W/"v1"' })
  })

  it('默认展开第一个条目，点击其他条目切换展开', async () => {
    renderEditor()

    expect(await screen.findByLabelText('Max Tokens')).toHaveValue(4096)
    fireEvent.click(screen.getByRole('button', { name: '▶ dp-4' }))

    expect(await screen.findByLabelText('Max Tokens')).toHaveValue(2048)
    expect(screen.getByRole('button', { name: '▶ glm-5' })).toBeInTheDocument()
  })

  it('删除条目：条目消失且保存不含该键', async () => {
    renderEditor()

    fireEvent.click(await screen.findByTitle('删除 glm-5'))
    expect(screen.queryByRole('button', { name: /glm-5/ })).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: '保存配置' }))
    await waitFor(() =>
      expect(mockSave).toHaveBeenCalledWith(
        'p1',
        'main',
        { models: { 'dp-4': { enabled: false, max_tokens: 2048 } } },
        'W/"v1"',
      ),
    )
  })

  it('添加对话框：空标识校验不通过，不产生保存', async () => {
    renderEditor()

    fireEvent.click(await screen.findByRole('button', { name: '添加Models条目' }))
    fireEvent.click(await screen.findByRole('button', { name: '添加' }))

    expect(toastError).toHaveBeenCalledWith('请输入条目标识')
    expect(mockSave).not.toHaveBeenCalled()
  })

  it('添加对话框：重复标识提示已存在', async () => {
    renderEditor()

    fireEvent.click(await screen.findByRole('button', { name: '添加Models条目' }))
    fireEvent.change(screen.getByPlaceholderText('条目标识（如 new_model）'), {
      target: { value: 'glm-5' },
    })
    fireEvent.click(screen.getByRole('button', { name: '添加' }))

    expect(toastError).toHaveBeenCalledWith('条目已存在', { description: 'glm-5' })
    expect(mockSave).not.toHaveBeenCalled()
  })

  it('添加对话框：新标识按模板结构入库（用户填值生效）并展开', async () => {
    renderEditor()

    fireEvent.click(await screen.findByRole('button', { name: '添加Models条目' }))
    fireEvent.change(screen.getByPlaceholderText('条目标识（如 new_model）'), {
      target: { value: 'kimi-k2' },
    })
    fireEvent.change(screen.getByPlaceholderText('max_tokens'), { target: { value: '512' } })
    // 对话框内唯一的 combobox 即模板 enabled 的 否/是 选择器
    fireEvent.change(screen.getByRole('combobox'), { target: { value: 'true' } })
    fireEvent.click(screen.getByRole('button', { name: '添加' }))

    expect(toastSuccess).toHaveBeenCalledWith('已添加: kimi-k2')
    expect(await screen.findByRole('button', { name: '▼ kimi-k2' })).toBeInTheDocument()
    expect(await screen.findByLabelText('Max Tokens')).toHaveValue(512)

    fireEvent.click(screen.getByRole('button', { name: '保存配置' }))
    await waitFor(() =>
      expect(mockSave).toHaveBeenCalledWith(
        'p1',
        'main',
        {
          models: {
            'glm-5': { enabled: true, max_tokens: 4096 },
            'dp-4': { enabled: false, max_tokens: 2048 },
            'kimi-k2': { enabled: true, max_tokens: 512 },
          },
        },
        'W/"v1"',
      ),
    )
  })

  it('添加对话框：取消丢弃输入', async () => {
    renderEditor()

    fireEvent.click(await screen.findByRole('button', { name: '添加Models条目' }))
    fireEvent.change(screen.getByPlaceholderText('条目标识（如 new_model）'), {
      target: { value: 'tmp' },
    })
    fireEvent.click(screen.getByRole('button', { name: '取消' }))

    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /tmp/ })).not.toBeInTheDocument()
    expect(mockSave).not.toHaveBeenCalled()
  })
})

describe('typed ↔ raw 逃生口往返', () => {
  it('切到原始 KV 后可返回类型化表单', async () => {
    mockGetMappings.mockReturnValue([
      {
        id: 'embedding',
        path: 'config/models/embedding.yaml',
        label: '向量模型配置',
        fields: [{ name: 'provider', type: 'string', label: '提供商' }],
      },
    ])
    renderEditor('embedding')

    fireEvent.click(await screen.findByText('原始 KV 编辑（fields 未覆盖的键）'))
    expect(await screen.findByText('添加自定义字段')).toBeInTheDocument()
    expect(screen.getByText('← 返回类型化表单')).toBeInTheDocument()

    fireEvent.click(screen.getByText('← 返回类型化表单'))
    await waitFor(() =>
      expect(screen.getByText('原始 KV 编辑（fields 未覆盖的键）')).toBeInTheDocument(),
    )
    expect(screen.queryByText('← 返回类型化表单')).not.toBeInTheDocument()
    expect(screen.queryByText('添加自定义字段')).not.toBeInTheDocument()
  })
})
