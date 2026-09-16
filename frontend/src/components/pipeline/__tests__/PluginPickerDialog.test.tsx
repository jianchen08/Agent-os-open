// @feature: FP-0.2.四 前端Schema | @ci: frontend-test
/**
 * PluginPickerDialog 行为测试（补齐目录过滤 / 手动引用两条输入通道）
 *
 * 弹窗职责：把一条插件引用写进 step 的 steps 组合。两条通道：
 * - 目录通道：role 分组展示 + 关键字过滤（id / name 任一命中）+ 已在本组合的置灰
 * - 手动通道：目录覆盖不到的形态（step 库 id / "{{...}}" 动态模板）原样加入，
 *   空串或纯空白不得成为引用
 *
 * 选中后统一语义：onPick(引用字符串) + 关闭弹窗 + 清空两条通道的输入。
 *
 * 测试策略：真实 Radix Dialog + 真实 Input，仅以 props 回传作观测面。
 */

import { fireEvent, render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { PluginPickerDialog } from '../PluginPickerDialog'
import type { PipelinePluginCatalogEntry } from '@/services/api/pipelines'

/** 目录条目工厂 */
function entry(over: Partial<PipelinePluginCatalogEntry> & { id: string }): PipelinePluginCatalogEntry {
  return {
    name: over.id,
    role: 'core',
    hostType: 'sidecar',
    version: '1.0.0',
    enabled: true,
    configFiles: [],
    ...over,
  }
}

const CATALOG: PipelinePluginCatalogEntry[] = [
  entry({ id: 'pipeline_tool_schema', name: 'Tool Schema', role: 'input' }),
  entry({ id: 'pipeline_spill_guard', name: 'Spill Guard', role: 'core' }),
  entry({ id: 'pipeline_result_format', name: 'Result Format', role: 'output', enabled: false }),
  entry({ id: 'pipeline_orphan', name: 'Orphan', role: null, enabled: null }),
]

function renderDialog(over: Partial<Parameters<typeof PluginPickerDialog>[0]> = {}) {
  const props = {
    open: true,
    onOpenChange: vi.fn(),
    catalog: CATALOG,
    excludeIds: [] as string[],
    onPick: vi.fn(),
    ...over,
  }
  const view = render(<PluginPickerDialog {...props} />)
  return { ...view, props }
}

beforeEach(() => {
  vi.resetAllMocks()
})

describe('PluginPickerDialog — 目录通道', () => {
  it('按 role 分组渲染四类标签，条目同时展示名称与 id', () => {
    renderDialog()

    expect(screen.getByText('输入 (input)')).toBeInTheDocument()
    expect(screen.getByText('核心 (core)')).toBeInTheDocument()
    expect(screen.getByText('输出 (output)')).toBeInTheDocument()
    expect(screen.getByText('未标注角色')).toBeInTheDocument()
    expect(screen.getByLabelText('添加 pipeline_tool_schema')).toHaveTextContent('Tool Schema')
    expect(screen.getByLabelText('添加 pipeline_tool_schema')).toHaveTextContent(
      'pipeline_tool_schema',
    )
  })

  it('关键字过滤：命中 id / 命中名称各一组输入，均只留匹配项', () => {
    renderDialog()
    const search = screen.getByLabelText('搜索插件')

    // 组 1：只与 id 匹配（"spill" 不出现在任何 name 中）
    fireEvent.change(search, { target: { value: 'spill' } })
    expect(screen.getByLabelText('添加 pipeline_spill_guard')).toBeInTheDocument()
    expect(screen.queryByLabelText('添加 pipeline_tool_schema')).not.toBeInTheDocument()
    expect(screen.queryByLabelText('添加 pipeline_result_format')).not.toBeInTheDocument()

    // 组 2：只与 name 匹配（"Result Format" 的 id 不含 result format）
    fireEvent.change(search, { target: { value: 'result format' } })
    expect(screen.getByLabelText('添加 pipeline_result_format')).toBeInTheDocument()
    expect(screen.queryByLabelText('添加 pipeline_spill_guard')).not.toBeInTheDocument()
  })

  it('过滤大小写与首尾空白不敏感；无命中时全部条目消失', () => {
    renderDialog()
    const search = screen.getByLabelText('搜索插件')

    fireEvent.change(search, { target: { value: '  TOOL  ' } })
    expect(screen.getByLabelText('添加 pipeline_tool_schema')).toBeInTheDocument()

    fireEvent.change(search, { target: { value: '不存在的插件' } })
    expect(screen.queryByRole('button', { name: /^添加 pipeline_/ })).not.toBeInTheDocument()
  })

  it('清空关键字恢复全量条目（过滤不破坏目录）', () => {
    renderDialog()
    const search = screen.getByLabelText('搜索插件')

    fireEvent.change(search, { target: { value: 'spill' } })
    expect(screen.getAllByRole('button', { name: /^添加 pipeline_/ })).toHaveLength(1)

    fireEvent.change(search, { target: { value: '' } })
    expect(screen.getAllByRole('button', { name: /^添加 pipeline_/ })).toHaveLength(CATALOG.length)
  })

  it('已在本组合的引用置灰不可添加（excludeIds 只压自己）', () => {
    renderDialog({ excludeIds: ['pipeline_tool_schema'] })

    expect(screen.getByLabelText('添加 pipeline_tool_schema')).toBeDisabled()
    expect(screen.getByLabelText('添加 pipeline_spill_guard')).not.toBeDisabled()
    expect(screen.getByLabelText('添加 pipeline_tool_schema')).toHaveTextContent('已添加')
  })

  it('启用状态三态如实展示：已禁用 / 状态未知 / 正常不标注', () => {
    renderDialog()

    expect(screen.getByLabelText('添加 pipeline_result_format')).toHaveTextContent('已禁用')
    expect(screen.getByLabelText('添加 pipeline_orphan')).toHaveTextContent('状态未知')
    expect(screen.getByLabelText('添加 pipeline_spill_guard')).not.toHaveTextContent('已禁用')
    expect(screen.getByLabelText('添加 pipeline_spill_guard')).not.toHaveTextContent('状态未知')
  })

  it('点击目录条目 → onPick(id) 并关闭弹窗', () => {
    const { props } = renderDialog()

    fireEvent.click(screen.getByLabelText('添加 pipeline_spill_guard'))

    expect(props.onPick).toHaveBeenCalledWith('pipeline_spill_guard')
    expect(props.onOpenChange).toHaveBeenCalledWith(false)
  })

  it('目录为空时提示降级但仍可用手动输入', () => {
    renderDialog({ catalog: [] })

    expect(screen.getByText(/插件目录不可用/)).toBeInTheDocument()
    expect(screen.getByLabelText('手动输入引用')).toBeEnabled()
  })
})

describe('PluginPickerDialog — 手动引用通道', () => {
  it('输入回显 + 回车提交（原样加入，含动态模板形态）', () => {
    const { props } = renderDialog()
    const manual = screen.getByLabelText('手动输入引用')

    fireEvent.change(manual, { target: { value: '{{state.core_plugin}}' } })
    expect(manual).toHaveValue('{{state.core_plugin}}')

    fireEvent.keyDown(manual, { key: 'Enter' })

    expect(props.onPick).toHaveBeenCalledTimes(1)
    expect(props.onPick).toHaveBeenCalledWith('{{state.core_plugin}}')
    expect(props.onOpenChange).toHaveBeenCalledWith(false)
  })

  it('回车提交前去除首尾空白（写入的引用整洁）', () => {
    const { props } = renderDialog()
    const manual = screen.getByLabelText('手动输入引用')

    fireEvent.change(manual, { target: { value: '  step_lib_common  ' } })
    fireEvent.keyDown(manual, { key: 'Enter' })

    expect(props.onPick).toHaveBeenCalledWith('step_lib_common')
  })

  it('空串 / 纯空白输入不可提交：回车与按钮均无效且按钮禁用', () => {
    const { props } = renderDialog()
    const manual = screen.getByLabelText('手动输入引用')
    const addBtn = screen.getByRole('button', { name: '添加引用' })

    expect(addBtn).toBeDisabled()
    fireEvent.keyDown(manual, { key: 'Enter' })
    expect(props.onPick).not.toHaveBeenCalled()

    fireEvent.change(manual, { target: { value: '   ' } })
    expect(addBtn).toBeDisabled()
    fireEvent.keyDown(manual, { key: 'Enter' })
    expect(props.onPick).not.toHaveBeenCalled()
  })

  it('非回车按键不触发提交（只认 Enter）', () => {
    const { props } = renderDialog()
    const manual = screen.getByLabelText('手动输入引用')

    fireEvent.change(manual, { target: { value: 'step_lib_common' } })
    fireEvent.keyDown(manual, { key: 'a' })

    expect(props.onPick).not.toHaveBeenCalled()
  })

  it('点击「添加引用」按钮 → onPick(trim 后引用) 并关闭弹窗', () => {
    const { props } = renderDialog()
    const manual = screen.getByLabelText('手动输入引用')

    fireEvent.change(manual, { target: { value: '  pipeline_legacy_step ' } })
    fireEvent.click(screen.getByRole('button', { name: '添加引用' }))

    expect(props.onPick).toHaveBeenCalledWith('pipeline_legacy_step')
    expect(props.onOpenChange).toHaveBeenCalledWith(false)
  })

  it('选中后两条通道输入均被清空（重新打开是干净状态）', () => {
    renderDialog()
    const search = screen.getByLabelText('搜索插件')
    const manual = screen.getByLabelText('手动输入引用')

    fireEvent.change(search, { target: { value: 'spill' } })
    fireEvent.change(manual, { target: { value: 'step_lib_common' } })
    fireEvent.keyDown(manual, { key: 'Enter' })

    expect(search).toHaveValue('')
    expect(manual).toHaveValue('')
    // 清空后目录恢复全量（关键字随之复位）
    expect(screen.getAllByRole('button', { name: /^添加 pipeline_/ })).toHaveLength(CATALOG.length)
  })
})
