/** @feature FP-0.2.grants_ledger P3 运营面 | @ci: frontend-test */
/**
 * grantsLedgerStore — 数据装载与撤销动作的分支补测：
 * 各 load* 的成功/失败面（错误如实落 state，不吞）、审计类型筛选参与查询、
 * 撤销/移除/管道档撤销的业务错误（resp.error → 抛错返回 false）与刷新链。
 * apiClient 全 mock（外部依赖边界）；端点字面量走生成层真值。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import apiClient from '@/services/api/client'
import { GRANTS_ENDPOINTS, PIPELINE_SECURITY_CHECK_ENDPOINTS } from '@/services/api/endpoints.generated'
import { useGrantsLedgerStore } from '../grantsLedgerStore'

vi.mock('@/services/api/client', () => ({
  default: {
    get: vi.fn(),
    post: vi.fn(),
    delete: vi.fn(),
  },
}))

function resetStore(): void {
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
}

beforeEach(() => {
  vi.mocked(apiClient.get).mockReset()
  vi.mocked(apiClient.post).mockReset()
  vi.mocked(apiClient.delete).mockReset()
  resetStore()
})

describe('grantsLedgerStore · load 面', () => {
  it('loadGrants 成功落 grants；失败落 grantsError 不吞异常', async () => {
    vi.mocked(apiClient.get).mockResolvedValue({ data: { grants: [{ grant_id: 'g1' }] } })
    await useGrantsLedgerStore.getState().loadGrants()
    expect(useGrantsLedgerStore.getState().grants).toEqual([{ grant_id: 'g1' }])
    expect(useGrantsLedgerStore.getState().grantsError).toBeNull()

    vi.mocked(apiClient.get).mockRejectedValue(new Error('boom'))
    await useGrantsLedgerStore.getState().loadGrants()
    expect(useGrantsLedgerStore.getState().grantsError).toBe('boom')
    expect(useGrantsLedgerStore.getState().grantsLoading).toBe(false)
  })

  it('loadMetrics 成功落 metrics；失败落 metricsError', async () => {
    vi.mocked(apiClient.get).mockResolvedValue({ data: { metrics: {} } })
    await useGrantsLedgerStore.getState().loadMetrics()
    expect(useGrantsLedgerStore.getState().metrics).toEqual({ metrics: {} })

    vi.mocked(apiClient.get).mockRejectedValue(new Error('metrics down'))
    await useGrantsLedgerStore.getState().loadMetrics()
    expect(useGrantsLedgerStore.getState().metricsError).toBe('metrics down')
  })

  it('loadAudit 按审计类型筛选带 event_type 参数；失败静默降级为空事件列表（不阻断页面）', async () => {
    useGrantsLedgerStore.getState().setAuditTypeFilter('issue')
    vi.mocked(apiClient.get).mockResolvedValue({ data: { events: [{ event_id: 'e1' }] } })
    await useGrantsLedgerStore.getState().loadAudit()
    expect(apiClient.get).toHaveBeenCalledWith(GRANTS_ENDPOINTS.grants_audit, {
      params: { event_type: 'issue' },
    })
    expect(useGrantsLedgerStore.getState().auditEvents).toEqual([{ event_id: 'e1' }])

    vi.mocked(apiClient.get).mockRejectedValue(new Error('audit unreadable'))
    await useGrantsLedgerStore.getState().loadAudit()
    expect(useGrantsLedgerStore.getState().auditEvents).toEqual([])
    expect(useGrantsLedgerStore.getState().auditLoading).toBe(false)
  })

  it('loadProtoGrants 成功落白名单与管道档；失败落 protoError', async () => {
    vi.mocked(apiClient.get).mockImplementation(async (url: string) => {
      if (url === PIPELINE_SECURITY_CHECK_ENDPOINTS.whitelist_get)
        return { data: { entries: ['git status#sig'] } }
      return { data: { entries: { 'pipe-1': 'bypass' } } }
    })
    await useGrantsLedgerStore.getState().loadProtoGrants()
    const s = useGrantsLedgerStore.getState()
    expect(s.whitelist).toEqual(['git status#sig'])
    expect(s.pipelineModes).toEqual({ 'pipe-1': 'bypass' })

    vi.mocked(apiClient.get).mockRejectedValue(new Error('proto down'))
    await useGrantsLedgerStore.getState().loadProtoGrants()
    expect(useGrantsLedgerStore.getState().protoError).toBe('proto down')
  })
})

describe('grantsLedgerStore · 撤销与移除动作', () => {
  it('revokeGrant 成功返回 true 并刷新三面；业务错误（resp.error）返回 false 落 grantsError', async () => {
    vi.mocked(apiClient.delete).mockResolvedValue({ data: { revoked: true } })
    vi.mocked(apiClient.get).mockResolvedValue({ data: {} })
    await expect(useGrantsLedgerStore.getState().revokeGrant('g-9')).resolves.toBe(true)
    expect(apiClient.delete).toHaveBeenCalledWith(
      expect.stringContaining(encodeURIComponent('g-9')),
    )

    vi.mocked(apiClient.delete).mockResolvedValue({ data: { error: '已被使用完' } })
    await expect(useGrantsLedgerStore.getState().revokeGrant('g-9')).resolves.toBe(false)
    expect(useGrantsLedgerStore.getState().grantsError).toBe('已被使用完')
  })

  it('removeWhitelistEntry 成功刷新 proto 面；失败落 protoError 返回 false', async () => {
    vi.mocked(apiClient.post).mockResolvedValue({ data: { removed: true } })
    vi.mocked(apiClient.get).mockResolvedValue({ data: { entries: [] } })
    await expect(
      useGrantsLedgerStore.getState().removeWhitelistEntry('git status#sig'),
    ).resolves.toBe(true)

    vi.mocked(apiClient.post).mockRejectedValue(new Error('remove rejected'))
    await expect(
      useGrantsLedgerStore.getState().removeWhitelistEntry('git status#sig'),
    ).resolves.toBe(false)
    expect(useGrantsLedgerStore.getState().protoError).toBe('remove rejected')
  })

  it('revokePipelineMode 成功返回 true；失败返回 false 落 protoError', async () => {
    vi.mocked(apiClient.delete).mockResolvedValue({ data: { revoked: true } })
    vi.mocked(apiClient.get).mockResolvedValue({ data: { entries: {} } })
    await expect(
      useGrantsLedgerStore.getState().revokePipelineMode('pipe-1'),
    ).resolves.toBe(true)

    vi.mocked(apiClient.delete).mockRejectedValue(new Error('mode revoke down'))
    await expect(
      useGrantsLedgerStore.getState().revokePipelineMode('pipe-1'),
    ).resolves.toBe(false)
    expect(useGrantsLedgerStore.getState().protoError).toBe('mode revoke down')
  })
})

describe('grantsLedgerStore · 筛选面', () => {
  it('setFilter 增量合并；resetFilters 清空回空筛选', () => {
    useGrantsLedgerStore.getState().setFilter({ tool: 'bash', status: 'active' })
    expect(useGrantsLedgerStore.getState().filters).toEqual({
      subject_kind: '',
      subject_id: '',
      tool: 'bash',
      status: 'active',
    })
    useGrantsLedgerStore.getState().resetFilters()
    expect(useGrantsLedgerStore.getState().filters).toEqual({
      subject_kind: '',
      subject_id: '',
      tool: '',
      status: '',
    })
  })
})
