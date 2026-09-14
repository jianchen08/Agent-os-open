// @feature: FP-T12 PluginConfigEditor 补测 | @ci: frontend-test
/**
 * PluginConfigEditor 残余分支补测（簇3，与 .test / .typed / .branches 三个文件互补）
 *
 * 覆盖既有测试未触达的分支：
 * - embedded 模式渲染（页头/描述由组件自身输出，无独立页面外壳）
 * - 字符串字段编辑 → onChange 上抛（值变化写回配置树）
 * - null/undefined 值字段渲染（String(value) 兜底空串）与编辑
 * - ConfigObject 添加字段表单的「取消」按钮（退出添加态不写回）
 * - dict-of-dicts 模板克隆：混合类型样本（对象→{}、数字→0、布尔→false、字符串→''）
 *   与克隆值经对话框提交后落到配置树
 * - dict-of-dicts 添加对话框标识输入框 Enter 快捷提交
 *
 * mock 仅限外部服务（pluginConfig API）与 registry/表单边界；断言落在可观察行为
 * （渲染输出 / 保存入参 / API 调用）。
 */
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
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

beforeEach(() => {
  vi.clearAllMocks()
  mockIsConflict.mockReturnValue(false)
  mockGetMappings.mockReturnValue([])
  mockGet.mockResolvedValue({ data: { data: {} }, etag: 'W/"v1"' })
  mockSave.mockResolvedValue({ etag: 'W/"v2"' })
})

/** 点击保存并等待 API 调用，返回传给 savePluginConfigFile 的配置体 */
async function saveAndGetBody(): Promise<Record<string, unknown>> {
  fireEvent.click(screen.getByRole('button', { name: '保存配置' }))
  await waitFor(() => expect(mockSave).toHaveBeenCalled())
  return mockSave.mock.calls[mockSave.mock.calls.length - 1][2] as Record<string, unknown>
}

describe('embedded 模式', () => {
  it('embedded → 渲染标题与描述（不渲染独立页面外壳的额外标题）', async () => {
    mockGet.mockResolvedValue({
      data: { data: { model_name: 'glm-5' } },
      etag: 'W/"v1"',
    })
    render(
      <PluginConfigEditor pluginId="p1" fileId="main" title="嵌入配置" description="说明文字" embedded />,
    )

    expect(await screen.findByText('嵌入配置')).toBeInTheDocument()
    expect(screen.getByText('说明文字')).toBeInTheDocument()
    expect(screen.getByRole('form', { name: '嵌入配置表单' })).toBeInTheDocument()
    // 字段仍可编辑（embedded 只换外壳，不裁剪功能）
    expect(screen.getByDisplayValue('glm-5')).toBeInTheDocument()
  })

  it('embedded + 描述缺省 → 不渲染描述段落', async () => {
    render(<PluginConfigEditor pluginId="p1" fileId="main" title="无描述" embedded />)
    expect(await screen.findByText('无描述')).toBeInTheDocument()
    expect(screen.queryByText('说明文字')).not.toBeInTheDocument()
  })
})

describe('基础类型字段编辑上抛', () => {
  it('字符串字段编辑 → 保存体含新值', async () => {
    mockGet.mockResolvedValue({ data: { data: { model_name: 'glm-5' } }, etag: 'W/"v1"' })
    render(<PluginConfigEditor pluginId="p1" fileId="main" title="配置" />)

    const input = await screen.findByDisplayValue('glm-5')
    fireEvent.change(input, { target: { value: 'dp-4' } })

    const body = await saveAndGetBody()
    expect(body.model_name).toBe('dp-4')
  })

  it.each([
    ['null 值', null],
    ['undefined 值', undefined],
  ])('%s 字段 → 输入框空串兜底，编辑后写回字符串', async (_name, value) => {
    mockGet.mockResolvedValue({ data: { data: { nullable: value } }, etag: 'W/"v1"' })
    render(<PluginConfigEditor pluginId="p1" fileId="main" title="配置" />)

    // 该字段渲染为空输入框（String(null/undefined) 兜底）
    const label = await screen.findByText('Nullable')
    const row = label.closest('.flex') as HTMLElement
    const input = within(row).getByRole('textbox') as HTMLInputElement
    expect(input.value).toBe('')

    fireEvent.change(input, { target: { value: '填入值' } })
    const body = await saveAndGetBody()
    expect(body.nullable).toBe('填入值')
  })

  it('布尔字段勾选切换 → 保存体含翻转值', async () => {
    mockGet.mockResolvedValue({ data: { data: { enabled: false } }, etag: 'W/"v1"' })
    render(<PluginConfigEditor pluginId="p1" fileId="main" title="配置" />)

    const checkbox = await screen.findByRole('checkbox')
    expect(checkbox).not.toBeChecked()
    fireEvent.click(checkbox)

    const body = await saveAndGetBody()
    expect(body.enabled).toBe(true)
  })

  it('数字字段编辑 → 保存体为 number 类型', async () => {
    mockGet.mockResolvedValue({ data: { data: { max_tokens: 1024 } }, etag: 'W/"v1"' })
    render(<PluginConfigEditor pluginId="p1" fileId="main" title="配置" />)

    const input = await screen.findByRole('spinbutton')
    fireEvent.change(input, { target: { value: '2048' } })

    const body = await saveAndGetBody()
    expect(body.max_tokens).toBe(2048)
  })
})

describe('ConfigObject 添加字段表单', () => {
  it('点添加 → 输入框出现；点「取消」→ 收起且保存体不含新键', async () => {
    mockGet.mockResolvedValue({ data: { data: { a: '1' } }, etag: 'W/"v1"' })
    render(<PluginConfigEditor pluginId="p1" fileId="main" title="配置" />)

    fireEvent.click(await screen.findByText('添加自定义字段'))
    const keyInput = screen.getByPlaceholderText(/字段名/)
    fireEvent.change(keyInput, { target: { value: 'new_key' } })

    fireEvent.click(screen.getByRole('button', { name: '取消' }))
    expect(screen.queryByPlaceholderText(/字段名/)).not.toBeInTheDocument()

    const body = await saveAndGetBody()
    expect(body).not.toHaveProperty('new_key')
    expect(body.a).toBe('1')
  })

  it('添加字段后输入框收起（状态复位）', async () => {
    mockGet.mockResolvedValue({ data: { data: { a: '1' } }, etag: 'W/"v1"' })
    render(<PluginConfigEditor pluginId="p1" fileId="main" title="配置" />)

    fireEvent.click(await screen.findByText('添加自定义字段'))
    fireEvent.change(screen.getByPlaceholderText(/字段名/), { target: { value: 'fresh' } })
    fireEvent.click(screen.getByRole('button', { name: '添加' }))

    await waitFor(() => expect(screen.queryByPlaceholderText(/字段名/)).not.toBeInTheDocument())
    expect(screen.getByText('Fresh')).toBeInTheDocument()
  })
})

describe('dict-of-dicts 模板克隆与添加对话框', () => {
  const MIXED = {
    entries: {
      'first-item': {
        label: '名称',
        count: 3,
        active: true,
        nested: { inner: 'x' },
      },
    },
  }

  it('添加对话框按样本类型克隆模板（对象→空对象、数字→0、布尔→false、字符串→空串）', async () => {
    mockGet.mockResolvedValue({ data: { data: structuredClone(MIXED) }, etag: 'W/"v1"' })
    render(<PluginConfigEditor pluginId="p1" fileId="main" title="配置" />)

    fireEvent.click(await screen.findByText('添加Entries条目'))
    const dialog = await screen.findByRole('dialog')

    // 布尔样本 → 下拉框（select），默认「否」
    const boolSelect = within(dialog).getByRole('combobox') as HTMLSelectElement
    expect(boolSelect.value).toBe('false')
    // 数字样本 → number 输入框，克隆值 0
    expect((within(dialog).getByPlaceholderText('count') as HTMLInputElement).value).toBe('0')
    // 字符串样本 → 空串
    expect((within(dialog).getByPlaceholderText('label') as HTMLInputElement).value).toBe('')
    // 对象样本 → 空对象（渲染为「{}」文本域在嵌套层；此处断言字段存在）
    expect(within(dialog).getByText('Nested')).toBeInTheDocument()
  })

  it('克隆值经标识输入框 Enter 提交 → 新落到配置树（字段结构保留）', async () => {
    mockGet.mockResolvedValue({ data: { data: structuredClone(MIXED) }, etag: 'W/"v1"' })
    render(<PluginConfigEditor pluginId="p1" fileId="main" title="配置" />)

    fireEvent.click(await screen.findByText('添加Entries条目'))
    const dialog = await screen.findByRole('dialog')
    const keyInput = within(dialog).getByPlaceholderText(/条目标识/)
    fireEvent.change(keyInput, { target: { value: 'second-item' } })
    fireEvent.keyDown(keyInput, { key: 'Enter' })

    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    const body = await saveAndGetBody()
    const entries = body.entries as Record<string, Record<string, unknown>>
    expect(Object.keys(entries).sort()).toEqual(['first-item', 'second-item'])
    // 克隆模板字段结构保留、值按类型清零
    expect(entries['second-item']).toEqual({ label: '', count: 0, active: false, nested: {} })
  })

  it('标识为空提交 → 校验拦截（不写回，对话框保留）', async () => {
    mockGet.mockResolvedValue({ data: { data: structuredClone(MIXED) }, etag: 'W/"v1"' })
    render(<PluginConfigEditor pluginId="p1" fileId="main" title="配置" />)

    fireEvent.click(await screen.findByText('添加Entries条目'))
    const dialog = await screen.findByRole('dialog')
    fireEvent.click(within(dialog).getByRole('button', { name: '添加' }))

    await waitFor(() => expect(toastError).toHaveBeenCalledWith('请输入条目标识'))
    expect(screen.getByRole('dialog')).toBeInTheDocument()
  })
})
