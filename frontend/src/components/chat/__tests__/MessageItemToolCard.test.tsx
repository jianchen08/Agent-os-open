/**
 * 工具卡片（tool card）统一形态测试（2026-08-19 统一改造后）
 *
 * 独立 tool 消息（未被 assistant parts 吸收的兜底路径）不再用自制"文本行 +
 * 状态药丸 + 查看详情"样式，统一走 ActivityCard（与消息流 parts 吸收路径同款
 * 满宽卡：registry 增强、render 卡、subtitle、打开文件入口、时长格式化）。
 *
 * 验证 AC：
 * 1. 容器宽度对齐气泡（max-w-[calc(100%-44px)]）且卡片满宽（w-full max-w-full）
 * 2. 长结果展开可读（统一滚动/换行样式契约）
 * 3. 出错默认折叠，点击头部展开
 * 4. 旧 UI 退役：无状态药丸文本、无裸时长字符串
 * 5. 统一增强管线：声明 render 的工具拿到人性化标题/subtitle/打开文件入口
 */
import { fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { MessageItem } from '../MessageItem'
import { clearRenderIntents, loadRenderIntents } from '@/utils/dshRenderIntent'
import type { Message } from '@/types/models'

// ============================================================
// Mock 外部依赖（MessageItem 依赖较重，仅保留被测分支需要的最小依赖）
// 注：不再 mock toolCardRegistry —— 统一后 isTool 分支走真实增强管线
// （activityConverter → toolCardRegistry → render 意图路由），mock 会砍断链路。
// ============================================================

vi.mock('@/stores/sessionStore', () => ({
  useSessionStore: () => ({ activeSessionId: 'session-1' }),
}))

vi.mock('@/stores/agentStore', () => ({
  useAgentStore: () => ({ agents: [] }),
}))

vi.mock('@/stores/interactionStore', () => ({
  useInteractionStore: () => ({ pendingInteractions: [] }),
}))

vi.mock('@/services/errorReporting', () => ({
  ErrorType: { CLIENT: 'client' },
  reportError: vi.fn(),
}))

vi.mock('@/services/attachmentOpener', () => ({
  openAttachment: vi.fn(),
}))

vi.mock('@/components/chat/MessageActions', () => ({
  MessageActions: () => null,
}))

vi.mock('@/components/chat/MessageContentRenderer', () => ({
  default: () => null,
}))

vi.mock('@/components/chat/hooks/useMessageRender', () => {
  const useMessageRender = () => ({ fragments: [], isStreaming: false })
  return { useMessageRender, default: useMessageRender }
})

// ============================================================
// 测试数据工厂
// ============================================================

function makeToolMessage(overrides: Partial<Message> = {}): Message {
  return {
    id: 'tool-1',
    sessionId: 'session-1',
    sequence: 2,
    role: 'tool',
    content: '',
    timestamp: new Date().toISOString(),
    status: 'completed',
    toolName: 'search',
    ...overrides,
  } as Message
}

/** 展开卡片（点卡片头部标题切换；'search' 工具人性化标题为 'Search'） */
function expandCard() {
  fireEvent.click(screen.getByText('Search'))
}

// ============================================================
// 测试套件
// ============================================================

describe('工具卡统一形态: 独立 tool 消息走 ActivityCard（2026-08-19）', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })
  afterEach(() => clearRenderIntents())

  describe('AC1: 宽度对齐气泡 + 卡片满宽', () => {
    it('容器含气泡同宽约束（max-w-[calc(100%-44px)]），卡片含满宽类（w-full max-w-full）', () => {
      render(<MessageItem message={makeToolMessage({ toolResult: '结果' })} />)

      const container = screen.getByTestId('message-item')
      expect(container.getAttribute('data-role')).toBe('tool')
      expect(container.className).toContain('max-w-[calc(100%-44px)]')

      const card = document.querySelector('[data-activity-type="tool_call"]')
      expect(card).toBeInTheDocument()
      expect(card!.className).toContain('w-full')
      expect(card!.className).toContain('max-w-full')
      // 紧凑变体类已退役
      expect(card!.className).not.toContain('w-fit')
    })
  })

  describe('AC2+AC3: 长结果展开可读（统一滚动/换行样式）', () => {
    it('展开后超长对象结果入 json 块：max-h + overflow-y-auto + 语义换行，无 break-all', () => {
      render(
        <MessageItem
          message={makeToolMessage({
            toolResult: { data: { rows: 'x'.repeat(2000) } },
          })}
        />,
      )

      // 默认折叠：内容不可见
      expect(screen.queryByText(/x{50}/)).not.toBeInTheDocument()

      expandCard()
      // 嵌套对象块（collapsible）再展开
      fireEvent.click(screen.getByText('data'))
      const pre = document.querySelector('[data-activity-type="tool_call"] pre')
      expect(pre).toBeInTheDocument()
      expect(pre!.className).toContain('max-h-40')
      expect(pre!.className).toContain('overflow-y-auto')
      expect(pre!.className).toContain('whitespace-pre-wrap')
      expect(pre!.className).toContain('break-words')
      expect(pre!.className).not.toContain('break-all')
    })
  })

  describe('AC4: 出错不自动展开（默认折叠）', () => {
    it('含错误的工具卡默认折叠，点击头部展开后错误可见，再点折叠', () => {
      render(
        <MessageItem
          message={makeToolMessage({
            status: 'failed',
            toolError: '连接超时：上游服务不可达',
            toolResult: undefined,
          })}
        />,
      )

      expect(screen.queryByText(/连接超时/)).not.toBeInTheDocument()

      expandCard()
      expect(screen.getByText(/连接超时/)).toBeInTheDocument()

      expandCard()
      expect(screen.queryByText(/连接超时/)).not.toBeInTheDocument()
    })
  })

  describe('旧 UI 退役：状态药丸与裸时长不再出现', () => {
    it('无"已完成"药丸文本；时长经 formatDuration 格式化（浮点不再原样输出）', () => {
      render(
        <MessageItem
          message={makeToolMessage({ toolResult: 'ok', durationMs: 1234.5678 })}
        />,
      )

      expect(screen.queryByText('已完成')).not.toBeInTheDocument()
      expect(screen.queryByText('查看详情')).not.toBeInTheDocument()
      expect(screen.getByText('1.2s')).toBeInTheDocument()
      expect(screen.queryByText(/1234\.5678/)).not.toBeInTheDocument()
    })
  })

  describe('统一增强管线：声明 render 的工具与消息流同款渲染', () => {
    it('file_read（read 卡声明）→ 人性化标题 + subtitle + 打开文件入口', () => {
      loadRenderIntents([{ name: 'file_read', render: { card: 'read' } }])
      render(
        <MessageItem
          message={makeToolMessage({
            toolName: 'file_read',
            toolResult: { file: 'src/main.py', content: 'print(1)' },
            metadata: { args: { path: 'src/main.py' } },
          })}
        />,
      )

      expect(screen.getByText('读取文件')).toBeInTheDocument()
      expect(screen.getByText('src/main.py')).toBeInTheDocument() // subtitle 摘要
      expect(
        screen.getByRole('button', { name: '打开文件 src/main.py' }),
      ).toBeInTheDocument()
    })
  })
})
