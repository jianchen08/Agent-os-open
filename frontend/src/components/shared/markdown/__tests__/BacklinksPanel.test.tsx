/** @feature FP-0.2.四 前端Schema | @ci frontend-test */
/**
 * BacklinksPanel 反向链接侧栏契约测试（AC-M4）
 *
 * 数据面：docs/graph/backlinks.json（graph_aggregate.build_backlinks 落盘投影），
 * 经 workspaces_file_content_get + container_task_id='_local'（=项目根）只读端点
 * 读取——零后端改动。状态机：loading → list / empty / error；error 与 empty
 * 两态可辨（索引不可用 ≠ 真无反链）。
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { reportError } from '@/services/errorReporting'
import { openFile } from '@/services/fileOpener'
import { BacklinksPanel } from '../BacklinksPanel'

vi.mock('@/services/fileOpener', () => ({
  openFile: vi.fn(async () => ({ success: true, editor: 'builtin' as const })),
}))
vi.mock('@/services/errorReporting', () => ({ reportError: vi.fn() }))

const apiMock = vi.hoisted(() => ({ get: vi.fn() }))
vi.mock('@/services/api/client', () => ({ default: apiMock, apiClient: apiMock }))

/** 投影形态与 scripts/graph_aggregate.build_backlinks 落盘契约一致 */
const PROJECTION = {
  meta: { generated_at: '2026-11-21T00:00:00+00:00', commit: '' },
  by_target: {
    '20261121-b-doc': [
      { s: '20261121-a-doc', label: 'B 设计', path: 'docs/a.md', k: '依据', note: '设计真值' },
    ],
    'docs/c.md': [{ s: '20261121-a-doc', label: 'A', path: 'docs/a.md', k: '依据', note: '' }],
  },
  path_to_id: { 'docs/b.md': '20261121-b-doc', 'docs/c.md': 'docs/c.md' },
}

beforeEach(() => {
  apiMock.get.mockReset()
  vi.mocked(openFile).mockClear()
  vi.mocked(reportError).mockClear()
})

function mockProjection(content: unknown): void {
  apiMock.get.mockResolvedValue({ data: { success: true, content: JSON.stringify(content) } })
}

describe('BacklinksPanel — 反链侧栏（AC-M4）', () => {
  it('AC-M4-1 列出来源条目（label/关系/说明），计数进标题', async () => {
    mockProjection(PROJECTION)
    render(<BacklinksPanel filePath="docs/b.md" />)
    const panel = await screen.findByTestId('backlinks-panel')
    expect(panel.textContent).toContain('反向链接')
    expect(panel.textContent).toContain('(1)')
    expect(await screen.findByText('B 设计')).toBeInTheDocument()
    expect(screen.getByText(/依据/)).toBeInTheDocument()
    expect(screen.getByText(/设计真值/)).toBeInTheDocument()
  })

  it('AC-M4-2 点击条目打开来源文档（openFile 收到来源仓库路径）', async () => {
    mockProjection(PROJECTION)
    render(<BacklinksPanel filePath="docs/c.md" />)
    const entry = await screen.findByText('A')
    fireEvent.click(entry)
    expect(openFile).toHaveBeenCalledTimes(1)
    expect(openFile).toHaveBeenCalledWith('docs/a.md')
  })

  it('AC-M4-2b 条目打开失败 → reportError 上报（MF-3——失败不得静默）', async () => {
    mockProjection(PROJECTION)
    vi.mocked(openFile).mockResolvedValueOnce({
      success: false,
      editor: 'builtin',
      message: '来源文件已删除',
    })
    render(<BacklinksPanel filePath="docs/c.md" />)
    const entry = await screen.findByText('A')
    fireEvent.click(entry)
    await waitFor(() =>
      expect(reportError).toHaveBeenCalledWith('来源文件已删除', expect.anything()),
    )
  })

  it('AC-M4-3 路径不在索引（任务工作区文件/未被引用）→ 诚实空态', async () => {
    mockProjection(PROJECTION)
    render(<BacklinksPanel filePath="docs/other.md" />)
    expect(await screen.findByText('无反向链接')).toBeInTheDocument()
  })

  it('AC-M4-4 投影不可用（端点 success=false）→ 错误态，不冒充空态', async () => {
    apiMock.get.mockResolvedValue({ data: { success: false, message: '文件不存在' } })
    render(<BacklinksPanel filePath="docs/b.md" />)
    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toContain('反向链接')
  })

  it('AC-M4-5 投影 JSON 损坏 → 错误态', async () => {
    apiMock.get.mockResolvedValue({ data: { success: true, content: 'not-json{{' } })
    render(<BacklinksPanel filePath="docs/b.md" />)
    expect(await screen.findByRole('alert')).toBeInTheDocument()
  })

  it('AC-M4-6 可折叠：点击标题切换列表显隐（aria-expanded 同步）', async () => {
    mockProjection(PROJECTION)
    render(<BacklinksPanel filePath="docs/b.md" />)
    await screen.findByText('B 设计')
    const toggle = screen.getByRole('button', { name: /反向链接/ })
    expect(toggle.getAttribute('aria-expanded')).toBe('true')
    fireEvent.click(toggle)
    expect(toggle.getAttribute('aria-expanded')).toBe('false')
    expect(screen.queryByText('B 设计')).not.toBeInTheDocument()
  })

  it('AC-M4-7 换文件重新查询：filePath 变化后按新路径取反链', async () => {
    mockProjection(PROJECTION)
    const { rerender } = render(<BacklinksPanel filePath="docs/b.md" />)
    await screen.findByText('B 设计')
    rerender(<BacklinksPanel filePath="docs/other.md" />)
    expect(await screen.findByText('无反向链接')).toBeInTheDocument()
  })
})
