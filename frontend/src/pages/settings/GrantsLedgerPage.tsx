/**
 * GrantsLedgerPage — 授权账本管理页（P3 运营面，grants 插件 contributes.pages
 * space=settings 声明页承载）
 *
 * 五功能区（任务书件 3）：grant 列表 + 筛选（subject/tool/status）+ 撤销（确认
 * 弹层）+ 审计流 + 四指标卡；proto-grant（永久白名单/管道级免审批，ADR 2026-11-12
 * 未做项「设置页白名单管理面板」）同面收口呈现。
 *
 * 红线：只读管理 + 撤销——不做白名单编辑/新增 grant 的 UI（授予面仍是审批卡）。
 * API 全走生成端点层（endpoints.generated.ts），零手写 /ext/ 字面量。
 */

import { Button, Card, Input, List, Modal, Select, Table, Tag, Tooltip, Typography } from 'antd'
import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  GRANT_EVENT_TYPES,
  GRANT_METRIC_KEYS,
  GRANT_STATUSES,
  GRANT_SUBJECT_KINDS,
  auditDisplayEvents,
  useGrantsLedgerStore,
} from '@/stores/grantsLedgerStore'
import type { GrantMetricKey, GrantRecord, MetricEntry } from '@/stores/grantsLedgerStore'
import type { ColumnsType } from 'antd/es/table'

const { Text } = Typography

/** grant 状态 → Tag 颜色（closed-set 映射，表外态灰色） */
const STATUS_COLOR: Record<string, string> = {
  active: 'green',
  consumed: 'blue',
  revoked: 'red',
  expired: 'default',
}

/** 审计事件类型 → Tag 颜色 */
const EVENT_COLOR: Record<string, string> = {
  issue: 'green',
  consume: 'blue',
  deny: 'red',
  revoke: 'orange',
  expire: 'default',
}

/** ratio 指标格式化：0–1 → 百分比；其余按 unit 直出 */
function formatMetricValue(entry: MetricEntry | undefined): string {
  if (!entry || entry.value === null) return '—'
  if (entry.unit === 'ratio') {
    const pct = entry.value * 100
    return `${Number.isInteger(pct) ? pct : pct.toFixed(1)}%`
  }
  return String(entry.value)
}

/** 指标卡组件值行：降级显示「—」+ 降级说明 Tooltip（禁造数，如实呈现） */
function MetricCard({ metricKey, entry }: { metricKey: GrantMetricKey; entry: MetricEntry | undefined }) {
  const degraded = !entry || entry.degraded || entry.value === null
  const componentLine = useMemo(() => {
    if (!entry) return ''
    return Object.entries(entry.components)
      .map(([k, v]) => `${k}=${v === null ? 'null' : v}`)
      .join(' · ')
  }, [entry])
  return (
    <Card size="small" title={entry?.label ?? metricKey} data-testid={`metric-card-${metricKey}`}>
      <div className="flex items-baseline gap-2">
        <span className="text-2xl font-semibold" data-testid={`metric-value-${metricKey}`}>
          {formatMetricValue(entry ?? { unit: '', value: null } as MetricEntry)}
        </span>
        {degraded && (
          <Tooltip title={entry?.reason ?? '指标加载失败'}>
            <Tag data-testid={`metric-degraded-${metricKey}`} color="warning">
              降级
            </Tag>
          </Tooltip>
        )}
      </div>
      <div className="text-muted-foreground mt-1 line-clamp-2 text-[11px]" title={entry?.caliber}>
        {entry?.caliber ?? ''}
      </div>
      {componentLine && <div className="text-muted-foreground mt-0.5 font-mono text-[10px]">{componentLine}</div>}
    </Card>
  )
}

/** grant 列表列定义（撤销操作列在组件内注入） */
function buildColumns(onRevoke: (record: GrantRecord) => void, revokingId: string | null): ColumnsType<GrantRecord> {
  return [
    {
      title: '主体',
      key: 'subject',
      render: (_, r) => (
        <Text code className="text-[11px]">
          {r.subject_kind}/{r.subject_id}
        </Text>
      ),
    },
    { title: '工具.动作', key: 'tool_action', render: (_, r) => `${r.tool}.${r.action}` },
    { title: '粒度', dataIndex: 'granularity', key: 'granularity', width: 80 },
    {
      title: '状态',
      dataIndex: 'status',
      key: 'status',
      width: 90,
      render: (_, r) => <Tag color={STATUS_COLOR[r.status] ?? 'default'}>{r.status}</Tag>,
    },
    {
      title: '使用',
      key: 'uses',
      width: 70,
      render: (_, r) => `${r.uses}${r.max_uses !== null ? `/${r.max_uses}` : ''}`,
    },
    { title: '来源', dataIndex: 'source', key: 'source', width: 110 },
    { title: '授予时间', dataIndex: 'created_at', key: 'created_at', width: 170, render: (v: string | null) => v ?? '—' },
    {
      title: '操作',
      key: 'action',
      width: 90,
      render: (_, r) =>
        r.status === 'active' ? (
          <Button
            size="small"
            danger
            data-testid={`revoke-btn-${r.grant_id}`}
            disabled={revokingId === r.grant_id}
            onClick={() => onRevoke(r)}
          >
            撤销
          </Button>
        ) : null,
    },
  ]
}

/** 授权账本管理页（设置中枢「插件页面」组声明页；embedded 供面板内嵌复用） */
export function GrantsLedgerPage({ embedded = false }: { embedded?: boolean }) {
  const {
    grants,
    filters,
    grantsLoading,
    grantsError,
    metrics,
    metricsError,
    auditEvents,
    auditTypeFilter,
    whitelist,
    pipelineModes,
    protoError,
    revokingId,
    setFilter,
    setAuditTypeFilter,
    loadGrants,
    loadAudit,
    loadAll,
    revokeGrant,
    removeWhitelistEntry,
    revokePipelineMode,
  } = useGrantsLedgerStore()

  // 撤销确认弹层目标（声明式 Modal，不用 antd 静态.confirm——无 App 上下文依赖）
  const [revokeTarget, setRevokeTarget] = useState<GrantRecord | null>(null)

  useEffect(() => {
    void loadAll()
    // 挂载即全量拉取（store action 引用稳定，仅挂载触发一次）
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const handleConfirmRevoke = useCallback(async () => {
    if (!revokeTarget) return
    await revokeGrant(revokeTarget.grant_id)
    setRevokeTarget(null)
  }, [revokeTarget, revokeGrant])

  const handleAuditTypeChange = useCallback(
    (value: string) => {
      setAuditTypeFilter(value)
      void loadAudit()
    },
    [setAuditTypeFilter, loadAudit],
  )

  const auditList = useMemo(() => auditDisplayEvents(auditEvents, auditTypeFilter), [auditEvents, auditTypeFilter])
  const columns = useMemo(() => buildColumns(setRevokeTarget, revokingId), [revokingId])

  const body = (
    <div className="flex flex-col gap-4 p-4" data-testid="grants-ledger-page">
      <div>
        <h2 className="text-foreground text-base font-semibold">授权账本</h2>
        <p className="text-muted-foreground mt-1 text-[12px]">
          工具调用授权的统一账本：审批卡授予的 grant 可撤销（撤销后下一次同类调用重弹审批卡）；
          永久白名单与管道级免审批为既有授予面的投影，同面管理（只读 + 撤销，授予仍是审批卡）。
        </p>
      </div>

      {/* ── 四指标卡 ── */}
      <div className="grid grid-cols-2 gap-3 xl:grid-cols-4">
        {GRANT_METRIC_KEYS.map((key) => (
          <MetricCard key={key} metricKey={key} entry={metrics?.metrics?.[key]} />
        ))}
      </div>
      {metricsError && (
        <div className="text-destructive text-[12px]" data-testid="metrics-error">
          指标加载失败: {metricsError}
        </div>
      )}

      {/* ── grant 列表 + 筛选 ── */}
      <Card
        size="small"
        title="Grant 列表"
        extra={
          <Button size="small" data-testid="grants-refresh" onClick={() => void loadGrants()}>
            查询 / 刷新
          </Button>
        }
      >
        <div className="mb-3 flex flex-wrap items-center gap-2">
          <Select
            allowClear
            placeholder="主体类型"
            className="w-[130px]"
            data-testid="filter-subject-kind"
            value={filters.subject_kind || undefined}
            onChange={(v) => setFilter({ subject_kind: v ?? '' })}
            options={GRANT_SUBJECT_KINDS.map((s) => ({ value: s, label: s }))}
          />
          <Input
            allowClear
            placeholder="主体 id"
            className="w-[150px]"
            data-testid="filter-subject-id"
            value={filters.subject_id}
            onChange={(e) => setFilter({ subject_id: e.target.value })}
          />
          <Input
            allowClear
            placeholder="工具名"
            className="w-[150px]"
            data-testid="filter-tool"
            value={filters.tool}
            onChange={(e) => setFilter({ tool: e.target.value })}
          />
          <Select
            allowClear
            placeholder="状态"
            className="w-[130px]"
            data-testid="filter-status"
            value={filters.status || undefined}
            onChange={(v) => setFilter({ status: v ?? '' })}
            options={GRANT_STATUSES.map((s) => ({ value: s, label: s }))}
          />
        </div>
        {grantsError && (
          <div className="text-destructive mb-2 text-[12px]" data-testid="grants-error">
            列表加载失败: {grantsError}
          </div>
        )}
        <Table
          size="small"
          rowKey="grant_id"
          columns={columns}
          dataSource={grants}
          loading={grantsLoading}
          pagination={{ pageSize: 10, showSizeChanger: false }}
          data-testid="grants-table"
        />
      </Card>

      {/* ── 审计流 ── */}
      <Card
        size="small"
        title="审计流"
        extra={
          <Select
            allowClear
            placeholder="全部事件"
            className="w-[140px]"
            data-testid="audit-type-filter"
            value={auditTypeFilter || undefined}
            onChange={handleAuditTypeChange}
            options={GRANT_EVENT_TYPES.map((t) => ({ value: t, label: t }))}
          />
        }
      >
        <List
          size="small"
          dataSource={auditList}
          locale={{ emptyText: '（暂无审计事件）' }}
          data-testid="audit-list"
          renderItem={(e) => (
            <List.Item>
              <div className="flex w-full items-center gap-2 overflow-hidden">
                <Tag color={EVENT_COLOR[e.event_type] ?? 'default'}>{e.event_type}</Tag>
                <Text type="secondary" className="text-[11px]">
                  {e.ts}
                </Text>
                <Text code className="text-[11px]" ellipsis>
                  {e.grant_id ?? '—'}
                </Text>
              </div>
            </List.Item>
          )}
        />
      </Card>

      {/* ── proto-grant 同面收口（ADR 2026-11-12 白名单管理面板未做项） ── */}
      <Card size="small" title="既有授予面（proto-grant 投影）">
        {protoError && (
          <div className="text-destructive mb-2 text-[12px]" data-testid="proto-error">
            既有授予面加载失败: {protoError}
          </div>
        )}
        <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
          <div data-testid="whitelist-section">
            <div className="text-foreground mb-1 text-[13px] font-semibold">永久命令白名单（standing）</div>
            <List
              size="small"
              dataSource={whitelist}
              locale={{ emptyText: '（空）' }}
              renderItem={(sig) => (
                <List.Item>
                  <div className="flex w-full items-center gap-2 overflow-hidden">
                    <Text code className="text-[11px]" ellipsis>
                      {sig}
                    </Text>
                    <Button
                      size="small"
                      danger
                      className="ml-auto shrink-0"
                      data-testid={`whitelist-remove-${sig}`}
                      onClick={() => void removeWhitelistEntry(sig)}
                    >
                      移除
                    </Button>
                  </div>
                </List.Item>
              )}
            />
          </div>
          <div data-testid="pipeline-modes-section">
            <div className="text-foreground mb-1 text-[13px] font-semibold">管道级免审批（task）</div>
            <List
              size="small"
              dataSource={Object.entries(pipelineModes)}
              locale={{ emptyText: '（空）' }}
              renderItem={([pipelineId, mode]) => (
                <List.Item>
                  <div className="flex w-full items-center gap-2 overflow-hidden">
                    <Text code className="text-[11px]" ellipsis>
                      {pipelineId}
                    </Text>
                    <Tag>{mode}</Tag>
                    <Button
                      size="small"
                      danger
                      className="ml-auto shrink-0"
                      data-testid={`pipeline-mode-revoke-${pipelineId}`}
                      onClick={() => void revokePipelineMode(pipelineId)}
                    >
                      撤销
                    </Button>
                  </div>
                </List.Item>
              )}
            />
          </div>
        </div>
      </Card>

      {/* ── 撤销确认弹层 ── */}
      <Modal
        open={revokeTarget !== null}
        title="撤销授权"
        okText="确认撤销"
        okButtonProps={{ danger: true }}
        cancelText="取消"
        confirmLoading={revokingId !== null}
        onOk={() => void handleConfirmRevoke()}
        onCancel={() => setRevokeTarget(null)}
        data-testid="revoke-confirm-modal"
      >
        <p>
          撤销后该 grant 即时失效，下一次同类工具调用将重新弹审批卡。确认撤销{' '}
          <Text code>
            {revokeTarget?.subject_kind}/{revokeTarget?.subject_id}
          </Text>{' '}
          对 <Text code>{revokeTarget ? `${revokeTarget.tool}.${revokeTarget.action}` : ''}</Text> 的授权？
        </p>
      </Modal>
    </div>
  )

  if (embedded) return body
  return <div className="mx-auto max-w-6xl">{body}</div>
}

export default GrantsLedgerPage
