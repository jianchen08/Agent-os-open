/** @feature FP-T12 前端适配 | @ci: frontend-test */
/**
 * ModelParamsEditor 组件交互补测（既有 ModelParamsEditor.test.ts 只覆盖纯函数序列化）
 *
 * 覆盖契约（受控组件，断言 onChange 提交的草稿）：
 * - 基础参数输入：上下文窗口/max_tokens/Temperature/Top P 数字框提交文本值
 * - 推理：勾选「推理模型」提交布尔；思考模式/推理力度下拉提交枚举值
 * - 思考强度映射：档位行下拉 onchange 写入 strength[档位]（缺失档位按空草稿补键）；
 *   自定义 levels 与 LEVEL_LABELS 兜底（未在词表的档位显示原值）
 * - 多模态：勾选后展开类型/上限输入，逐项提交；未勾选时不渲染子输入
 * - 自定义参数：输入框 Enter（名/值两处）触发加入；按钮禁用条件（空 key）；
 *   同名覆盖；移除已加入参数
 *
 * 测试策略：真实受控组件 + onChange spy；输入控件全部走真实 DOM 事件，
 * 不 mock 任何内部模块。
 */

import { fireEvent, render, screen, within } from '@testing-library/react'
import { useState } from 'react'
import { describe, expect, it, vi } from 'vitest'
import { emptyModelParamsDraft, type ModelParamsDraft } from '@/pages/settings/modelParams'
import { ModelParamsEditor } from '@/pages/settings/ModelParamsEditor'

function draft(overrides: Partial<ModelParamsDraft> = {}): ModelParamsDraft {
  return { ...emptyModelParamsDraft(), ...overrides }
}

/** 受控宿主：把 onChange 回流到 state，还原真实使用方式 */
function Controlled({ initial }: { initial?: Partial<ModelParamsDraft> }) {
  const [value, setValue] = useState<ModelParamsDraft>(draft(initial))
  return <ModelParamsEditor value={value} onChange={setValue} />
}

describe('ModelParamsEditor — 基础与推理参数', () => {
  it.each([
    ['最大输出 (max_tokens)', '4096'],
    ['Temperature', '0.35'],
    ['Top P', '0.9'],
  ] as const)('%s 输入提交同值草稿', (label, typed) => {
    const onChange = vi.fn()
    render(<ModelParamsEditor value={draft()} onChange={onChange} />)
    fireEvent.change(screen.getByLabelText(label), { target: { value: typed } })

    const next = onChange.mock.calls[0][0] as ModelParamsDraft
    const field = { '最大输出 (max_tokens)': 'maxTokens', Temperature: 'temperature', 'Top P': 'topP' }[
      label
    ] as keyof ModelParamsDraft
    expect(next[field]).toBe(Number(typed))
  })

  it('上下文窗口以字符串提交（不做数值转换）', () => {
    const onChange = vi.fn()
    render(<ModelParamsEditor value={draft()} onChange={onChange} />)
    fireEvent.change(screen.getByLabelText('上下文窗口 (tokens)'), { target: { value: '128000' } })
    expect(onChange.mock.calls[0][0].contextWindow).toBe('128000')
  })

  it('勾选推理模型提交布尔 true/false', () => {
    const onChange = vi.fn()
    render(<ModelParamsEditor value={draft({ reasoningModel: false })} onChange={onChange} />)
    const box = screen.getByRole('checkbox', { name: '推理模型' })
    fireEvent.click(box)
    expect(onChange.mock.calls[0][0].reasoningModel).toBe(true)

    onChange.mockClear()
    render(<ModelParamsEditor value={draft({ reasoningModel: true })} onChange={onChange} />)
    const boxes = screen.getAllByRole('checkbox', { name: '推理模型' })
    fireEvent.click(boxes[boxes.length - 1])
    expect(onChange.mock.calls[0][0].reasoningModel).toBe(false)
  })

  it.each([
    ['思考模式', 'adaptive'],
    ['推理力度', 'high'],
  ] as const)('%s 下拉选择提交枚举值', (label, value) => {
    const onChange = vi.fn()
    render(<ModelParamsEditor value={draft()} onChange={onChange} />)
    fireEvent.change(screen.getByLabelText(label), { target: { value } })

    const next = onChange.mock.calls[0][0] as ModelParamsDraft
    expect(label === '思考模式' ? next.thinkingType : next.effort).toBe(value)
  })
})

describe('ModelParamsEditor — 思考强度映射', () => {
  it('档位下拉写入对应档位（其余档位保持原值不丢）', () => {
    const onChange = vi.fn()
    render(
      <ModelParamsEditor
        value={emptyModelParamsDraft(['high', 'low'])}
        onChange={onChange}
        levels={['high', 'low']}
      />,
    )

    fireEvent.change(screen.getByLabelText('思考模式（高）'), { target: { value: 'disabled' } })
    expect((onChange.mock.calls[0][0] as ModelParamsDraft).strength).toEqual({
      high: { thinkingType: 'disabled', effort: '' },
      low: { thinkingType: '', effort: '' },
    })

    onChange.mockClear()
    fireEvent.change(screen.getByLabelText('推理力度（低）'), { target: { value: 'max' } })
    const next = onChange.mock.calls[0][0] as ModelParamsDraft
    expect(next.strength.low).toEqual({ thinkingType: '', effort: 'max' })
    expect(Object.keys(next.strength)).toEqual(['high', 'low'])
  })

  it('草稿缺该档位键时写入只含被改字段（读取空草稿、写入补键）', () => {
    const onChange = vi.fn()
    render(<ModelParamsEditor value={draft({ strength: {} })} onChange={onChange} levels={['high']} />)
    // 读取走 levelDraft 空草稿（下拉显示「不覆盖」），写入按 patch 补出该档键
    expect(screen.getByLabelText('推理力度（高）')).toHaveValue('')
    fireEvent.change(screen.getByLabelText('推理力度（高）'), { target: { value: 'medium' } })
    expect((onChange.mock.calls[0][0] as ModelParamsDraft).strength).toEqual({
      high: { effort: 'medium' },
    })
  })

  it('未在中文词表中的档位（如自定义 off）显示原值作为标签', () => {
    render(<ModelParamsEditor value={draft()} onChange={() => {}} levels={['off']} />)
    expect(screen.getByLabelText('思考模式（关）')).toBeInTheDocument()

    render(<ModelParamsEditor value={draft()} onChange={() => {}} levels={['turbo']} />)
    expect(screen.getByLabelText('思考模式（turbo）')).toBeInTheDocument()
  })

  it('已有档位配置回显在对应下拉', () => {
    render(
      <ModelParamsEditor
        value={draft({ strength: { high: { thinkingType: 'enabled', effort: 'low' } } })}
        onChange={() => {}}
        levels={['high']}
      />,
    )
    expect(screen.getByLabelText('思考模式（高）')).toHaveValue('enabled')
    expect(screen.getByLabelText('推理力度（高）')).toHaveValue('low')
  })
})

describe('ModelParamsEditor — 多模态', () => {
  it('未启用时不渲染类型与上限输入', () => {
    render(<ModelParamsEditor value={draft()} onChange={() => {}} />)
    expect(screen.queryByLabelText('图片类型')).toBeNull()
    expect(screen.queryByLabelText('图片上限(MB)')).toBeNull()
  })

  it('启用图片后展开输入，类型与上限各自提交', () => {
    const onChange = vi.fn()
    render(
      <ModelParamsEditor
        value={draft({
          multimodal: {
            ...emptyModelParamsDraft().multimodal,
            image: { enabled: true, types: '', maxSizeMb: '' },
          },
        })}
        onChange={onChange}
      />,
    )
    fireEvent.change(screen.getByLabelText('图片类型'), { target: { value: 'image/png' } })
    expect(onChange.mock.calls[0][0].multimodal.image.types).toBe('image/png')

    onChange.mockClear()
    fireEvent.change(screen.getByLabelText('图片上限(MB)'), { target: { value: '20' } })
    expect(onChange.mock.calls[0][0].multimodal.image.maxSizeMb).toBe('20')
  })

  it('勾选启用后子输入出现（受控回流）', () => {
    render(<Controlled />)
    expect(screen.queryByLabelText('音频类型')).toBeNull()

    fireEvent.click(screen.getByRole('checkbox', { name: '音频' }))
    expect(screen.getByLabelText('音频类型')).toBeInTheDocument()
    expect(screen.getByLabelText('音频上限(MB)')).toBeInTheDocument()
  })

  it('三种模态各自独立控制（勾选视频不影响图片）', () => {
    render(<Controlled />)
    fireEvent.click(screen.getByRole('checkbox', { name: '视频' }))
    expect(screen.getByLabelText('视频类型')).toBeInTheDocument()
    expect(screen.queryByLabelText('图片类型')).toBeNull()
  })
})

describe('ModelParamsEditor — 自定义参数', () => {
  it('点击「加入」把键值写入 customParams 并清空输入框', () => {
    const onChange = vi.fn()
    render(<ModelParamsEditor value={draft({ customKey: 'extra_body', customValue: 'auto' })} onChange={onChange} />)
    fireEvent.click(screen.getByRole('button', { name: '加入' }))

    const next = onChange.mock.calls[0][0] as ModelParamsDraft
    expect(next.customParams).toEqual([{ key: 'extra_body', value: 'auto' }])
    expect(next.customKey).toBe('')
    expect(next.customValue).toBe('')
  })

  it.each([
    ['自定义参数名', 'key'],
    ['自定义参数值', 'value'],
  ] as const)('%s 输入框 Enter 触发加入（表单快捷提交）', (label, _target) => {
    const onChange = vi.fn()
    render(<ModelParamsEditor value={draft({ customKey: 'k', customValue: 'v' })} onChange={onChange} />)
    fireEvent.keyDown(screen.getByLabelText(label), { key: 'Enter' })
    expect(onChange).toHaveBeenCalledTimes(1)
    expect((onChange.mock.calls[0][0] as ModelParamsDraft).customParams).toEqual([{ key: 'k', value: 'v' }])
  })

  it('非 Enter 按键不提交', () => {
    const onChange = vi.fn()
    render(<ModelParamsEditor value={draft({ customKey: 'k', customValue: 'v' })} onChange={onChange} />)
    fireEvent.keyDown(screen.getByLabelText('自定义参数名'), { key: 'a' })
    expect(onChange).not.toHaveBeenCalled()
  })

  it('key 为空时「加入」按钮禁用且 Enter 不提交', () => {
    const onChange = vi.fn()
    render(<ModelParamsEditor value={draft({ customKey: '  ', customValue: 'v' })} onChange={onChange} />)
    expect(screen.getByRole('button', { name: '加入' })).toBeDisabled()

    fireEvent.keyDown(screen.getByLabelText('自定义参数名'), { key: 'Enter' })
    expect(onChange).not.toHaveBeenCalled()
  })

  it('同名覆盖：重复加入同一 key 保留一份（后值胜）', () => {
    const onChange = vi.fn()
    render(
      <ModelParamsEditor
        value={draft({
          customParams: [{ key: 'dup', value: 'old' }],
          customKey: 'dup',
          customValue: 'new',
        })}
        onChange={onChange}
      />,
    )
    fireEvent.click(screen.getByRole('button', { name: '加入' }))
    expect((onChange.mock.calls[0][0] as ModelParamsDraft).customParams).toEqual([
      { key: 'dup', value: 'new' },
    ])
  })

  it('已加入参数以 key=value 形式展示，空值显示引号占位', () => {
    render(
      <ModelParamsEditor
        value={draft({ customParams: [{ key: 'a', value: '1' }, { key: 'b', value: '' }] })}
        onChange={() => {}}
      />,
    )
    expect(screen.getByText("a=1")).toBeInTheDocument()
    expect(screen.getByText("b=''")).toBeInTheDocument()
  })

  it('移除按钮只删除对应参数', () => {
    const onChange = vi.fn()
    render(
      <ModelParamsEditor
        value={draft({ customParams: [{ key: 'a', value: '1' }, { key: 'b', value: '2' }] })}
        onChange={onChange}
      />,
    )
    fireEvent.click(screen.getByRole('button', { name: '移除自定义参数 a' }))
    expect((onChange.mock.calls[0][0] as ModelParamsDraft).customParams).toEqual([{ key: 'b', value: '2' }])
  })

  it('受控回流：加入后参数出现在列表且输入框已清空', () => {
    render(<Controlled />)
    fireEvent.change(screen.getByLabelText('自定义参数名'), { target: { value: 'flag' } })
    fireEvent.change(screen.getByLabelText('自定义参数值'), { target: { value: 'true' } })
    fireEvent.click(screen.getByRole('button', { name: '加入' }))

    const chip = screen.getByText('flag=true')
    expect(chip).toBeInTheDocument()
    expect(screen.getByLabelText('自定义参数名')).toHaveValue('')
    within(chip.closest('span') as HTMLElement).getByRole('button', { name: '移除自定义参数 flag' })
  })
})
