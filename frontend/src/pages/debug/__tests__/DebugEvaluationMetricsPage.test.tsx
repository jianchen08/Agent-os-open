// @feature: FP-T12 补测 | @ci: frontend-test
/**
 * DebugEvaluationMetricsPage 调试评估指标页测试
 *
 * 覆盖：loading/error/empty/列表四态、指标卡片字段投影（红线/状态/阈值/层级/
 * 权重）、展开详情（评估器/来源/使用次数/平均耗时/包含指标/标签）与收起、
 * 分类过滤显式重拉（含"全部"复位）、embedded 模式不渲染页头。
 *
 * mock 约定：仅网络层（getEvaluationMetrics）为外部依赖整模块 mock，
 * query 链路（useEvaluationMetricsQuery）走真实现 + 每用例独立 QueryClient。
 */
import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { getEvaluationMetrics } from '@/services/api/evaluationMetrics'
import type { EvaluationMetric } from '@/services/api/evaluationMetrics'
import { createTestQueryClient, renderWithProviders } from '@/test/renderWithProviders'
import { DebugEvaluationMetricsPage } from '../DebugEvaluationMetricsPage'

vi.mock('@/services/api/evaluationMetrics', () => ({
  getEvaluationMetrics: vi.fn(),
}))

const mockGetEvaluationMetrics = vi.mocked(getEvaluationMetrics)

function makeMetric(overrides: Partial<EvaluationMetric> = {}): EvaluationMetric {
  return {
    id: 'm-1',
    name: '回答质量',
    description: '衡量回答与提问的相关性和准确性',
    category: 'quality',
    evaluator_type: 'llm_judge',
    evaluator_id: 'eval-quality-1',
    level: 2,
    is_red_line: true,
    default_weight: 1.5,
    default_pass_threshold: 0.9,
    source: 'system',
    status: 'active',
    usage_count: 12,
    success_count: 10,
    avg_execution_time: 1234.6,
    created_at: '2026-09-01T00:00:00Z',
    ...overrides,
  } as EvaluationMetric
}

function renderPage(props: { embedded?: boolean } = {}) {
  return renderWithProviders(<DebugEvaluationMetricsPage {...props} />, {
    queryClient: createTestQueryClient(),
  })
}

beforeEach(() => {
  mockGetEvaluationMetrics.mockResolvedValue({ metrics: [], total: 0 })
})

afterEach(() => {
  vi.clearAllMocks()
})

describe('DebugEvaluationMetricsPage 四态', () => {
  it('加载态：pending 且无数据时渲染骨架，数据到达后渲染列表', async () => {
    mockGetEvaluationMetrics.mockImplementation(
      () =>
        new Promise((resolve) =>
          setTimeout(() => resolve({ metrics: [makeMetric()], total: 1 }), 30),
        ),
    )
    renderPage()
    expect(document.querySelector('.animate-spin')).toBeInTheDocument()
    expect(screen.queryByText('回答质量')).not.toBeInTheDocument()

    await screen.findByText('回答质量')
    expect(document.querySelector('.animate-spin')).not.toBeInTheDocument()
  })

  it('错误态：Error 实例的 message 透传 ErrorState', async () => {
    mockGetEvaluationMetrics.mockRejectedValue(new Error('评估服务后端 500'))
    renderPage()
    await screen.findByText('评估服务后端 500')
    expect(screen.queryByText('暂无数据')).not.toBeInTheDocument()
  })

  it('错误态：非 Error 拒绝值显示通用文案', async () => {
    mockGetEvaluationMetrics.mockRejectedValue('raw-string-rejection')
    renderPage()
    await screen.findByText('获取评估指标失败')
  })

  it('空态：零指标显示"暂无数据"与计数 0', async () => {
    mockGetEvaluationMetrics.mockResolvedValue({ metrics: [], total: 0 })
    renderPage()
    await screen.findByText('暂无数据')
    expect(screen.getByText('共 0 个指标')).toBeInTheDocument()
  })
})

describe('DebugEvaluationMetricsPage 列表字段投影', () => {
  it('红线/状态/分类/层级/权重/阈值按字段渲染，非红线指标无红线标', async () => {
    mockGetEvaluationMetrics.mockResolvedValue({
      metrics: [
        makeMetric(),
        makeMetric({
          id: 'm-2',
          name: '响应时延',
          category: 'performance',
          is_red_line: false,
          level: 1,
          default_weight: 0.5,
          status: 'draft',
        }),
      ],
      total: 2,
    })
    renderPage()

    await screen.findByText('回答质量')
    expect(screen.getByText('响应时延')).toBeInTheDocument()
    expect(screen.getByText('共 2 个指标')).toBeInTheDocument()
    // 红线标仅出现在 is_red_line 的指标上
    expect(screen.getByText('红线')).toBeInTheDocument()
    // 状态/分类/层级/权重/阈值（分类与过滤按钮同名，断言圈定在卡片内）
    expect(screen.getByText('active')).toBeInTheDocument()
    expect(screen.getByText('draft')).toBeInTheDocument()
    const first = screen.getByText('回答质量').closest('.cursor-pointer')!
    const second = screen.getByText('响应时延').closest('.cursor-pointer')!
    expect(within(first).getByText('quality')).toBeInTheDocument()
    expect(within(second).getByText('performance')).toBeInTheDocument()
    expect(within(first).getByText('L2')).toBeInTheDocument()
    expect(within(second).getByText('L1')).toBeInTheDocument()
    expect(within(first).getByText('权重: 1.5')).toBeInTheDocument()
    expect(within(second).getByText('权重: 0.5')).toBeInTheDocument()
    expect(within(first).getByText('阈值: 0.9')).toBeInTheDocument()
  })

  it('无 default_pass_threshold 的指标不渲染阈值标', async () => {
    const { default_pass_threshold: _threshold, ...metric } = makeMetric()
    mockGetEvaluationMetrics.mockResolvedValue({ metrics: [metric], total: 1 })
    renderPage()
    await screen.findByText('回答质量')
    expect(screen.queryByText(/阈值:/)).not.toBeInTheDocument()
  })
})

describe('DebugEvaluationMetricsPage 展开详情', () => {
  function fullDetailMetric(): EvaluationMetric {
    return makeMetric({
      includes: ['sub-a', 'sub-b'],
      tags: ['核心', 'P0'],
    })
  }

  it('点击卡片展开：评估器/来源/使用次数/平均耗时/包含指标/标签全部显示，再点收起', async () => {
    mockGetEvaluationMetrics.mockResolvedValue({ metrics: [fullDetailMetric()], total: 1 })
    renderPage()
    await screen.findByText('回答质量')

    // 未展开：详情不可见
    expect(screen.queryByText(/评估器：/)).not.toBeInTheDocument()

    fireEvent.click(screen.getByText('回答质量').closest('.cursor-pointer')!)
    // 标签 span 与值文本是兄弟节点，断言落在父级 div 上
    expect(screen.getByText('评估器：').parentElement).toHaveTextContent('llm_judge (eval-quality-1)')
    expect(screen.getByText('来源：').parentElement).toHaveTextContent('system')
    expect(screen.getByText('使用次数：').parentElement).toHaveTextContent('12 (成功 10)')
    // avg_execution_time 1234.6 → toFixed(0)
    expect(screen.getByText('平均耗时：').parentElement).toHaveTextContent('1235ms')
    expect(screen.getByText(/包含指标：/)).toBeInTheDocument()
    expect(screen.getByText('sub-a')).toBeInTheDocument()
    expect(screen.getByText('sub-b')).toBeInTheDocument()
    expect(screen.getByText('核心')).toBeInTheDocument()
    expect(screen.getByText('P0')).toBeInTheDocument()

    // 再点同一卡片收起
    fireEvent.click(screen.getByText('回答质量').closest('.cursor-pointer')!)
    expect(screen.queryByText(/评估器：/)).not.toBeInTheDocument()
  })

  it('可选字段缺省（无 avg/includes/tags）：对应行不渲染', async () => {
    const metric = makeMetric({ id: 'm-min', name: '最小指标', avg_execution_time: undefined })
    mockGetEvaluationMetrics.mockResolvedValue({ metrics: [metric], total: 1 })
    renderPage()
    await screen.findByText('最小指标')

    fireEvent.click(screen.getByText('最小指标').closest('.cursor-pointer')!)
    expect(screen.getByText(/评估器：/)).toBeInTheDocument()
    expect(screen.queryByText(/平均耗时：/)).not.toBeInTheDocument()
    expect(screen.queryByText(/包含指标：/)).not.toBeInTheDocument()
    expect(screen.queryByText('核心')).not.toBeInTheDocument()
  })
})

describe('DebugEvaluationMetricsPage 分类过滤', () => {
  it('初始以无分类拉取；点击分类带参重拉；点击"全部"复位', async () => {
    mockGetEvaluationMetrics.mockResolvedValue({ metrics: [makeMetric()], total: 1 })
    renderPage()
    await screen.findByText('回答质量')
    expect(mockGetEvaluationMetrics).toHaveBeenCalledWith({ category: undefined, limit: 100 })

    fireEvent.click(screen.getByRole('button', { name: 'quality' }))
    await waitFor(() =>
      expect(mockGetEvaluationMetrics).toHaveBeenLastCalledWith({
        category: 'quality',
        limit: 100,
      }),
    )

    fireEvent.click(screen.getByRole('button', { name: '全部' }))
    await waitFor(() =>
      expect(mockGetEvaluationMetrics).toHaveBeenLastCalledWith({
        category: undefined,
        limit: 100,
      }),
    )
  })
})

describe('DebugEvaluationMetricsPage embedded 模式', () => {
  it('embedded：不渲染页头标题，内容照常渲染', async () => {
    mockGetEvaluationMetrics.mockResolvedValue({ metrics: [makeMetric()], total: 1 })
    renderPage({ embedded: true })
    await screen.findByText('回答质量')
    expect(screen.queryByRole('heading', { level: 1, name: '评估指标' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: '全部' })).toBeInTheDocument()
  })

  it('非 embedded：渲染页头标题', async () => {
    renderPage()
    await screen.findByRole('heading', { level: 1, name: '评估指标' })
  })
})
