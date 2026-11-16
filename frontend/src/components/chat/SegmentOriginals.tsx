/**
 * SegmentOriginals — 段成员原文装配器（消息段模型 §5.1 展示层，宿主数据路由面）
 *
 * 职责只有装配，零卡面 UI：按 compression_ref.segment_id 取段详情（内核
 * REST），成员经 mapBackendMessageToMessage 映射、与主列表同一渲染流水线
 * （toRenderableMessages）还原为只读消息行，挂载在压缩卡（插件
 * compression_card webview 卡面，渲染本体归插件）下方。
 *
 * 落位依据（ADR 2026-08-28 压缩块消息化）：被压原文冻结为段
 * （visible_to=user，模型永不消费）——宿主是唯一同时摸得到内核段端点与
 * 宿主行渲染器的角色，取数+挂载天然归宿主（ADR 2026-10-14「宿主只做
 * 数据路由与挂载、渲染本体归插件」）。取数失败给一行提示，不阻塞主列表。
 */

import { useEffect, useState, type ReactNode } from 'react'
import { getMessageSegmentDetail } from '@/services/api/messageSegments'
import { mapBackendMessageToMessage } from '@/services/api/session'
import { toRenderableMessages } from '@/utils/messageDisplay'
import type { Message } from '@/types/models'

export interface SegmentOriginalsProps {
  /** 段引用（块消息 metadata.compression_ref.segment_id，写入方契约） */
  segmentId: string
  /** 成员映射所属会话（取自块消息 sessionId） */
  sessionId: string
  /** 段成员行渲染回调（宿主渲染本体：与主列表同一 MessageItem 行，只读） */
  renderMember: (message: Message) => ReactNode
}

/** 段成员原文：取数 + 还原 + 挂载（失败降级提示） */
export function SegmentOriginals({ segmentId, sessionId, renderMember }: SegmentOriginalsProps) {
  const [members, setMembers] = useState<Message[] | null>(null)
  const [loadFailed, setLoadFailed] = useState(false)

  useEffect(() => {
    if (!segmentId) return
    let cancelled = false
    setLoadFailed(false)
    getMessageSegmentDetail(segmentId)
      .then((detail) => {
        if (cancelled) return
        setMembers(
          toRenderableMessages(
            detail.members.map((raw) => mapBackendMessageToMessage(raw, sessionId)),
          ),
        )
      })
      .catch(() => {
        if (!cancelled) setLoadFailed(true)
      })
    return () => {
      cancelled = true
    }
  }, [segmentId, sessionId])

  return (
    <div data-testid="compression-segment-originals">
      {loadFailed && <div className="text-muted-foreground py-1 text-xs">原始消息加载失败</div>}
      {members?.map((m) => (
        <div key={m.id}>{renderMember(m)}</div>
      ))}
    </div>
  )
}
