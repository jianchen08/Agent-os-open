/**
 * CompressionOriginalsButton — 压缩块的宿主侧「查看原始 N 条」入口
 *
 * 背景（消息段模型 §5.1 展开双层）：压缩卡（webview）的「查看原始」按钮因
 * 容器上行桥只放行 /ext/{pluginId}/** 插件端点、内核段读取端点不可达而只显
 * 示提示（web/cards/compression.html 取数缺口登记）。宿主侧桥接 = 本组件：
 * 对携带 compression_ref.segment_id 的 system 块消息，由宿主直接取段详情
 * （getMessageSegmentDetail → GET message-segments/{id}）并经 Modal 只读渲染
 * 成员消息列表——不激活、不写序列（§2.5 段展开为只读视图）。
 *
 * 计数口径与卡一致：优先 compression_ref.seq_range 跨度，缺省退化为「查看原始」。
 */
import { Modal } from 'antd'
import { useState } from 'react'
import { toast } from 'sonner'
import { getMessageSegmentDetail, type SegmentDetail } from '@/services/api/messageSegments'
import { formatTimestamp } from '@/utils/format'
import type { BackendMessageResponse } from '@/services/api/session'
import type { Message } from '@/types/models'

export interface CompressionOriginalsButtonProps {
  /** 压缩块消息（role=system，metadata.compression_ref 携带段引用） */
  message: Message
}

/** compression_ref 形状（写入方契约：segment_id + seq_range） */
interface CompressionRef {
  segment_id?: unknown
  seq_range?: unknown
}

export function readCompressionRef(message: Message): {
  segmentId: string
  count: number
} | null {
  if (message.role !== 'system') return null
  const ref = message.metadata?.compression_ref as CompressionRef | undefined
  const segmentId = ref && typeof ref.segment_id === 'string' ? ref.segment_id : ''
  if (!segmentId) return null
  const range = ref && Array.isArray(ref.seq_range) ? (ref.seq_range as unknown[]) : null
  const start = range?.[0]
  const end = range?.[1]
  const count =
    typeof start === 'number' && typeof end === 'number' && end >= start ? end - start + 1 : 0
  return { segmentId, count }
}

/** 宿主侧「查看原始 N 条」按钮 + 只读 Modal（段成员消息列表） */
export function CompressionOriginalsButton({
  message,
}: CompressionOriginalsButtonProps) {
  const ref = readCompressionRef(message)
  const [open, setOpen] = useState(false)
  const [loading, setLoading] = useState(false)
  const [detail, setDetail] = useState<SegmentDetail | null>(null)

  if (!ref) return null

  const handleOpen = () => {
    setOpen(true)
    if (detail || loading) return
    setLoading(true)
    getMessageSegmentDetail(ref.segmentId)
      .then((d) => setDetail(d))
      .catch(() => {
        toast.error('获取原始消息失败，请稍后重试')
        setOpen(false)
      })
      .finally(() => setLoading(false))
  }

  return (
    <>
      <button
        type="button"
        className="text-muted-foreground mt-1 text-xs underline hover:opacity-80"
        data-testid="compression-originals-button"
        onClick={handleOpen}
      >
        {ref.count > 0 ? `查看原始 ${ref.count} 条` : '查看原始'}
      </button>
      <Modal
        open={open}
        onCancel={() => setOpen(false)}
        footer={null}
        width={640}
        title={detail ? `原始消息（${detail.members.length} 条）` : '原始消息'}
      >
        {loading && <div className="text-muted-foreground py-4 text-sm">加载中...</div>}
        {!loading && detail && (
          <div className="flex max-h-[60vh] flex-col gap-2 overflow-y-auto" data-testid="originals-list">
            {detail.members.map((m: BackendMessageResponse, i: number) => (
              <div
                key={m.id ?? `member-${i}`}
                className="border-border/40 bg-muted/30 rounded-md border px-3 py-2 text-sm"
                data-testid="original-message"
                data-role={m.role}
              >
                <div className="text-muted-foreground mb-1 text-xs">
                  {m.role}
                  {m.timestamp ? ` · ${formatTimestamp(m.timestamp)}` : ''}
                </div>
                <div className="break-words whitespace-pre-wrap">{m.content}</div>
              </div>
            ))}
          </div>
        )}
        {!loading && !detail && (
          <div className="text-muted-foreground py-4 text-sm">等待加载...</div>
        )}
      </Modal>
    </>
  )
}

export default CompressionOriginalsButton
