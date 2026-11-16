/**
 * 会话身份绑定指示条 —— mode.session 桥落地的会话执行选项绑定（绑定存在且未附身
 * 时常驻展示 + 一键退出；与附身 chip 互斥，附身是显式全局态，优先展示——发送链同序）。
 */

export interface SessionAgentIndicatorProps {
  /** 绑定的执行者名（展示用） */
  name: string
  /** 退出绑定（清会话执行选项的 agentId，发送链随之不带身份） */
  onExit: () => void
}

export function SessionAgentIndicator({ name, onExit }: SessionAgentIndicatorProps) {
  return (
    <div
      data-testid="session-agent-indicator"
      className="bg-muted text-muted-foreground flex h-8 shrink-0 items-center gap-1.5 rounded-lg px-2.5 text-xs font-medium"
    >
      <span aria-hidden="true">👤</span>
      <span>会话身份：{name}</span>
      <button
        type="button"
        className="hover:text-foreground ml-0.5 rounded px-0.5 text-sm leading-none"
        onClick={onExit}
        aria-label="退出会话身份绑定"
        title="退出会话身份绑定"
      >
        ×
      </button>
    </div>
  )
}
