/** 消息段 API 服务（消息段模型：多代切换与压缩原文，方案 §5 读路径）
 *  [来源: docs/working/消息历史双能力方案_多代切换与压缩原文_20260923.md] */

import apiClient from '@/services/api/client'
import { API_ENDPOINTS } from '@/constants/api'
import type { BackendMessageResponse } from '@/services/api/session'

const SEGMENTS_OF_PIPELINE = (pipelineId: string) =>
  API_ENDPOINTS.MESSAGES.SEGMENTS_OF_PIPELINE(pipelineId)
const SEGMENT_DETAIL = (segmentId: string) => API_ENDPOINTS.MESSAGES.SEGMENT(segmentId)

/** 段元数据（GET message-segments 条目形状；方案 §4 物理编码的读面投影） */
export interface SegmentMeta {
  /** 段唯一标识（seg_<uuid>） */
  id: string
  /** 区间起点（slots 物理 seq 坐标系） */
  base_seq: number
  /** 区间长度（冻结时确定） */
  base_len: number
  /** 非启用段的可消费者标签集：空串 = 谁都可以看（‹i/n› 可切换），
   *  [user] = 仅用户界面可渲染（压缩原文段，模型永不消费） */
  visible_to: string
  /** 首条 content 截断（代际预览免解 blob） */
  preview: string | null
  created_at: string
}

/** 段详情：元数据 + 成员消息有序列表（引用列表解析后的消息本体） */
export interface SegmentDetail extends SegmentMeta {
  /** 消息数组，role/content/tool_calls 等与 GET messages 同一后端形状 */
  members: BackendMessageResponse[]
}

/**
 * 成员消息词汇归一：内核槽位 blob 存储词汇（message_id/seq）与 HTTP 投影
 * 词汇（id/sequence）并存，段成员自 blob 直读时可能是前者。统一补齐前端
 * 消费键（BackendMessageResponse），不改写其余字段。
 */
function normalizeSegmentMember(raw: Record<string, unknown>): BackendMessageResponse {
  const withIdentity = {
    ...raw,
    id: (raw.id ?? raw.message_id) as string,
    sequence: (raw.sequence ?? raw.seq) as number | undefined,
  }
  return withIdentity as BackendMessageResponse
}

/**
 * 段清单：某管道的全部段（按区间聚合）。
 *
 * 信封双形态：共享契约为 `{segments:[...]}`；内核 routes.rs 清单族落地为
 * `{items:[...]}`（list_pipelines 同款惯例）。两者都接受（契约形态优先），
 * 都缺 = 协议违反 fail-closed 抛错——空表会让 ‹i/n› 切换器静默消失，
 * 分不清「无段」与「协议破坏」。
 */
export async function getMessageSegments(pipelineId: string): Promise<SegmentMeta[]> {
  const response = await apiClient.get<{ segments?: unknown; items?: unknown }>(
    SEGMENTS_OF_PIPELINE(pipelineId),
  )
  const payload = response?.data
  const list = Array.isArray(payload?.segments)
    ? payload.segments
    : Array.isArray(payload?.items)
      ? payload.items
      : null
  if (!list) {
    throw new Error(`获取消息段列表失败：响应缺少 segments/items 字段（协议违反）(pipeline=${pipelineId})`)
  }
  return list as SegmentMeta[]
}

/**
 * 段详情：解析引用列表为成员消息（‹i/n› 切换预览/换装数据源；只读展开不限运行态）。
 *
 * 信封双形态：契约为 `{segment:{...members}}`；内核 routes.rs 将段对象本身
 * 置顶返回（members 在顶层）。两者都接受；members 缺失/非数组 = 协议违反。
 */
export async function getMessageSegmentDetail(segmentId: string): Promise<SegmentDetail> {
  const response = await apiClient.get<{ segment?: unknown }>(SEGMENT_DETAIL(segmentId))
  const payload = response?.data
  const segment = (
    payload && typeof payload === 'object' && payload.segment && typeof payload.segment === 'object'
      ? payload.segment
      : payload
  ) as SegmentDetail | undefined
  if (!segment || typeof segment !== 'object' || !Array.isArray(segment.members)) {
    throw new Error(`获取消息段详情失败：响应缺少 segment.members 字段（协议违反）(segment=${segmentId})`)
  }
  return {
    ...segment,
    members: segment.members.map((m) =>
      normalizeSegmentMember(m as unknown as Record<string, unknown>),
    ),
  }
}
