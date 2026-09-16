// @feature: FP-T12 补测 | @ci: frontend-test
/**
 * TableWidget 组件测试（批九覆盖率冲刺）
 *
 * dataWidget.test.tsx 已覆盖 datasourceUri 接线的最小路径；本文件补齐：
 * - 静态渲染与单元格类型渲染（布尔 ✓/✗、空值 —、对象 JSON、循环引用回退）
 * - 未定义列 / 空数据 / 加载中 / 拉取失败四种非常规态
 * - 非法列与非法行过滤
 * - 列排序：数值序（非字典序）、字符串字典序、空值排末尾、相等值、
 *   升→降→取消 三态循环、排序重置页码、箭头高亮
 * - 分页：四按钮跳转、边界禁用、行数统计
 * - 行操作：when 显隐过滤、variant 样式、DOM 确认层确认/取消、
 *   成功提示与 reloadTick 重拉、失败提示含原因、
 *   URL 模板替换（编码 + 缺列保留原样）、默认 POST/显式 DELETE
 */
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { TableWidget } from '../TableWidget'

const { toastSuccess, toastError, apiCall, apiGet } = vi.hoisted(
  () => ({
    toastSuccess: vi.fn(),
    toastError: vi.fn(),
    apiCall: vi.fn(),
    apiGet: vi.fn(),
  }),
)

vi.mock('@/components/ui/sonner', () => ({
  toast: { success: toastSuccess, error: toastError },
}))
vi.mock('@/services/api/client', () => ({
  default: Object.assign((...args: unknown[]) => apiCall(...args), {
    get: (...args: unknown[]) => apiGet(...args),
  }),
}))
vi.mock('@/services/websocket/GlobalWebSocket', () => ({
  globalWS: { subscribe: vi.fn(), unsubscribe: vi.fn() },
}))

beforeEach(() => {
  // 被测码有 apiCall().catch —— mock 工厂默认 resolved，避免裸 undefined
  apiCall.mockReset()
  apiCall.mockResolvedValue({ data: { ok: true } })
  apiGet.mockReset()
  apiGet.mockResolvedValue({ data: {} })
  toastSuccess.mockReset()
  toastError.mockReset()
})

afterEach(() => {
  cleanup()
})

const BASE_COLUMNS = [
  { key: 'name', label: '名称', sortable: true },
  { key: 'count', label: '数量' },
  { key: 'ok', label: '启用' },
  { key: 'extra', label: '附加' },
]

const BASE_DATA = [
  { name: 'alpha', count: 3, ok: true, extra: { tags: ['a'] } },
  { name: 'beta', count: null, ok: false, extra: undefined },
]

/** 表体每行的单元格文本（用 | 连接），断言渲染结果与行序 */
function bodyRows(): string[] {
  return Array.from(document.querySelectorAll('tbody tr')).map((tr) =>
    Array.from(tr.querySelectorAll('td'))
      .map((td) => td.textContent)
      .join('|'),
  )
}

describe('TableWidget 渲染与单元格', () => {
  it('静态 columns/data：标题行 + 布尔/空值/对象单元格', () => {
    render(<TableWidget columns={BASE_COLUMNS} data={BASE_DATA} title="监控面板" />)
    expect(screen.getByText('监控面板')).toBeInTheDocument()
    for (const h of ['名称', '数量', '启用', '附加']) {
      expect(screen.getByText(h)).toBeInTheDocument()
    }
    expect(screen.getByText('{"tags":["a"]}')).toBeInTheDocument()
    expect(screen.getByText('✓')).toBeInTheDocument()
    expect(screen.getByText('✗')).toBeInTheDocument()
    expect(screen.getByText('alpha')).toBeInTheDocument()
    // beta 的 count=null 与 extra=undefined 均渲染为占位符
    expect(screen.getAllByText('—')).toHaveLength(2)
  })

  it('循环引用对象单元格回退 String() 渲染', () => {
    const circular: Record<string, unknown> = { n: 1 }
    circular.self = circular
    render(<TableWidget columns={[{ key: 'a', label: 'A' }]} data={[{ a: circular }]} />)
    expect(screen.getByText('[object Object]')).toBeInTheDocument()
  })

  it('有行数据但未声明列 → 未定义列提示', () => {
    render(<TableWidget data={[{ a: 1 }]} />)
    expect(screen.getByText('未定义列')).toBeInTheDocument()
  })

  it('无列无数据 → 空态提示', () => {
    render(<TableWidget />)
    expect(screen.getByText('暂无表格数据')).toBeInTheDocument()
  })

  it('有列无行 → 表体显示无数据占位', () => {
    render(<TableWidget columns={[{ key: 'a', label: 'A' }]} data={[]} />)
    expect(screen.getByText('无数据')).toBeInTheDocument()
    expect(screen.getByText('无数据')).toHaveAttribute('colspan', '1')
  })

  it('有列无行且声明行操作 → 无数据占位 colSpan 含操作列', () => {
    render(
      <TableWidget
        columns={[{ key: 'a', label: 'A' }]}
        data={[]}
        rowActions={[{ key: 'run', label: '执行', url: '/api/run/{id}' }]}
      />,
    )
    expect(screen.getByText('无数据')).toHaveAttribute('colspan', '2')
  })

  it('列声明 width → 表头应用列宽样式', () => {
    render(
      <TableWidget
        columns={[
          { key: 'a', label: 'A', width: 120 },
          { key: 'b', label: 'B' },
        ]}
        data={[{ a: 1, b: 2 }]}
      />,
    )
    const thA = screen.getByText('A').closest('th') as HTMLElement
    const thB = screen.getByText('B').closest('th') as HTMLElement
    expect(thA.style.width).toBe('120px')
    expect(thB.style.width).toBe('')
  })

  it('非法列与非法行被过滤', () => {
    render(
      <TableWidget
        columns={[{ key: 'a', label: 'A' }, null, 'junk', 42, { label: '缺key' }]}
        data={[{ a: 1 }, 'x', null, 7]}
      />,
    )
    expect(screen.getByText('A')).toBeInTheDocument()
    expect(screen.queryByText('缺key')).not.toBeInTheDocument()
    expect(bodyRows()).toEqual(['1'])
  })
})

describe('TableWidget datasourceUri 状态', () => {
  it('拉取成功：响应声明列渲染（未传 columns 时回退响应列）', async () => {
    apiGet.mockResolvedValue({
      data: { columns: [{ key: 'id', label: 'ID' }], rows: [{ id: 7 }] },
    })
    render(<TableWidget datasourceUri="/api/x" />)
    expect(await screen.findByText('ID')).toBeInTheDocument()
    expect(bodyRows()).toEqual(['7'])
  })

  it('首拉挂起 → 显示加载数据占位而非空态', async () => {
    apiGet.mockReturnValue(new Promise(() => {}))
    render(<TableWidget datasourceUri="/api/slow" />)
    expect(await screen.findByText('加载数据…')).toBeInTheDocument()
    expect(screen.queryByText('暂无表格数据')).not.toBeInTheDocument()
  })

  it('拉取失败 → 错误提示含原因', async () => {
    apiGet.mockRejectedValue(new Error('网络中断'))
    render(<TableWidget datasourceUri="/api/bad" />)
    expect(await screen.findByRole('alert')).toHaveTextContent('网络中断')
  })
})

describe('TableWidget 排序', () => {
  it('数值列三态循环：升序 → 降序 → 取消恢复原始顺序', () => {
    render(
      <TableWidget
        columns={[{ key: 'n', label: 'N', sortable: true }]}
        data={[{ n: 3 }, { n: 1 }, { n: 2 }]}
      />,
    )
    fireEvent.click(screen.getByText('N'))
    expect(bodyRows()).toEqual(['1', '2', '3'])
    fireEvent.click(screen.getByText('N'))
    expect(bodyRows()).toEqual(['3', '2', '1'])
    fireEvent.click(screen.getByText('N'))
    expect(bodyRows()).toEqual(['3', '1', '2'])
  })

  it('数值按数值序比较（10 排 9 后），字符串按字典序升降', () => {
    const { unmount } = render(
      <TableWidget
        columns={[{ key: 'n', label: 'N', sortable: true }]}
        data={[{ n: 10 }, { n: 9 }, { n: 2 }]}
      />,
    )
    fireEvent.click(screen.getByText('N'))
    expect(bodyRows()).toEqual(['2', '9', '10'])
    unmount()

    render(
      <TableWidget
        columns={[{ key: 's', label: 'S', sortable: true }]}
        data={[{ s: 'banana' }, { s: 'apple' }, { s: 'cherry' }]}
      />,
    )
    fireEvent.click(screen.getByText('S'))
    expect(bodyRows()).toEqual(['apple', 'banana', 'cherry'])
    fireEvent.click(screen.getByText('S'))
    expect(bodyRows()).toEqual(['cherry', 'banana', 'apple'])
  })

  it('空值（null/undefined）排末尾，相等值不动', () => {
    render(
      <TableWidget
        columns={[{ key: 'v', label: 'V', sortable: true }]}
        data={[{ v: undefined }, { v: 5 }, { v: null }, { v: 5 }]}
      />,
    )
    fireEvent.click(screen.getByText('V'))
    expect(bodyRows()).toEqual(['5', '5', '—', '—'])
  })

  it('排序点击重置回第 1 页，箭头高亮随方向切换', () => {
    render(
      <TableWidget
        columns={[{ key: 'n', label: 'N', sortable: true }]}
        data={[{ n: 1 }, { n: 2 }, { n: 3 }]}
        pageSize={1}
      />,
    )
    expect(screen.getByText('共 3 条，第 1/3 页')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '下一页 ›' }))
    expect(screen.getByText('共 3 条，第 2/3 页')).toBeInTheDocument()

    fireEvent.click(screen.getByText('N'))
    expect(screen.getByText('共 3 条，第 1/3 页')).toBeInTheDocument()

    const arrows = Array.from(
      (screen.getByText('N').closest('th') as HTMLElement).querySelectorAll('span'),
    ).filter((s) => s.textContent === '▲' || s.textContent === '▼')
    expect(arrows.find((s) => s.textContent === '▲')).toHaveClass('text-foreground')
    expect(arrows.find((s) => s.textContent === '▼')).toHaveClass(
      'text-muted-foreground/40',
    )

    fireEvent.click(screen.getByText('N'))
    expect(arrows.find((s) => s.textContent === '▼')).toHaveClass('text-foreground')
    expect(arrows.find((s) => s.textContent === '▲')).toHaveClass(
      'text-muted-foreground/40',
    )
  })
})

describe('TableWidget 分页', () => {
  it('四按钮跳转与边界禁用', () => {
    render(
      <TableWidget
        columns={[{ key: 'n', label: 'N' }]}
        data={[{ n: 1 }, { n: 2 }, { n: 3 }, { n: 4 }, { n: 5 }]}
        pageSize={2}
      />,
    )
    const first = () => screen.getByRole('button', { name: '首页' })
    const prev = () => screen.getByRole('button', { name: '‹ 上一页' })
    const next = () => screen.getByRole('button', { name: '下一页 ›' })
    const last = () => screen.getByRole('button', { name: '末页' })

    expect(first()).toBeDisabled()
    expect(prev()).toBeDisabled()

    fireEvent.click(last())
    expect(screen.getByText('共 5 条，第 3/3 页')).toBeInTheDocument()
    expect(bodyRows()).toEqual(['5'])
    expect(next()).toBeDisabled()
    expect(last()).toBeDisabled()

    fireEvent.click(first())
    expect(screen.getByText('共 5 条，第 1/3 页')).toBeInTheDocument()
    expect(bodyRows()).toEqual(['1', '2'])

    fireEvent.click(next())
    expect(screen.getByText('共 5 条，第 2/3 页')).toBeInTheDocument()
    expect(bodyRows()).toEqual(['3', '4'])

    fireEvent.click(prev())
    expect(screen.getByText('共 5 条，第 1/3 页')).toBeInTheDocument()
  })
})

describe('TableWidget 行操作', () => {
  const ACTIONS = [
    { key: 'run', label: '执行', url: '/api/run/{id}' },
    {
      key: 'stop',
      label: '停止',
      url: '/api/stop/{id}',
      when: { key: 'enabled', equals: true },
    },
    {
      key: 'del',
      label: '删除',
      url: '/api/del/{id}',
      variant: 'destructive',
      method: 'DELETE',
      confirm: '确定删除?',
    },
    { key: 'mark', label: '标记', url: '/api/mark/{id}', variant: 'outline' },
  ]

  it('操作列渲染 + when 条件显隐 + variant 样式', () => {
    render(
      <TableWidget
        columns={[{ key: 'name', label: '名称' }]}
        data={[
          { id: 'a1', name: 'r1', enabled: true },
          { id: 'b2', name: 'r2', enabled: false },
        ]}
        rowActions={ACTIONS}
      />,
    )
    expect(screen.getByText('操作')).toBeInTheDocument()
    // 无 when 的动作恒显（两行各一）；有 when 的只出现在 enabled=true 行
    expect(screen.getAllByTestId('row-action-run')).toHaveLength(2)
    expect(screen.getAllByTestId('row-action-mark')).toHaveLength(2)
    expect(screen.getAllByTestId('row-action-del')).toHaveLength(2)
    expect(screen.getAllByTestId('row-action-stop')).toHaveLength(1)
    expect(screen.getAllByTestId('row-action-del')[0]).toHaveClass('text-status-error')
    expect(screen.getAllByTestId('row-action-mark')[0]).toHaveClass('border-border')
    expect(screen.getAllByTestId('row-action-run')[0]).toHaveClass('text-primary')
  })

  it('无 confirm：默认 POST 直发 + URL 模板替换（编码/缺列保留）+ 成功提示 + 重拉', async () => {
    apiGet.mockResolvedValue({ data: { rows: [{ id: 'a b', name: 'r1' }] } })
    render(
      <TableWidget
        datasourceUri="/api/list"
        rowActions={[{ key: 'run', label: '执行', url: '/api/run/{id}/{missing}' }]}
      />,
    )
    await screen.findByText('r1')
    expect(apiGet).toHaveBeenCalledTimes(1)

    fireEvent.click(screen.getByTestId('row-action-run'))
    await waitFor(() => expect(apiCall).toHaveBeenCalled())
    expect(apiCall).toHaveBeenCalledWith({
      method: 'POST',
      url: '/api/run/a%20b/{missing}',
    })
    expect(toastSuccess).toHaveBeenCalledWith('操作成功')
    // 操作成功后 reloadTick 递增触发重拉
    await waitFor(() => expect(apiGet).toHaveBeenCalledTimes(2))
  })

  it('显式 method=DELETE：点击先出 DOM 确认层（零请求）→ 确认后才发 + 缺省成功文案', async () => {
    render(
      <TableWidget
        columns={[{ key: 'name', label: '名称' }]}
        data={[{ id: 'a1', name: 'r1' }]}
        rowActions={[ACTIONS[2]]}
      />,
    )
    fireEvent.click(screen.getByTestId('row-action-del'))
    // BUG-23：确认层必须是 DOM 可见（原生 confirm 在自动化浏览器被静默
    // auto-dismiss，点击流无声中断）；确认前零请求零副作用
    const dialog = await screen.findByRole('dialog', { name: '确认操作' })
    expect(dialog).toHaveTextContent('确定删除?')
    expect(apiCall).not.toHaveBeenCalled()
    expect(toastSuccess).not.toHaveBeenCalled()

    fireEvent.click(screen.getByRole('button', { name: '确认' }))
    await waitFor(() =>
      expect(apiCall).toHaveBeenCalledWith({ method: 'DELETE', url: '/api/del/a1' }),
    )
    expect(toastSuccess).toHaveBeenCalledWith('操作成功')
    await waitFor(() =>
      expect(screen.queryByRole('dialog', { name: '确认操作' })).not.toBeInTheDocument(),
    )
  })

  it('确认层点取消 → 不发请求不提示，弹层关闭', async () => {
    render(
      <TableWidget
        columns={[{ key: 'name', label: '名称' }]}
        data={[{ id: 'a1', name: 'r1' }]}
        rowActions={[{ key: 'run', label: '执行', url: '/api/run/{id}', confirm: '确定?' }]}
      />,
    )
    fireEvent.click(screen.getByTestId('row-action-run'))
    await screen.findByRole('dialog', { name: '确认操作' })
    fireEvent.click(screen.getByRole('button', { name: '取消' }))
    await waitFor(() =>
      expect(screen.queryByRole('dialog', { name: '确认操作' })).not.toBeInTheDocument(),
    )
    expect(apiCall).not.toHaveBeenCalled()
    expect(toastSuccess).not.toHaveBeenCalled()
  })

  it('请求失败 → 错误提示带 Error 原因', async () => {
    apiCall.mockRejectedValue(new Error('下游炸了'))
    render(
      <TableWidget
        columns={[{ key: 'name', label: '名称' }]}
        data={[{ id: 'a1', name: 'r1' }]}
        rowActions={[{ key: 'run', label: '执行', url: '/api/run/{id}' }]}
      />,
    )
    fireEvent.click(screen.getByTestId('row-action-run'))
    await waitFor(() =>
      expect(toastError).toHaveBeenCalledWith('操作失败', { description: '下游炸了' }),
    )
  })

  it('请求失败（非 Error 值）→ 错误提示带字符串化原因', async () => {
    apiCall.mockRejectedValue('字符串原因')
    render(
      <TableWidget
        columns={[{ key: 'name', label: '名称' }]}
        data={[{ id: 'a1', name: 'r1' }]}
        rowActions={[{ key: 'run', label: '执行', url: '/api/run/{id}' }]}
      />,
    )
    fireEvent.click(screen.getByTestId('row-action-run'))
    await waitFor(() =>
      expect(toastError).toHaveBeenCalledWith('操作失败', { description: '字符串原因' }),
    )
  })
})
