/** @feature FP-0.2.grants_ledger P3 运营面 | @ci: frontend-test */
/**
 * GrantsLedgerPage — 授权账本管理页（P3）。
 *
 * 覆盖：挂载渲染（四指标卡/grant 表/审计流/proto-grant 区）、筛选交互（工具名
 * 筛选 → 查询请求带参）、撤销确认弹层 → DELETE 端点调用、指标降级如实呈现
 * （value=null → 「—」+ 降级标注）、proto-grant 移除/撤销端点调用。
 * apiClient 全 mock（外部依赖边界），端点字面量走生成层真值（不 mock 端点常量）。
 */
import { fireEvent, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import apiClient from '@/services/api/client'
import { GRANTS_ENDPOINTS, PIPELINE_SECURITY_CHECK_ENDPOINTS } from '@/services/api/endpoints.generated'
import { useGrantsLedgerStore } from '@/stores/grantsLedgerStore'
import { renderWithProviders } from '@/test/renderWithProviders'
import { GrantsLedgerPage } from '../GrantsLedgerPage'

vi.mock('@/services/api/client', () => ({
  default: {
    get: vi.fn(),
    post: vi.fn(),
    delete: vi.fn(),
  },
}))

const METRICS_RESPONSE = {
  metrics: {
    approval_card_popup_rate: {
      label: '审批卡弹出率',
      caliber: '弹卡次数 / 分级调用总数',
      unit: 'ratio',
      value: null,
      degraded: true,
      reason: '弹卡事件在既有数据面无记录',
      components: { approval_card_grant_issues: 2, auto_allow_events: 5 },
    },
    grant_hit_rate: {
      label: 'grant 命中率（省弹次数）',
      caliber: 'authorize 命中数 /（命中数 + 弹卡数）',
      unit: 'ratio',
      value: null,
      degraded: true,
      reason: '弹卡数不可得，率降级',
      components: { saved_popups: 7, approval_card_issues: 2 },
    },
    revoke_to_effect: {
      label: '撤销→生效耗时',
      caliber: 'revoke 返回 → 下一次同类调用被拒的墙钟差',
      unit: 'ms',
      value: null,
      degraded: true,
      reason: '被拒事件不可配对',
      components: { revokes: 1, post_revoke_consumes: 0 },
    },
    uncategorized_blocked: {
      label: '未分类拦截数',
      caliber: 'classify 未分类 deny 事件数',
      unit: 'count',
      value: 3,
      degraded: false,
      reason: null,
      components: { unclassified: 3, invalid_entry: 1, other: 0 },
    },
  },
  sources: { security_audit_jsonl: { reachable: true, reason: null } },
}

function makeGrant(overrides: Record<string, unknown>) {
  return {
    grant_id: 'g-1',
    subject_kind: 'pipeline',
    subject_id: 'pipe-1',
    tool: 'memory',
    action: 'store',
    scope: {},
    granularity: 'task',
    status: 'active',
    max_uses: null,
    uses: 3,
    created_at: '2026-11-19T10:00:00+00:00',
    expires_at: null,
    source: 'approval_card',
    evidence: 'card-1',
    pipeline_id: 'pipe-1',
    parent_task_id: null,
    ...overrides,
  }
}

const GRANTS_RESPONSE = {
  grants: [
    makeGrant({}),
    makeGrant({ grant_id: 'g-2', status: 'revoked', subject_id: 'pipe-2', tool: 'bash', action: 'execute' }),
  ],
}

const AUDIT_RESPONSE = {
  events: [
    { event_id: 'e1', grant_id: 'g-1', event_type: 'issue', ts: '2026-11-19T10:00:00+00:00', context: { source: 'approval_card' } },
    { event_id: 'e2', grant_id: 'g-1', event_type: 'consume', ts: '2026-11-19T10:01:00+00:00', context: null },
  ],
}

const WHITELIST_RESPONSE = { entries: ['git status#sig1'], count: 1 }
const PIPELINE_MODES_RESPONSE = { entries: { 'pipe-1': 'bypass' }, count: 1 }

function routeGet(url: string): Promise<{ data: unknown }> {
  if (url === GRANTS_ENDPOINTS.grants_list) return Promise.resolve({ data: GRANTS_RESPONSE })
  if (url === GRANTS_ENDPOINTS.grants_metrics) return Promise.resolve({ data: METRICS_RESPONSE })
  if (url === GRANTS_ENDPOINTS.grants_audit) return Promise.resolve({ data: AUDIT_RESPONSE })
  if (url === PIPELINE_SECURITY_CHECK_ENDPOINTS.whitelist_get) return Promise.resolve({ data: WHITELIST_RESPONSE })
  if (url === PIPELINE_SECURITY_CHECK_ENDPOINTS.pipeline_permission_mode_get)
    return Promise.resolve({ data: PIPELINE_MODES_RESPONSE })
  return Promise.resolve({ data: {} })
}

beforeEach(() => {
  vi.mocked(apiClient.get).mockClear()
  vi.mocked(apiClient.post).mockClear()
  vi.mocked(apiClient.delete).mockClear()
  vi.mocked(apiClient.get).mockImplementation(async (url: string) => routeGet(url))
  vi.mocked(apiClient.post).mockResolvedValue({ data: { removed: true } })
  vi.mocked(apiClient.delete).mockResolvedValue({ data: { revoked: true } })
  useGrantsLedgerStore.setState({
    grants: [],
    filters: { subject_kind: '', subject_id: '', tool: '', status: '' },
    grantsLoading: false,
    grantsError: null,
    metrics: null,
    metricsError: null,
    auditEvents: [],
    auditTypeFilter: '',
    auditLoading: false,
    whitelist: [],
    pipelineModes: {},
    protoError: null,
    revokingId: null,
  })
})

describe('GrantsLedgerPage', () => {
  it('挂载渲染：四指标卡 + grant 表 + 审计流 + proto-grant 区全在位', async () => {
    renderWithProviders(<GrantsLedgerPage embedded />)
    for (const key of ['approval_card_popup_rate', 'grant_hit_rate', 'revoke_to_effect', 'uncategorized_blocked']) {
      await waitFor(() => {
        expect(screen.getByTestId(`metric-card-${key}`)).toBeInTheDocument()
      })
    }
    await waitFor(() => {
      expect(screen.getByTestId('grants-table')).toHaveTextContent('memory.store')
      expect(screen.getByTestId('grants-table')).toHaveTextContent('bash.execute')
    })
    expect(screen.getByTestId('audit-list')).toHaveTextContent('issue')
    expect(screen.getByTestId('whitelist-section')).toHaveTextContent('git status#sig1')
    expect(screen.getByTestId('pipeline-modes-section')).toHaveTextContent('pipe-1')
    // 挂载即拉取全部数据面
    expect(vi.mocked(apiClient.get)).toHaveBeenCalledWith(
      GRANTS_ENDPOINTS.grants_list,
      expect.objectContaining({ params: {} }),
    )
  })

  it('指标降级如实呈现：value=null → 「—」+ 降级标注；可得指标直出数值', async () => {
    renderWithProviders(<GrantsLedgerPage embedded />)
    await waitFor(() => {
      expect(screen.getByTestId('metric-value-approval_card_popup_rate')).toHaveTextContent('—')
    })
    expect(screen.getByTestId('metric-degraded-approval_card_popup_rate')).toBeInTheDocument()
    expect(screen.getByTestId('metric-degraded-grant_hit_rate')).toBeInTheDocument()
    // 省弹次数组件值可见（grant 命中率的可得绝对量）
    expect(screen.getByTestId('metric-card-grant_hit_rate')).toHaveTextContent('saved_popups=7')
    // 完全可得指标：数值直出、无降级标注
    expect(screen.getByTestId('metric-value-uncategorized_blocked')).toHaveTextContent('3')
    expect(screen.queryByTestId('metric-degraded-uncategorized_blocked')).not.toBeInTheDocument()
  })

  it('筛选交互：输入工具名 → 查询请求携带 tool 参数', async () => {
    renderWithProviders(<GrantsLedgerPage embedded />)
    await waitFor(() => {
      expect(screen.getByTestId('filter-tool')).toBeInTheDocument()
    })
    vi.mocked(apiClient.get).mockClear()
    fireEvent.change(screen.getByTestId('filter-tool'), { target: { value: 'memory' } })
    fireEvent.click(screen.getByTestId('grants-refresh'))
    await waitFor(() => {
      expect(vi.mocked(apiClient.get)).toHaveBeenCalledWith(
        GRANTS_ENDPOINTS.grants_list,
        expect.objectContaining({ params: { tool: 'memory' } }),
      )
    })
  })

  it('撤销：active 行点撤销 → 确认弹层 → 确认后 DELETE 端点按 grant_id 调用并刷新', async () => {
    renderWithProviders(<GrantsLedgerPage embedded />)
    await waitFor(() => {
      expect(screen.getByTestId(`revoke-btn-g-1`)).toBeInTheDocument()
    })
    // revoked 行无撤销按钮（只读管理：非 active 不可撤）
    expect(screen.queryByTestId('revoke-btn-g-2')).not.toBeInTheDocument()

    fireEvent.click(screen.getByTestId('revoke-btn-g-1'))
    const confirmBtn = await screen.findByRole('button', { name: '确认撤销' })
    fireEvent.click(confirmBtn)
    await waitFor(() => {
      expect(vi.mocked(apiClient.delete)).toHaveBeenCalledWith(
        GRANTS_ENDPOINTS.grants_revoke.replace('{grant_id}', 'g-1'),
      )
    })
    // 撤销后刷新列表/审计/指标
    await waitFor(() => {
      expect(vi.mocked(apiClient.get)).toHaveBeenCalledWith(
        GRANTS_ENDPOINTS.grants_list,
        expect.objectContaining({ params: {} }),
      )
    })
    // 弹层关闭
    await waitFor(() => {
      expect(screen.queryByRole('button', { name: '确认撤销' })).not.toBeInTheDocument()
    })
  })

  it('proto-grant 同面管理：白名单移除 → POST remove；管道档撤销 → DELETE 按管道 id', async () => {
    renderWithProviders(<GrantsLedgerPage embedded />)
    await waitFor(() => {
      expect(screen.getByTestId('whitelist-remove-git status#sig1')).toBeInTheDocument()
    })
    fireEvent.click(screen.getByTestId('whitelist-remove-git status#sig1'))
    await waitFor(() => {
      expect(vi.mocked(apiClient.post)).toHaveBeenCalledWith(PIPELINE_SECURITY_CHECK_ENDPOINTS.whitelist_remove, {
        signature: 'git status#sig1',
      })
    })
    fireEvent.click(screen.getByTestId('pipeline-mode-revoke-pipe-1'))
    await waitFor(() => {
      expect(vi.mocked(apiClient.delete)).toHaveBeenCalledWith(
        PIPELINE_SECURITY_CHECK_ENDPOINTS.pipeline_permission_mode_revoke,
        expect.objectContaining({ data: { pipeline_id: 'pipe-1' } }),
      )
    })
  })

  it('指标 ratio 有值时按百分比格式化（整数直出 / 小数留一位），非 ratio 单位直出', async () => {
    const withValues = JSON.parse(JSON.stringify(METRICS_RESPONSE)) as typeof METRICS_RESPONSE
    withValues.metrics.approval_card_popup_rate.value = 0.5
    withValues.metrics.approval_card_popup_rate.degraded = false
    withValues.metrics.grant_hit_rate.value = 0.4567
    withValues.metrics.grant_hit_rate.degraded = false
    withValues.metrics.revoke_to_effect.value = 1200
    withValues.metrics.revoke_to_effect.degraded = false
    vi.mocked(apiClient.get).mockImplementation(async (url: string) => {
      if (url === GRANTS_ENDPOINTS.grants_metrics) return { data: withValues }
      return routeGet(url)
    })
    renderWithProviders(<GrantsLedgerPage embedded />)
    await waitFor(() => {
      expect(screen.getByTestId('metric-value-approval_card_popup_rate')).toHaveTextContent('50%')
    })
    expect(screen.getByTestId('metric-value-grant_hit_rate')).toHaveTextContent('45.7%')
    expect(screen.getByTestId('metric-value-revoke_to_effect')).toHaveTextContent('1200')
  })

  it('筛选面：主体 id 输入参与查询参数（kind/id 补全面）', async () => {
    renderWithProviders(<GrantsLedgerPage embedded />)
    await waitFor(() => {
      expect(screen.getByTestId('filter-subject-id')).toBeInTheDocument()
    })
    vi.mocked(apiClient.get).mockClear()
    fireEvent.change(screen.getByTestId('filter-subject-id'), { target: { value: 'pipe-9' } })
    fireEvent.click(screen.getByTestId('grants-refresh'))
    await waitFor(() => {
      expect(vi.mocked(apiClient.get)).toHaveBeenCalledWith(
        GRANTS_ENDPOINTS.grants_list,
        expect.objectContaining({ params: { subject_id: 'pipe-9' } }),
      )
    })
  })

  it('审计类型切换：选 issue → loadAudit 携带 event_type 参数', async () => {
    renderWithProviders(<GrantsLedgerPage embedded />)
    await waitFor(() => {
      expect(screen.getByTestId('audit-type-filter')).toBeInTheDocument()
    })
    vi.mocked(apiClient.get).mockClear()
    fireEvent.mouseDown(screen.getByTestId('audit-type-filter'))
    const option = (await screen.findAllByText('issue')).find((el) =>
      el.closest('.ant-select-item-option'),
    )
    expect(option).toBeDefined()
    fireEvent.click(option as HTMLElement)
    await waitFor(() => {
      expect(vi.mocked(apiClient.get)).toHaveBeenCalledWith(
        GRANTS_ENDPOINTS.grants_audit,
        expect.objectContaining({ params: { event_type: 'issue' } }),
      )
    })
  })
})

describe('GrantsLedgerPage 补充面', () => {
  it('非嵌入形态：正文包最大宽容器（embedded=false 分支）', async () => {
    const { container } = renderWithProviders(<GrantsLedgerPage />)
    await waitFor(() => {
      expect(screen.getByTestId('grants-ledger-page')).toBeInTheDocument()
    })
    expect(container.querySelector('.mx-auto.max-w-6xl')).not.toBeNull()
  })

  it('筛选面：主体类别选择与状态输入参与查询参数', async () => {
    renderWithProviders(<GrantsLedgerPage embedded />)
    await waitFor(() => {
      expect(screen.getByTestId('filter-subject-id')).toBeInTheDocument()
    })
    vi.mocked(apiClient.get).mockClear()
    fireEvent.mouseDown(screen.getByTestId('filter-subject-kind'))
    const option = (await screen.findAllByText('user')).find((el) =>
      el.closest('.ant-select-item-option'),
    )
    expect(option).toBeDefined()
    fireEvent.click(option as HTMLElement)
    fireEvent.mouseDown(screen.getByTestId('filter-status'))
    const statusOption = (await screen.findAllByText('active')).find((el) =>
      el.closest('.ant-select-item-option'),
    )
    expect(statusOption).toBeDefined()
    fireEvent.click(statusOption as HTMLElement)
    fireEvent.click(screen.getByTestId('grants-refresh'))
    await waitFor(() => {
      expect(vi.mocked(apiClient.get)).toHaveBeenCalledWith(
        GRANTS_ENDPOINTS.grants_list,
        expect.objectContaining({ params: { subject_kind: 'user', status: 'active' } }),
      )
    })
  })
})
