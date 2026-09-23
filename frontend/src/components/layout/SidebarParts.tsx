/**
 * 侧边栏附属部件（从 Sidebar.tsx 拆出：冻结千行文件只许缩小）
 * - resolveSessionByPipeline：消息搜索命中管道 → 归属会话解析
 * - SIDEBAR_STYLES：Deep Space v2 侧栏尺寸
 * - SettingsEntryButton：设置常驻入口
 */
import { Settings } from '@/assets/icons'
import { cn } from '@/lib/utils'
import { openWorkspacePanelByPath } from '@/services/workspacePanelOpener'
import { mainPipelineIdOf } from '@/utils/mappers'
import type { Session } from '@/types'

/**
 * 消息搜索命中的管道 → 归属会话解析。
 *
 * 搜索结果的 session_id 是管道 ID（monitoring 插件 search 域回传 pipeline_id，
 * 12hex 短 id），而会话列表 id 是 thread_id（thread-xxx）——两者不同值。
 * 归属判定：主管道（pipelineIds[0] 映射真值）命中直接归该会话；否则线性扫描
 * 全部会话的 pipelineIds（含子管道）找包含该管道的会话；均未命中（旧数据
 * thread_id==pipeline_id 同值）时兜底按会话 id 本身匹配。无归属返回 null
 * （调用方放弃跳转，避免误切到无关会话）。
 */
export function resolveSessionByPipeline(pipelineId: string, sessions: Session[]): Session | null {
  if (!pipelineId) return null
  for (const s of sessions) {
    if (mainPipelineIdOf(s) === pipelineId) return s
  }
  for (const s of sessions) {
    if (s.pipelineIds?.includes(pipelineId)) return s
  }
  return sessions.find((s) => s.id === pipelineId) ?? null
}

/**
 * Deep Space v2 侧栏尺寸
 * 设计来源：画布 C · SideBar · 会话视图 (49:196)
 * - 宽度 288px，内边距 12px
 * - 头部 36px，搜索 32px，会话项 55px
 * - 折叠按钮放最顶部（用户决策）
 */
export const SIDEBAR_STYLES = {
  headerHeight: 'h-9', // 36px
  padding: 'p-3', // 12px
  paddingX: 'px-3',
  buttonSize: 'sm' as const,
  searchHeight: 'h-8', // 32px
  itemHeight: 55,
  width: {
    desktop: 288,
    smallDesktop: 260,
    mobile: 288,
  },
} as const

/**
 * 设置常驻入口：侧栏底栏一级可见齿轮按钮（展开态/折叠 rail 两形态），
 * 点击打开设置中枢（settings_hub 工作区页签）——模型配置等插件声明页
 * （contributes.pages space=settings）在其中左导航可达。
 */
export function SettingsEntryButton({ rail = false }: { rail?: boolean }) {
  return (
    <button
      type="button"
      onClick={() => openWorkspacePanelByPath('/settings')}
      className={cn(
        'text-muted-foreground hover:text-foreground flex shrink-0 items-center justify-center transition-colors hover:bg-[var(--hover-overlay)]',
        rail ? 'h-9 w-9 rounded-lg' : 'h-7 w-7 rounded-md',
      )}
      title="设置"
      aria-label="设置"
      data-testid={rail ? 'sidebar-rail-settings' : 'sidebar-settings'}
    >
      <Settings className={rail ? 'h-4 w-4' : 'h-3.5 w-3.5'} />
    </button>
  )
}
