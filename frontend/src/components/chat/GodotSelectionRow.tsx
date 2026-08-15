/**
 * Godot 实时选中引用行——聊天输入框上方的选中状态实时镜像：
 * Godot 选中节点即出现，取消选中即消失（全程事件驱动，无手动操作）。
 */
import { useGodotSelection } from '@/hooks/useGodotSelection'
import { godotPreviewUrl } from '@/services/godot/selectionBridge'
import { ReferenceChip, type ReferenceChipData } from './ReferenceChip'

export function GodotSelectionRow({ threadId }: { threadId?: string }) {
  const sel = useGodotSelection(threadId)
  if (!sel.items.length) return null

  const chips: ReferenceChipData[] = sel.items.map((it, i) => ({
    kind: 'godot-node',
    title: it.name,
    subtitle: `${it.type} @ ${it.path}`,
    previewUrl: it.preview_kind ? godotPreviewUrl(i, sel.signature) : undefined,
  }))

  return (
    <div className="mb-2 flex flex-wrap items-center gap-2" data-testid="godot-selection-row">
      <span className="text-muted-foreground inline-flex items-center gap-1.5 text-[11px]">
        <span
          className={`inline-block h-1.5 w-1.5 rounded-full ${
            sel.connected ? 'bg-status-success' : 'bg-status-warning'
          }`}
        />
        Godot 引用
      </span>
      {chips.map((chip) => (
        <ReferenceChip key={`${chip.title}-${chip.subtitle}`} data={chip} />
      ))}
    </div>
  )
}
