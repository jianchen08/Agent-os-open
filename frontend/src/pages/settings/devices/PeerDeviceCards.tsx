/**
 * 注册表设备卡（「设备」卡列表成员）：可达性三态 + 卡内看地盘三态
 *
 * 数据源（均为已交付只读端点，零新造）：
 * - /ext/sync_agent/relay/devices（云端权威设备注册表透传，R30 换源；在线判定 = 心跳窗口）
 * - /ext/sync_agent/probe（有地址设备实测探活，ADR 2026-10-21-device-card-direct-connect）
 * - /ext/sync_agent/status（本机地盘身份去重）
 * - /ext/sync_agent/mirror/records?table=sessions|messages（镜像只读面）
 *
 * 可达性语义分层（禁混用一面旗子）：中枢卡 = 注册表拉取成功即「可达」（请求
 * 刚被它服务，心跳单源误报中和）；有地址卡 = 实测探活（可达 · xx ms / 不可达
 * （实测））；无地址卡（NAT 远端）= 心跳窗口（在线（心跳）/ 离线（心跳超窗））。
 *
 * 口径（用户拍板，禁加戏）：断链=写硬边界（快照态禁发消息/禁写操作，置灰+文案）；
 * 用户唯一概念=地盘（文案不用运维词汇）；本机不接管他端数据：镜像会话不进主
 * 会话列表/聊天流（D2 他端只读镜像）。
 */
import { useMemo, useState } from 'react'
import { API_BASE_URL } from '@/constants/api'
import {
  useDeviceProbesQuery,
  useMirrorMessagesQuery,
  useMirrorSessionsQuery,
  useRelayDevicesQuery,
  useSyncAgentStatusQuery,
} from '@/hooks/queries/useSyncMirrorQuery'
import { connectKernel } from '@/pages/settings/devices/connectKernel'
import { SelfDeviceCard } from '@/pages/settings/devices/SelfDeviceCard'
import {
  formatSnapshotTime,
  formatStorageLine,
  messagesOfSession,
  sessionsOfTerritory,
  snapshotTimeOf,
  type DeviceProbeResult,
  type MirrorRecord,
  type RelayDevice,
} from '@/services/api/syncMirror'
import { isLoopbackOrigin } from '@/services/connectionProbe'
import { borrowAndConnect } from '@/services/deviceBorrow'
import { getServerBaseUrl } from '@/services/shellApiBase'

const ROLE_LABELS: Record<string, string> = {
  user: '用户',
  assistant: '助手',
  system: '系统',
  tool: '工具',
}

function roleLabel(role: unknown): string {
  return ROLE_LABELS[String(role)] ?? '消息'
}

function messageText(row: Record<string, unknown>): string {
  const content = row.content
  return typeof content === 'string' ? content : JSON.stringify(content)
}

/** 可达性呈现三态（语义分层，禁混用一面旗子）；probe online=null = 探测进行中 */
type ReachView =
  | { kind: 'hub'; text: string; online: true }
  | { kind: 'probe'; text: string; online: boolean | null }
  | { kind: 'heartbeat'; text: string; online: boolean }

function reachViewOf(device: RelayDevice, probe: DeviceProbeResult | undefined): ReachView {
  if (device.is_hub) return { kind: 'hub', text: '可达', online: true }
  if (device.address) {
    if (probe === undefined) return { kind: 'probe', text: '探测中…', online: null }
    if (probe.error === 'no_address') {
      // 地址对本机不可用（回环自报/未透出）：如实回落心跳语义，不冒充实测
      return {
        kind: 'heartbeat',
        online: device.online,
        text: device.online ? '在线（心跳）' : '离线（心跳超窗）',
      }
    }
    const err = probe.error ? `· ${probe.error}` : ''
    return {
      kind: 'probe',
      online: probe.ok,
      text: probe.ok ? `可达 · ${probe.latency_ms}ms` : `不可达（实测${err}）`,
    }
  }
  return {
    kind: 'heartbeat',
    online: device.online,
    text: device.online ? '在线（心跳）' : '离线（心跳超窗）',
  }
}

export function PeerDeviceCards() {
  const statusQuery = useSyncAgentStatusQuery()
  const devicesQuery = useRelayDevicesQuery()

  /** 展开的设备卡（同时至多一张；展开即浏览其地盘） */
  const [expandedId, setExpandedId] = useState<string | null>(null)
  const [selectedSessionId, setSelectedSessionId] = useState<string | null>(null)
  /** 连接失败提示（探活闸拒绝切换；device_id → 错误文案，卡片留原位可重试） */
  const [connectErrors, setConnectErrors] = useState<Record<string, string>>({})

  const selfDeviceId = statusQuery.data?.loaded ? statusQuery.data?.device_id : undefined
  const registryDevices = devicesQuery.data?.devices

  /** 配对中枢的对端可达源站（非回环）：中枢卡注册表无地址（loopback 自配对
   * 已被端侧清空）时的卡上直连入口——中枢即本端所配对端，地址同源供给。 */
  const pairedHubOrigin = (() => {
    const relayUrl = statusQuery.data?.paired ? statusQuery.data?.relay_url : undefined
    if (!relayUrl) return null
    try {
      const origin = new URL(relayUrl).origin
      return isLoopbackOrigin(origin) ? null : origin
    } catch {
      return null
    }
  })()

  /** 本端卡连接态 = 本机内核档使用中；连远端时「已连接」呈现在中枢卡上 */
  const selfConnected = (getServerBaseUrl() ?? '') === '' 

  /** 统一渲染（注册表驱动，一机一卡）：全部注册表行进列表，本端行渲染本机卡；
   * 注册表暂缺本端行（未配对/降级）时合成一张，页面恒有「自己」的卡。 */
  const rows = useMemo(() => {
    const base = (registryDevices ?? []).filter(
      (d): d is RelayDevice => Boolean(d && d.device_id),
    )
    if (selfDeviceId && !base.some((d) => d.device_id === selfDeviceId)) {
      base.unshift({
        device_id: selfDeviceId,
        tier: statusQuery.data?.tier ?? '',
        app_version: '',
        online: false,
        last_heartbeat_at: '',
      })
    }
    return base
  }, [registryDevices, selfDeviceId, statusQuery.data])

  /** 当前连接的卡置顶（连谁谁置顶；连接判定 = 卡上直连地址即生效基址，本端行
   * 看连接档：本机内核档或所连即所配中枢 = 连接中） */
  const connectedBase = API_BASE_URL
  const sortedPeers = useMemo(() => {
    const connectedFirst = (d: RelayDevice) => {
      if (selfDeviceId && d.device_id === selfDeviceId) return selfConnected ? 0 : 1
      const addr = d.address || (d.is_hub ? pairedHubOrigin ?? '' : '')
      return addr !== '' && addr === connectedBase ? 0 : 1
    }
    const sorted = [...rows].sort((a, b) => connectedFirst(a) - connectedFirst(b))
    return sorted
  }, [rows, connectedBase, pairedHubOrigin, selfDeviceId, selfConnected])

  /** 有地址的非中枢卡走实测探活（中枢卡以注册表拉取成功为准，不重复探） */
  const probedIds = useMemo(
    () => rows.filter((d) => d.address && !d.is_hub).map((d) => d.device_id),
    [rows],
  )
  const probesQuery = useDeviceProbesQuery(probedIds)
  const probesById = useMemo(() => {
    const map = new Map<string, DeviceProbeResult>()
    for (const r of probesQuery.data ?? []) map.set(r.device_id, r)
    return map
  }, [probesQuery.data])

  const sessionsQuery = useMirrorSessionsQuery(expandedId !== null)
  const messagesQuery = useMirrorMessagesQuery(expandedId !== null && selectedSessionId !== null)

  const territorySessions: MirrorRecord[] | null =
    expandedId !== null && sessionsQuery.data
      ? sessionsOfTerritory(sessionsQuery.data.records, expandedId)
      : null
  const snapshotAt = territorySessions !== null ? snapshotTimeOf(territorySessions) : null
  const sessionMessages: MirrorRecord[] | null =
    selectedSessionId !== null && messagesQuery.data
      ? messagesOfSession(messagesQuery.data.records, selectedSessionId)
      : null

  const toggle = (deviceId: string) => {
    setExpandedId((cur) => (cur === deviceId ? null : deviceId))
    setSelectedSessionId(null)
  }

  return (
    <>
      {/* ── 注册表设备卡（设备同级；有地址卡上直连，可达性三态呈现）────── */}
      {devicesQuery.data === undefined ? (
        <p className="text-muted-foreground text-xs" data-testid="territory-list-unavailable">
          其他设备暂不可见：多端同步未启用（本机登录 admin 后自动入网，启用见插件设置）。
        </p>
      ) : (
        <>
        {/* 本端身份未知（状态面不可用）时先给本机卡——连接面不丢 */}
        {!selfDeviceId && <SelfDeviceCard connected={selfConnected} />}
        {sortedPeers.map((d) => {
          if (selfDeviceId && d.device_id === selfDeviceId) {
            return <SelfDeviceCard key={d.device_id} connected={selfConnected} />
          }
          const isOpen = expandedId === d.device_id
          const reach = reachViewOf(d, probesById.get(d.device_id))
          /** 卡上直连地址：注册表地址优先；中枢卡缺地址回落配对中枢源站 */
          const connectAddress = d.address || (d.is_hub && pairedHubOrigin ? pairedHubOrigin : '')
          const isConnected = connectAddress !== '' && connectAddress === connectedBase
          return (
            <div
              key={d.device_id}
              className={`rounded-lg border ${isConnected || isOpen ? 'border-[var(--ds-border-active)]' : 'border-border'} p-4`}
              data-testid={`device-card-${d.device_id}`}
            >
              {/* 头部 flex-wrap：窄视口（375）长 device_id/meta 折行，主操作钮保持视口内可达 */}
              <div className="flex flex-wrap items-center gap-2 text-xs">
                <span
                  className={`inline-block h-2 w-2 shrink-0 rounded-full ${
                    reach.online === null
                      ? 'bg-muted-foreground/20 animate-pulse'
                      : reach.online
                        ? 'bg-status-success'
                        : 'bg-muted-foreground/40'
                  }`}
                  data-testid={`device-card-dot-${d.device_id}`}
                />
                {/* device_id 是无空格长 token（UUID/hex）：break-words 无断点不可用，须 break-all 才断行 */}
                <span className="min-w-0 break-all font-mono font-medium">{d.device_id}</span>
                <span className="min-w-0 text-muted-foreground">
                  {d.tier || '未知档位'} · v{d.app_version || '?'} ·{' '}
                  {d.storage && <>{formatStorageLine(d.storage)} · </>}
                </span>
                <span data-testid={`device-card-reach-${d.device_id}`}>{reach.text}</span>
                <span className="ml-auto flex shrink-0 items-center gap-1.5">
                  {connectAddress ? (
                    connectAddress === API_BASE_URL ? (
                      <span
                        className="text-[var(--ds-accent-primary)] px-1"
                        data-testid={`device-card-connected-${d.device_id}`}
                      >
                        已连接
                      </span>
                    ) : (
                      <button
                        type="button"
                        onClick={() => {
                          setConnectErrors((cur) => ({ ...cur, [d.device_id]: '' }))
                          void borrowAndConnect(connectAddress, d.is_hub === true ? true : undefined).then(
                            (r) => {
                              if (!r.ok) {
                                setConnectErrors((cur) => ({
                                  ...cur,
                                  [d.device_id]: r.error,
                                }))
                              }
                            },
                          )
                        }}
                        className="border-border hover:bg-muted/30 rounded-lg border px-2.5 py-1"
                        data-testid={`device-card-connect-${d.device_id}`}
                      >
                        连接此内核
                      </button>
                    )
                  ) : null}
                  <button
                    type="button"
                    onClick={() => toggle(d.device_id)}
                    className="border-border hover:bg-muted/30 rounded-lg border px-2.5 py-1"
                    data-testid={`device-card-toggle-${d.device_id}`}
                  >
                    {isOpen ? '收起' : '看地盘'}
                  </button>
                </span>
              </div>

              {connectErrors[d.device_id] && (
                <p className="text-destructive mt-2 text-xs" data-testid={`device-card-connect-error-${d.device_id}`}>
                  {connectErrors[d.device_id]}
                  <button
                    type="button"
                    onClick={() => {
                      setConnectErrors((cur) => ({ ...cur, [d.device_id]: '' }))
                      void connectKernel(connectAddress)
                    }}
                    className="text-primary ml-2 underline"
                    data-testid={`device-card-connect-fallback-${d.device_id}`}
                  >
                    仍要切换（到对方登录页）
                  </button>
                </p>
              )}

              {isOpen && (
                <div
                  className="border-border mt-3 border-t pt-3"
                  data-testid={`device-card-body-${d.device_id}`}
                >
                  {sessionsQuery.isError && (
                    <p className="text-destructive text-xs" data-testid="territory-snapshot-error">
                      快照数据暂不可见：读取失败，请稍后重试。
                    </p>
                  )}
                  {!sessionsQuery.isError && !d.online && snapshotAt !== null && (
                    <p
                      className="text-status-warning mb-3 text-xs font-medium"
                      data-testid="snapshot-banner"
                    >
                      离线快照 · 截至 {formatSnapshotTime(snapshotAt)} · 不可干活
                    </p>
                  )}
                  {!sessionsQuery.isError && territorySessions !== null && territorySessions.length === 0 && (
                    <p className="text-muted-foreground text-xs" data-testid="territory-empty">
                      {d.online ? '该地盘可达，暂无同步会话。' : '该地盘不可达且无快照。'}
                    </p>
                  )}
                  {territorySessions !== null && territorySessions.length > 0 && (
                    <ul className="flex flex-col gap-1" data-testid="territory-session-items">
                      {territorySessions.map((s) => (
                        <li key={s.asset_key}>
                          <button
                            type="button"
                            onClick={() => setSelectedSessionId(s.pk)}
                            className={`w-full rounded-lg px-2.5 py-2 text-left font-mono text-xs transition-colors ${
                              selectedSessionId === s.pk
                                ? 'bg-[var(--ds-bg-elevated)] text-foreground ring-1 ring-[var(--ds-border-active)]'
                                : 'hover:bg-[var(--ds-bg-hover)] text-[var(--ds-text-secondary)]'
                            }`}
                            data-testid={`territory-session-row-${s.pk}`}
                            title={s.pk}
                          >
                            会话 {s.pk.slice(0, 12)}
                          </button>
                        </li>
                      ))}
                    </ul>
                  )}

                  {/* ── 选中会话的详情（只读快照；断链=写硬边界）────────── */}
                  {selectedSessionId !== null && (
                    <div
                      className="border-border mt-3 rounded-lg border p-3"
                      data-testid="territory-session-detail"
                    >
                      {messagesQuery.isError && (
                        <p className="text-destructive text-xs" data-testid="territory-messages-error">
                          快照数据暂不可见：读取失败，请稍后重试。
                        </p>
                      )}
                      {sessionMessages !== null && sessionMessages.length === 0 && (
                        <p className="text-muted-foreground text-xs" data-testid="territory-messages-empty">
                          该会话暂无同步消息。
                        </p>
                      )}
                      {sessionMessages !== null && sessionMessages.length > 0 && (
                        <div
                          className={`flex flex-col gap-3 ${d.online ? '' : 'opacity-60'}`}
                          data-testid="territory-messages"
                          data-dimmed={d.online ? 'false' : 'true'}
                        >
                          {sessionMessages.map((m) => (
                            <div key={m.asset_key} className="text-xs" data-testid="mirror-message">
                              <span className="text-muted-foreground mr-2 font-medium">
                                {roleLabel(m.row.role)}
                              </span>
                              {/* 镜像消息可能含 JSON/base64/URL 等长 token：break-words 仅实际溢出才断行（正常散文换行点不变，桌面无感） */}
                              <span className="whitespace-pre-wrap break-words">{messageText(m.row)}</span>
                            </div>
                          ))}
                        </div>
                      )}
                      {!d.online && (
                        <div className="mt-4 flex gap-2" data-testid="snapshot-write-blocked">
                          <input
                            type="text"
                            disabled
                            placeholder="离线快照，不可发送消息"
                            className="border-border bg-background h-9 flex-1 rounded-lg border px-3 text-sm opacity-60"
                            data-testid="snapshot-input"
                          />
                          <button
                            type="button"
                            disabled
                            className="bg-primary text-primary-foreground h-9 shrink-0 rounded-lg px-4 text-sm opacity-50"
                            data-testid="snapshot-send"
                          >
                            发送
                          </button>
                        </div>
                      )}
                    </div>
                  )}
                </div>
              )}
            </div>
          )
        })}
        {sortedPeers.length <= (selfDeviceId ? 1 : 0) && (
          <p className="text-muted-foreground text-xs" data-testid="territory-list-empty">
            尚无其他设备：设备在对方端登录 admin 后即自动入网（登录即入网）。
          </p>
        )}
        </>
      )}
    </>
  )
}
