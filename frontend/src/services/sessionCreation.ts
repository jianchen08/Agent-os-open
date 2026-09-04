/**
 * 会话创建共享流（Sidebar 侧边栏与声明式表单页 FormWidget createSession 共用）
 *
 * createSession 落会话 + 主管道（sessionListStore 编排：activeSessionId /
 * initSessionTabs / activatePipeline，创建即激活）；声明了工作空间目录
 * （source_path）时随建即登记项目（projects 域，同路径幂等复用）。
 * 登记失败不阻断会话创建（会话是主体），错误上报可见。
 */
import { createProject } from '@/services/api/tasks'
import { reportError, ErrorSeverity, ErrorType } from '@/services/errorReporting'
import { useSessionListStore } from '@/stores/sessionListStore'
import type { Session } from '@/types'
import type { SessionFormOptions } from '@/components/session/SessionEditModal'

/**
 * 会话目录 → 项目登记：保存会话时若声明了工作空间目录（source_path），
 * 立即以该目录为项目登记（projects 域 API；同路径幂等复用）。
 *
 * 登记失败不阻断会话保存（会话保存是主体），错误上报可见。
 */
export async function registerSessionProject(
  sessionId: string,
  title: string,
  options?: SessionFormOptions,
  componentName = 'Sidebar',
): Promise<void> {
  const wsSpec = options?.executionContext?.workspace
  const sourcePath =
    wsSpec && typeof wsSpec === 'object' && 'source_path' in wsSpec
      ? String((wsSpec as { source_path?: unknown }).source_path ?? '')
      : ''
  if (!sourcePath.trim()) return
  try {
    await createProject(title.trim() || '新会话', sessionId, { path: sourcePath.trim() })
  } catch (error) {
    reportError(error instanceof Error ? error.message : String(error), {
      type: ErrorType.SERVER,
      severity: ErrorSeverity.ERROR,
      componentName,
      operation: 'registerSessionProject',
      sessionId,
    })
  }
}

/**
 * 创建会话（主管道随生激活）+ 按声明登记项目。
 *
 * 两个入口（Sidebar 侧边栏、触发器等声明式表单页）共享同一创建语义。
 */
export async function createSessionWithProject(
  title: string | undefined,
  agentId: string | null,
  options?: SessionFormOptions,
  componentName = 'Sidebar',
): Promise<Session> {
  const created = await useSessionListStore.getState().createSession(title, {
    agentId: agentId || undefined,
    fieldMetadata: options?.fieldMetadata,
  })
  await registerSessionProject(created.id, created.title || title || '', options, componentName)
  return created
}
