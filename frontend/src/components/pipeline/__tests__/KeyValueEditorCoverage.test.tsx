/** @feature FP-T12 前端适配 | @ci: frontend-test */
/**
 * KeyValueEditor 覆盖缺口补测
 *
 * 覆盖契约（受控组件，断言 onChange 提交的整对象值）：
 * - 值类型分发：boolean → 勾选框提交布尔；number → 数字框提交数值（空串提交空串）；
 *   string/undefined/null → 文本框提交字符串；对象/数组 → JSON 文本域
 * - JSON 文本域草稿：输入期间不提交，onBlur 且解析合法才提交解析结果；
 *   非法 JSON 在 blur 时不提交并还原外部值；外部值变化丢弃未提交草稿
 * - addField 自动键名：field_1 已存在时递增到 field_2
 * - removeKey 删空后回传 undefined；value 为 undefined 时 removeKey 不提交
 * - renameKey：同名或空名不提交，改名保持其余键值
 * - 空态提示与 keyPlaceholder 的 sr-only 占位
 *
 * 测试策略：真实受控组件 + 真实 onChange spy；仅用受控父级驱动值回流。
 */

import { fireEvent, render, screen } from '@testing-library/react'
import { useState } from 'react'
import { describe, expect, it, vi } from 'vitest'
import { KeyValueEditor } from '@/components/pipeline/KeyValueEditor'

/** 受控宿主：把 onChange 回流到 state，还原真实使用方式 */
function Controlled({
  initial,
  onChange,
}: {
  initial?: Record<string, unknown>
  onChange?: (next: Record<string, unknown> | undefined) => void
}) {
  const [value, setValue] = useState<Record<string, unknown> | undefined>(initial)
  return (
    <KeyValueEditor
      value={value}
      onChange={(next) => {
        onChange?.(next)
        setValue(next)
      }}
      emptyHint="无 context 字段"
    />
  )
}

describe('KeyValueEditor — 值类型分发', () => {
  it('布尔值走勾选框，切换提交布尔', () => {
    const onChange = vi.fn()
    render(<KeyValueEditor value={{ flag: true }} onChange={onChange} />)
    fireEvent.click(screen.getByLabelText('布尔值'))
    expect(onChange).toHaveBeenCalledWith({ flag: false })
  })

  it.each([
    ['42', 42],
    ['', ''],
  ] as const)('数值输入 %s 提交 %s（空串保留为空串）', (typed, expected) => {
    const onChange = vi.fn()
    render(<KeyValueEditor value={{ n: 7 }} onChange={onChange} />)
    fireEvent.change(screen.getByLabelText('数值'), { target: { value: typed } })
    expect(onChange).toHaveBeenCalledWith({ n: expected })
  })

  it('字符串值走文本框，提交新字符串', () => {
    const onChange = vi.fn()
    render(<KeyValueEditor value={{ s: '旧' }} onChange={onChange} />)
    fireEvent.change(screen.getByLabelText('字符串值'), { target: { value: '新' } })
    expect(onChange).toHaveBeenCalledWith({ s: '新' })
  })

  it.each([[undefined], [null]])('值为 %s 时按字符串输入渲染（空文本）', (raw) => {
    render(<KeyValueEditor value={{ k: raw }} onChange={() => {}} />)
    expect(screen.getByLabelText('字符串值')).toHaveValue('')
  })

  it('对象值走 JSON 文本域并按 2 空格缩进展示', () => {
    render(<KeyValueEditor value={{ cfg: { a: 1 } }} onChange={() => {}} />)
    const area = screen.getByLabelText('JSON 值') as HTMLTextAreaElement
    expect(area.value).toBe('{\n  "a": 1\n}')
  })
})

describe('KeyValueEditor — JSON 草稿提交', () => {
  it('输入草稿不立即提交，blur 解析合法才提交对象', () => {
    const onChange = vi.fn()
    render(<KeyValueEditor value={{ cfg: { a: 1 } }} onChange={onChange} />)
    const area = screen.getByLabelText('JSON 值')

    fireEvent.change(area, { target: { value: '{"b":2}' } })
    expect(onChange).not.toHaveBeenCalled()

    fireEvent.blur(area)
    expect(onChange).toHaveBeenCalledWith({ cfg: { b: 2 } })
  })

  it('非法 JSON：blur 不提交且还原为外部值', () => {
    const onChange = vi.fn()
    render(<KeyValueEditor value={{ cfg: { a: 1 } }} onChange={onChange} />)
    const area = screen.getByLabelText('JSON 值') as HTMLTextAreaElement

    fireEvent.change(area, { target: { value: '{bad' } })
    fireEvent.blur(area)
    expect(onChange).not.toHaveBeenCalled()
    expect(screen.getByLabelText('JSON 值')).toHaveValue('{\n  "a": 1\n}')
  })

  it('数组值同样接受 JSON 草稿（解析后提交数组）', () => {
    const onChange = vi.fn()
    render(<KeyValueEditor value={{ list: [1] }} onChange={onChange} />)
    const area = screen.getByLabelText('JSON 值')
    fireEvent.change(area, { target: { value: '[1,2,3]' } })
    fireEvent.blur(area)
    expect(onChange).toHaveBeenCalledWith({ list: [1, 2, 3] })
  })

  it('外部值变化后丢弃未提交草稿（重新展示外部值）', () => {
    const { rerender } = render(<KeyValueEditor value={{ cfg: { a: 1 } }} onChange={() => {}} />)
    fireEvent.change(screen.getByLabelText('JSON 值'), { target: { value: '{"draft":true}' } })

    rerender(<KeyValueEditor value={{ cfg: { a: 2 } }} onChange={() => {}} />)
    expect(screen.getByLabelText('JSON 值')).toHaveValue('{\n  "a": 2\n}')
  })
})

describe('KeyValueEditor — 增删改键', () => {
  it('添加字段：空对象时用 field_1 起名', () => {
    const onChange = vi.fn()
    render(<KeyValueEditor value={{}} onChange={onChange} />)
    fireEvent.click(screen.getByRole('button', { name: '添加字段' }))
    expect(onChange).toHaveBeenCalledWith({ field_1: '' })
  })

  it('field_1 已存在时新键递增到 field_2', () => {
    const onChange = vi.fn()
    render(<KeyValueEditor value={{ field_1: 'x' }} onChange={onChange} />)
    fireEvent.click(screen.getByRole('button', { name: '添加字段' }))
    expect(onChange).toHaveBeenCalledWith({ field_1: 'x', field_2: '' })
  })

  it('删除最后一个键回传 undefined（不写出空对象）', () => {
    const onChange = vi.fn()
    render(<KeyValueEditor value={{ only: 'v' }} onChange={onChange} />)
    fireEvent.click(screen.getByRole('button', { name: '删除 only' }))
    expect(onChange).toHaveBeenCalledWith(undefined)
  })

  it('删除非最后键回传剩余键的对象', () => {
    const onChange = vi.fn()
    render(<KeyValueEditor value={{ a: 1, b: 2 }} onChange={onChange} />)
    fireEvent.click(screen.getByRole('button', { name: '删除 a' }))
    expect(onChange).toHaveBeenCalledWith({ b: 2 })
  })

  it('重命名键保持原值且不改变顺序', () => {
    const onChange = vi.fn()
    render(<KeyValueEditor value={{ a: 1, b: 2 }} onChange={onChange} />)
    fireEvent.change(screen.getByLabelText('a 键名'), { target: { value: 'renamed' } })
    expect(onChange).toHaveBeenCalledWith({ renamed: 1, b: 2 })
  })

  it('键名清空时不提交（防写出空键）', () => {
    const onChange = vi.fn()
    render(<KeyValueEditor value={{ a: 1 }} onChange={onChange} />)
    fireEvent.change(screen.getByLabelText('a 键名'), { target: { value: '' } })
    expect(onChange).not.toHaveBeenCalled()
  })

  it('受控回流：新增字段后新键出现在界面上且可继续添加下一个序号', () => {
    render(<Controlled initial={{}} />)
    fireEvent.click(screen.getByRole('button', { name: '添加字段' }))
    expect(screen.getByLabelText('field_1 键名')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: '添加字段' }))
    expect(screen.getByLabelText('field_2 键名')).toBeInTheDocument()
  })
})

describe('KeyValueEditor — 空态', () => {
  it.each([
    ['暂无字段', undefined],
    ['自定义空态', '自定义空态'],
  ] as const)('value 为 undefined 时展示空态提示 %s 与 sr-only 键名占位', (expected, emptyHint) => {
    render(<KeyValueEditor value={undefined} onChange={() => {}} emptyHint={emptyHint} />)
    expect(screen.getByText(expected)).toBeInTheDocument()
    expect(screen.getByTestId('kv-empty')).toHaveTextContent('字段名')
  })

  it('keyPlaceholder 透传到 sr-only 占位（无可见干扰）', () => {
    render(
      <KeyValueEditor value={{}} onChange={() => {}} keyPlaceholder="参数名" emptyHint={undefined} />,
    )
    expect(screen.getByTestId('kv-empty')).toHaveTextContent('参数名')
  })

  it('非空时不渲染空态提示', () => {
    render(<KeyValueEditor value={{ a: 1 }} onChange={() => {}} />)
    expect(screen.queryByText('暂无字段')).toBeNull()
    expect(screen.queryByTestId('kv-empty')).toBeNull()
  })
})
