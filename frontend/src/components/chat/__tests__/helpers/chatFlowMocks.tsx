/**
 * chat 交互流测试家族共享的 UI mock 工厂（lucide 图标/Button/cn）。
 *
 * 这些工厂曾在多个测试文件逐字复制（jscpd 克隆门禁重复源）；
 * 统一为异步工厂引用：`vi.mock('lucide-react', async () => (await import('./helpers/chatFlowMocks')).lucideMock())`。
 */
import React from 'react'

const CHAT_FLOW_ICONS = [
  'ArrowRight',
  'Ban',
  'Check',
  'CheckCircle2',
  'ChevronDown',
  'ChevronRight',
  'Clock',
  'Copy',
  'AlertTriangle',
  'Loader2',
  'MessageSquare',
  'Play',
  'RefreshCw',
  'Send',
  'Sparkles',
  'Target',
  'Wrench',
  'X',
  'XCircle',
]

export function lucideMock(extraIcons: string[] = []) {
  const m: Record<string, any> = {}
  for (const name of [...CHAT_FLOW_ICONS, ...extraIcons]) {
    m[name] = (p: any) => <svg data-testid={`icon-${name}`} {...p} />
  }
  return m
}

export function cnMock() {
  return {
    cn: (...args: (string | undefined | null | false)[]) => args.filter(Boolean).join(' '),
  }
}

export function buttonMock() {
  return {
    Button: ({
      children,
      onClick,
      disabled,
      ...rest
    }: {
      children: React.ReactNode
      onClick?: () => void
      disabled?: boolean
      [key: string]: any
    }) => (
      <button
        data-testid={`button-${typeof children === 'string' ? children : 'action'}`}
        onClick={onClick}
        disabled={disabled}
        {...rest}
      >
        {children}
      </button>
    ),
  }
}

export function dialogMock() {
  return {
    Dialog: ({ children, open }: { children: React.ReactNode; open?: boolean }) => {
      if (!open) return null
      return <div data-testid="dialog-root">{children}</div>
    },
    DialogContent: ({ children, className }: { children: React.ReactNode; className?: string }) => (
      <div data-testid="dialog-content" className={className}>
        {children}
      </div>
    ),
    DialogHeader: ({ children }: { children: React.ReactNode }) => (
      <div data-testid="dialog-header">{children}</div>
    ),
    DialogTitle: ({ children }: { children: React.ReactNode }) => (
      <h2 data-testid="dialog-title">{children}</h2>
    ),
    DialogDescription: ({ children }: { children: React.ReactNode }) => (
      <p data-testid="dialog-description">{children}</p>
    ),
    DialogFooter: ({ children }: { children: React.ReactNode }) => (
      <div data-testid="dialog-footer">{children}</div>
    ),
    DialogPortal: ({ children }: { children: React.ReactNode }) => <>{children}</>,
    DialogOverlay: () => <div data-testid="dialog-overlay" />,
    DialogTrigger: ({ children }: { children: React.ReactNode }) => <>{children}</>,
    DialogClose: ({ children, onClick }: { children: React.ReactNode; onClick?: () => void }) => (
      <button data-testid="dialog-close" onClick={onClick}>{children}</button>
    ),
  }
}

/** agentTabStore 测试替身（组件选择器子集，无网络副作用），供 ChatContainer 族测试共享 */
export async function agentTabStoreDouble() {
  const { create } = await import('zustand')
  const useAgentTabStore = create<any>(() => ({
    tabs: [],
    activeTabId: null,
    unreadCounts: {},
    switchToTab: vi.fn(),
    closeTab: vi.fn(),
    initSessionTabs: vi.fn(),
  }))
  return { useAgentTabStore }
}
