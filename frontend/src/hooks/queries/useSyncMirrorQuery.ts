/**
 * 只读快照 query（服务端状态 query 化，useSessionsQuery 同款模式）
 *
 * 缓存 key 单点在 queryKeys；staleTime 与会话列表同窗（30s 内切页零请求）。
 * 镜像面 enabled 门控：未选中地盘/会话时不发请求。
 */

import { useQuery } from '@tanstack/react-query'
import {
  fetchMirrorRecords,
  fetchRelayDevices,
  fetchSyncAgentStatus,
  probeDevices,
} from '@/services/api/syncMirror'
import { queryKeys } from '@/services/query/queryKeys'

/** 新鲜窗口：与 useSessionsQuery 同窗（窗口内切页/重挂零请求） */
const SNAPSHOT_STALE_TIME = 30_000

/** 本机同步状态（本机地盘身份） */
export function useSyncAgentStatusQuery() {
  return useQuery({
    queryKey: queryKeys.syncAgentStatus,
    queryFn: () => fetchSyncAgentStatus(),
    staleTime: SNAPSHOT_STALE_TIME,
  })
}

/** 地盘可达性（relay 设备注册表，online = 心跳窗口内） */
export function useRelayDevicesQuery() {
  return useQuery({
    queryKey: queryKeys.relayDevices,
    queryFn: () => fetchRelayDevices(),
    staleTime: SNAPSHOT_STALE_TIME,
  })
}

/** 镜像会话记录（table=sessions；选中地盘时才拉取） */
export function useMirrorSessionsQuery(enabled: boolean) {
  return useQuery({
    queryKey: queryKeys.syncMirrorRecords('sessions'),
    queryFn: () => fetchMirrorRecords('sessions'),
    enabled,
    staleTime: SNAPSHOT_STALE_TIME,
  })
}

/** 镜像消息记录（table=messages；选中会话时才拉取） */
export function useMirrorMessagesQuery(enabled: boolean) {
  return useQuery({
    queryKey: queryKeys.syncMirrorRecords('messages'),
    queryFn: () => fetchMirrorRecords('messages'),
    enabled,
    staleTime: SNAPSHOT_STALE_TIME,
  })
}

/**
 * 注册表设备实测探活（有地址的设备卡可达性数据源；探活是即时证据不做
 * 长缓存——进页实测一次，30s 窗口内切页零请求）。
 */
export function useDeviceProbesQuery(deviceIds: string[]) {
  return useQuery({
    queryKey: queryKeys.deviceProbes(deviceIds),
    queryFn: () => probeDevices(deviceIds),
    enabled: deviceIds.length > 0,
    staleTime: SNAPSHOT_STALE_TIME,
    retry: 1,
  })
}
