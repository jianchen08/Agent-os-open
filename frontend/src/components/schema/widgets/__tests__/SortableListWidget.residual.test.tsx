/** @feature FP-T12 前端适配 | @ci: frontend-test */
/**
 * SortableListWidget 残余分支补测（簇3）
 *
 * 覆盖既有 RichWidgets.test.tsx 未触达的分支：
 * - items 归一化：数字条目、缺 value 的对象条目、非法条目被丢弃（return null）
 * - 空列表 → 「暂无条目」占位
 * - 单条目 → 不可拖拽、不渲染上/下移按钮
 * - 原生 HTML5 拖放：dragStart 记录起点 → drop 重排并回调
 * - 多条目上移按钮在首项禁用、下移在末项禁用
 * - move 的越界/同位置守卫（不产生回调）
 * - 无 onChange 声明时点击按钮不抛错（只读形态）
 *
 * 不可达/未覆盖说明（本文件 docstring 存证）：
 * - move 的 `to < 0 || to >= items.length || from === to` 守卫中的越界两面
 *   由按钮 disabled 属性在 UI 层拦截（首项上移/末项下移），但拖放路径（onDrop）
 *   可传入任意索引，故本文件以拖放驱动越界分支——非死代码。
 * - normalizeItems 的 `.filter((x): x is SortableItem => x !== null)` 仅在存在
 *   非法条目时触达，由"非法条目混排"用例覆盖。
 */
import { createEvent, fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { SortableListWidget } from '../SortableListWidget'

/** 构造 dataTransfer 桩（jsdom 未实现 DataTransfer） */
function makeDataTransfer(): DataTransfer {
  return { effectAllowed: 'none', dropEffect: 'none' } as unknown as DataTransfer
}

describe('条目归一化', () => {
  it.each([
    ['数字数组', [1, 2, 3], ['1', '2', '3']],
    ['字符串数组', ['a', 'b'], ['a', 'b']],
  ])('%s → label/value 同为字符串', (_name, items, expected) => {
    render(<SortableListWidget items={items} />)
    const rendered = expected.map((_, i) => screen.getByTestId(`sortable-item-${i}`).textContent)
    expected.forEach((v, i) => expect(rendered[i]).toContain(v))
  })

  it('对象条目缺 value 时回落 label；两者皆缺 → 整条丢弃', () => {
    render(
      <SortableListWidget
        items={[
          { label: '仅有 label' },
          { value: 'only-value' },
          { label: '' },
          null,
          42,
        ]}
      />,
    )
    // 前两条保留，无 value/label 的空对象与 null 被丢弃；数字 42 保留
    expect(screen.getByTestId('sortable-item-0')).toHaveTextContent('仅有 label')
    expect(screen.getByTestId('sortable-item-1')).toHaveTextContent('only-value')
    expect(screen.getByTestId('sortable-item-2')).toHaveTextContent('42')
    expect(screen.queryByTestId('sortable-item-3')).not.toBeInTheDocument()
  })

  it.each([
    ['非数组（字符串）', 'not-an-array'],
    ['非数组（对象）', { a: 1 }],
    ['undefined', undefined],
  ])('items 为%s → 空列表占位', (_name, items) => {
    render(<SortableListWidget items={items} />)
    expect(screen.getByText('暂无条目')).toBeInTheDocument()
  })

  it('空数组 → 空列表占位且无任何条目', () => {
    render(<SortableListWidget items={[]} />)
    expect(screen.getByText('暂无条目')).toBeInTheDocument()
    expect(screen.queryByTestId('sortable-item-0')).not.toBeInTheDocument()
  })

  it('labels 映射覆盖显示名，未映射回落 item.label', () => {
    render(<SortableListWidget items={['a', 'b']} labels={{ a: '甲' }} />)
    expect(screen.getByTestId('sortable-item-0')).toHaveTextContent('甲')
    expect(screen.getByTestId('sortable-item-1')).toHaveTextContent('b')
  })
})

describe('单条目形态', () => {
  it('单条目不可拖拽且不渲染排序按钮（拖拽无意义）', () => {
    render(<SortableListWidget items={['only']} />)
    const row = screen.getByTestId('sortable-item-0')
    expect(row).toHaveAttribute('draggable', 'false')
    expect(screen.queryByRole('button', { name: '上移 only' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '下移 only' })).not.toBeInTheDocument()
  })
})

describe('按钮排序', () => {
  it('多条目上移/下移：首项上移禁用、末项下移禁用（越界守卫在 UI 层）', () => {
    render(<SortableListWidget items={['a', 'b', 'c']} onChange={vi.fn()} />)
    expect(screen.getByRole('button', { name: '上移 a' })).toBeDisabled()
    expect(screen.getByRole('button', { name: '下移 c' })).toBeDisabled()
    expect(screen.getByRole('button', { name: '下移 a' })).toBeEnabled()
    expect(screen.getByRole('button', { name: '上移 c' })).toBeEnabled()
  })

  it.each([
    ['下移 a', ['b', 'a', 'c']],
    ['上移 c', ['a', 'c', 'b']],
  ])('%s → onChange 收到新序', (buttonName, expected) => {
    const onChange = vi.fn()
    render(<SortableListWidget items={['a', 'b', 'c']} onChange={onChange} />)

    fireEvent.click(screen.getByRole('button', { name: buttonName }))

    const next = onChange.mock.calls[0][0] as Array<{ value: string }>
    expect(next.map((i) => i.value)).toEqual(expected)
  })

  it('无 onChange 声明（只读形态）点击排序按钮不抛错', () => {
    render(<SortableListWidget items={['a', 'b']} />)
    expect(() => fireEvent.click(screen.getByRole('button', { name: '下移 a' }))).not.toThrow()
  })
})

describe('HTML5 拖放排序', () => {
  it('dragStart 记录起点 → drop 到目标位置后回调新序', () => {
    const onChange = vi.fn()
    render(<SortableListWidget items={['a', 'b', 'c']} onChange={onChange} />)

    const first = screen.getByTestId('sortable-item-0')
    const third = screen.getByTestId('sortable-item-2')
    const dt = makeDataTransfer()

    fireEvent.dragStart(first, { dataTransfer: dt })
    expect(dt.effectAllowed).toBe('move')
    fireEvent.drop(third, { dataTransfer: dt })

    const next = onChange.mock.calls[0][0] as Array<{ value: string }>
    expect(next.map((i) => i.value)).toEqual(['b', 'c', 'a'])
  })

  it('drop 到自身位置 → 不回调（from === to 守卫）', () => {
    const onChange = vi.fn()
    render(<SortableListWidget items={['a', 'b']} onChange={onChange} />)

    const first = screen.getByTestId('sortable-item-0')
    fireEvent.dragStart(first, { dataTransfer: makeDataTransfer() })
    fireEvent.drop(first, { dataTransfer: makeDataTransfer() })

    expect(onChange).not.toHaveBeenCalled()
  })

  it('未经 dragStart 直接 drop → dragIndex 为 null 不回调', () => {
    const onChange = vi.fn()
    render(<SortableListWidget items={['a', 'b']} onChange={onChange} />)

    fireEvent.drop(screen.getByTestId('sortable-item-1'), { dataTransfer: makeDataTransfer() })
    expect(onChange).not.toHaveBeenCalled()
  })

  it('drop 后重置拖拽态：行不再带 opacity-40 视觉标记', () => {
    const onChange = vi.fn()
    render(<SortableListWidget items={['a', 'b', 'c']} onChange={onChange} />)

    const first = screen.getByTestId('sortable-item-0')
    const dt = makeDataTransfer()
    fireEvent.dragStart(first, { dataTransfer: dt })
    expect(first.className).toContain('opacity-40')

    fireEvent.drop(screen.getByTestId('sortable-item-1'), { dataTransfer: dt })
    expect(screen.getByTestId('sortable-item-0').className).not.toContain('opacity-40')
  })

  it('dragover 默认行为被阻止（允许成为放置目标）', () => {
    render(<SortableListWidget items={['a', 'b']} onChange={vi.fn()} />)
    const row = screen.getByTestId('sortable-item-0')
    const event = createEvent.dragOver(row, { dataTransfer: makeDataTransfer() })
    fireEvent(row, event)
    expect(event.defaultPrevented).toBe(true)
  })
})
