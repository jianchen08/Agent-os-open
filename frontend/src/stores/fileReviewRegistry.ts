/**
 * 文件审批数据注册表
 *
 * 跨组件数据传递层：useInteractionHandler 写入 → FiveSpaceLayout 读取。
 * 使用 module-level Map 存储，不依赖 Zustand，轻量无副作用。
 *
 * 为什么不直接用 interactionStore？
 * → interactionStore 在用户响应后 2 秒自动清理数据，
 *   但文件审批 Tab 需要在用户响应后仍然保持可查看状态，
 *   因此需要独立于 interactionStore 的持久化存储。
 */

/** 文件审批 Tab 所需的完整数据 */
export interface FileReviewData {
  /** 交互请求 ID */
  requestId: string
  /** 交互模式 */
  mode: 'choice' | 'conversation' | 'notification'
  /** 交互标题 */
  title: string
  /** 管道 ID（用于发送消息） */
  pipelineId: string
  /** 文件内容映射 {文件路径: 文件内容} */
  fileContents: Record<string, string>
  /** 选项列表（choice 模式） */
  options?: Array<{ id: string; label: string }>
  /** 所属会话 ID */
  sessionId?: string
  /** 容器任务 ID（工作空间模式，用于保存文件） */
  containerTaskId?: string
}

/** 内部存储：tabId → FileReviewData */
const reviewDataMap = new Map<string, FileReviewData>()

/**
 * 注册文件审批数据
 *
 * @param tabId - 工作区 Tab ID（格式：review-${requestId}）
 * @param data - 完整的文件审批数据
 */
export function registerFileReview(tabId: string, data: FileReviewData): void {
  reviewDataMap.set(tabId, data)
}

/**
 * 获取文件审批数据
 *
 * @param tabId - 工作区 Tab ID
 * @returns 文件审批数据，不存在则返回 undefined
 */
export function getFileReviewData(tabId: string): FileReviewData | undefined {
  return reviewDataMap.get(tabId)
}

/**
 * 移除文件审批数据
 *
 * 在关闭 Tab 时调用，防止内存泄漏。
 *
 * @param tabId - 工作区 Tab ID
 */
export function removeFileReviewData(tabId: string): void {
  reviewDataMap.delete(tabId)
}
