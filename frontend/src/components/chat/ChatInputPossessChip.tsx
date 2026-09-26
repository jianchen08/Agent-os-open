/** 附身指示条 chip（roleplay.possess 桥宿主侧可视化）：附身期间常驻展示 + 一键解除 */

import { useRoleplayPossessStore } from '@/stores/roleplayPossessStore'
import type { RoleplayPossession } from '@/services/schema/modeOptions'

/** 附身指示条 props（possessed 由宿主 ChatInput 订阅传入；null = 未附身零渲染） */
interface ChatInputPossessChipProps {
  possessed: RoleplayPossession | null
}

/** 附身指示条 chip：轻量 chip 形态对齐任务模式选择器触发器（h-8 rounded-lg text-xs） */
export function ChatInputPossessChip({ possessed }: ChatInputPossessChipProps) {
  if (!possessed) return null
  return (
    <div
      data-testid="possess-indicator"
      className="bg-muted text-muted-foreground flex h-8 shrink-0 items-center gap-1.5 rounded-lg px-2.5 text-xs font-medium"
    >
      <span aria-hidden="true">🧑‍🎤</span>
      <span>已附身：{possessed.name}</span>
      <button
        type="button"
        className="hover:text-foreground ml-0.5 rounded px-0.5 text-sm leading-none"
        onClick={() => useRoleplayPossessStore.getState().clearPossessed()}
        aria-label="解除附身"
        title="解除附身"
      >
        ×
      </button>
    </div>
  )
}
