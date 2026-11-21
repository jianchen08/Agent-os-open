// @feature FP-0.2.mobile_visual_p1 @ci: frontend-test
/**
 * 任务管理面板行操作收纳（P1-3 → 方案 B 行形态）与详情省略（P1-4）行为锁。
 *
 * 方案 B 适配：旧「移动视口收纳」前提退役——响应式改容器宽度驱动
 * （@container/entry，jsdom 无 CSS 排版不可单测），低频动作在任何宽度下都收
 * 进「⋮」菜单、行内常驻只「发消息」「⋮」两件（方案 B 8 条）。
 * - 「⋮」菜单打开后低频动作可达（复制/删除）；「--」token 空占位不渲染，
 *   真值在场照显。
 * 方案 B 适配登记：旧用例断言菜单含「打开对话/打开任务现场」——打开对话已改
 * 行内常驻「发消息」、任务现场入口移除（用户裁定 2026-11-20），按新菜单面
 * 改写（非删除：收纳可达契约保留）。
 * P1-4（shots/02 UUID/时间戳/错误墙）：
 * - ID 显示前 8 位（复制完整值走 clipboard + toast）；ISO 时间戳渲染本地
 *   「YYYY-MM-DD HH:mm」；错误长文默认 2 行折叠 + 展开/收起。
 */
import { fireEvent, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { pmMod, pmSeed, renderPmAllStatuses, resetPmContainers } from './pmTestUtils'
// eslint-disable-next-line import-x/order -- mock 装配时序约束（见 failureAttribution 同款）
import { PipelineManagerWidget } from '@/components/schema/widgets/PipelineManagerWidget'

vi.mock('@/services/api/tasks', () => pmMod.tasksApi())
vi.mock('@/services/api/client', () => pmMod.apiClient())
vi.mock('@/services/pipelineNavigator', () => pmMod.pipelineNavigator())
vi.mock('@/hooks/queries/usePipelineRunsQuery', () => pmMod.pipelineRunsQuery())
vi.mock('@/hooks/queries/useAllTasksQuery', () => pmMod.allTasksQuery())
vi.mock('@/hooks/queries/useLongTermTasksQuery', () => pmMod.longTermTasksQuery())
vi.mock('@/hooks/queries/useSessionsQuery', () => pmMod.sessionsQuery())
vi.mock('@/hooks/queries/useAgentsQuery', () => pmMod.agentsQuery())
vi.mock('@/stores/contextUsageStore', () => pmMod.contextUsageStore())
vi.mock('@/stores/sessionListStore', () => pmMod.sessionListStore())
vi.mock('@/services/api/modes', async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  useModesRegistry: () => pmSeed.modesRegistry,
}))

const RUN_ID = 'dbad2a38-eb2f-4b86-811d-bfc3533a9040'
const LONG_ERROR =
  '单次插件调用超时（上界 600s，初始档 600s）：invoke 等待被放弃（tokio drop 取消）。'.repeat(6)

/** 播种一条任务（uuid 运行 ID + token 真值 + 长错误全文） */
function seedTask(overrides: Record<string, unknown> = {}) {
  pmSeed.runs.r1 = {
    pipeline_id: 'pipe-1',
    run_id: RUN_ID,
    thread_id: 'th-1',
    title: '收纳测试任务',
    status: 'completed',
    started_at: '2026-11-13T18:09:05.771+00:00',
    ended_at: '2026-11-13T18:23:11.794+00:00',
    ...overrides,
  }
  pmSeed.states['pipe-1'] = {
    pipeline_id: 'pipe-1',
    thread_id: 'th-1',
    source: 'memory',
    state: {
      run_status: 'completed',
      'track.llm_usage': { totalTokens: 4321 },
      raw_error: LONG_ERROR,
    },
  }
  pmSeed.tasks.push({ id: 't-1', title: '收纳测试任务', status: 'completed', pipeline_run_id: 'pipe-1' })
}

async function openDetail() {
  renderPmAllStatuses(<PipelineManagerWidget />)
  const title = (await screen.findAllByText('收纳测试任务'))[0]
  fireEvent.click(title)
  await screen.findByTestId('pipeline-detail-panel')
}

beforeEach(() => {
  resetPmContainers()
})

afterEach(() => {
  vi.unstubAllGlobals()
  window.innerWidth = 1024
})

describe('任务行操作收纳（P1-3 → 方案 B 行形态）', () => {
  it('常驻只「发消息」「⋮」两件，平铺动作图标退场；「⋮」菜单打开后低频动作可达', async () => {
    seedTask()
    renderPmAllStatuses(<PipelineManagerWidget />)
    await screen.findAllByText('收纳测试任务')

    // 平铺图标退场（打开对话/复制管道 ID 两个 aria 名不再作为按钮在场）
    expect(screen.queryByRole('button', { name: '打开对话' })).toBeNull()
    expect(screen.queryByRole('button', { name: '复制管道 ID' })).toBeNull()
    // 常驻 = 发消息 + 「⋮」触发器，两件（方案 B 行形态）
    expect(screen.getByTestId('pipeline-entry-message-pipe-1')).toHaveAttribute(
      'aria-label',
      '发消息',
    )
    const more = screen.getByTestId('pipeline-entry-more-pipe-1')
    expect(more).toBeInTheDocument()

    // 打开菜单：收纳后的低频动作可达（radix 菜单触发器走 pointerdown）
    fireEvent.pointerDown(more)
    await screen.findByText('复制管道 ID')
    expect(screen.getByText('删除管道')).toBeInTheDocument()
    // 方案 B：打开对话改行内常驻「发消息」、任务现场入口移除（用户裁定
    // 2026-11-20）——两项不再出现在菜单
    expect(screen.queryByText('打开任务现场')).toBeNull()
    expect(screen.queryByText('打开对话')).toBeNull()

    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.assign(navigator, { clipboard: { writeText } })
    fireEvent.click(screen.getByText('复制管道 ID'))
    await waitFor(() => expect(writeText).toHaveBeenCalledWith('pipe-1'))
  })

  it('token「--」空占位不渲染；真值在场照显', async () => {
    seedTask()
    const first = renderPmAllStatuses(<PipelineManagerWidget />)
    await screen.findAllByText('收纳测试任务')
    expect(screen.queryByTestId('pipeline-entry-token')).toBeNull()
    expect(screen.queryByText('--')).toBeNull()
    first.unmount()

    resetPmContainers()
    seedTask({ total_tokens: { total: 4321 } }) // run 行带 token 真值（entryTokenTotal 源）
    renderPmAllStatuses(<PipelineManagerWidget />)
    await screen.findAllByText('收纳测试任务')
    expect(screen.getByTestId('pipeline-entry-token')).toHaveTextContent('4,321')
  })
})

describe('任务详情省略（P1-4）', () => {
  it('运行 ID 显示前 8 位；点击复制完整值（toast 反馈面）', async () => {
    seedTask()
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.assign(navigator, { clipboard: { writeText } })
    await openDetail()

    expect(screen.getByTestId('pipeline-detail-copy-运行 ID')).toHaveTextContent('dbad2a38…')
    expect(screen.queryByText(RUN_ID)).toBeNull() // 完整值不进显示面
    fireEvent.click(screen.getByTestId('pipeline-detail-copy-运行 ID'))
    await waitFor(() => expect(writeText).toHaveBeenCalledWith(RUN_ID))
  })

  it('时间行渲染本地短格式（YYYY-MM-DD HH:mm），原始 ISO 不再满宽换行', async () => {
    seedTask()
    await openDetail()
    const localPattern = /\d{4}-\d{2}-\d{2} \d{2}:\d{2}/
    const startRow = screen.getByText('开始').closest('div')!
    expect(startRow.textContent).toContain('开始')
    expect(startRow.textContent).toMatch(localPattern)
    const endRow = screen.getByText('结束').closest('div')!
    expect(endRow.textContent).toMatch(localPattern)
    expect(screen.queryByText('2026-11-13T18:09:05.771+00:00')).toBeNull()
  })

  it('错误长文默认折叠（line-clamp-2），展开显全文、收起回折叠', async () => {
    seedTask()
    await openDetail()
    const text = screen.getByTestId('pipeline-detail-error-text')
    expect(text).toHaveClass('line-clamp-2')
    expect(screen.getByTestId('pipeline-detail-error-expand')).toBeInTheDocument()

    fireEvent.click(screen.getByTestId('pipeline-detail-error-expand'))
    expect(screen.getByTestId('pipeline-detail-error-text')).not.toHaveClass('line-clamp-2')
    expect(screen.getByTestId('pipeline-detail-error-text')).toHaveTextContent('单次插件调用超时')
    expect(screen.getByTestId('pipeline-detail-error-collapse')).toBeInTheDocument()

    fireEvent.click(screen.getByTestId('pipeline-detail-error-collapse'))
    expect(screen.getByTestId('pipeline-detail-error-text')).toHaveClass('line-clamp-2')
  })
})
