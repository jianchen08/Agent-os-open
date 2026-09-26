/**
 * 会话插件表单值的前端持久层
 *
 * 模态框里除标题外的字段全部由插件贡献（contributes.thread_fields 声明，
 * 字段可来自不同插件），值的存储键与生效路径由各插件声明表达。本层只承载
 * 「表单层已按声明组装好的保存产物」，不感知具体字段名：
 * - 创建：fieldMetadata 随 POST /api/v1/sessions 写入 thread metadata（出生值）
 * - 编辑保存：整包写入本地快照（values 区 + 已组装的 executionContext 区）
 * - 生效：下一次 send_message 把 executionContext 随消息携带（消息级
 *   execution_context，内核 chat.send_message 一等参数，优先级高于会话级注入）
 */

/** 单个会话的插件表单快照（v2：键为 metadata 存储形状，翻译已在表单层完成） */
export interface SessionFieldSnapshot {
  /** 插件表单值（metadata 存储形状；回显经 schema 反查回声明名） */
  values: Record<string, string>
  /** 按 x_execution_path 组装好的消息级执行上下文（无声明值时缺省） */
  executionContext?: Record<string, unknown>
  /** 扮演会话绑定（roleplay.continue 桥）：非空 = 每条消息以该 agent 身份发送 */
  agentId?: string
  /** 绑定卡显示名（输入区指示条展示用；缺席回退 agentId 尾段） */
  agentName?: string
  /** 会话化开演档：所选开场白文本（发送链并入 execution_context.roleplay_greeting） */
  roleplayGreeting?: string
  /** 会话化开演档：用户设定文本（发送链并入 execution_context.roleplay_user_persona） */
  roleplayUserPersona?: string
}

const STORAGE_PREFIX = 'session-exec-options:'

function storageKey(threadId: string): string {
  return `${STORAGE_PREFIX}${threadId}`
}

/** 损坏快照留证：warn（键名 + 原文摘要）+ 改名 .corrupt 后缀（rename 失败不阻断回退） */
function quarantineCorruptSnapshot(key: string, raw: string, cause: unknown): void {
  console.warn(
    `[sessionExecutionOptions] 快照损坏，按无记录回退出生值: key=${key} raw=${raw.slice(0, 80)}`,
    cause,
  )
  try {
    localStorage.setItem(`${key}.corrupt`, raw)
    localStorage.removeItem(key)
  } catch {
    // 留证失败不影响回退出生值的主流程
  }
}

function isSnapshot(value: unknown): value is SessionFieldSnapshot {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    return false
  }
  const v = value as { values?: unknown }
  return typeof v.values === 'object' && v.values !== null && !Array.isArray(v.values)
}

/** 读会话的编辑后快照；无记录/损坏返回 null（消费方回退出生值），损坏留证 */
export function loadSessionExecutionOptions(threadId: string): SessionFieldSnapshot | null {
  const key = storageKey(threadId)
  let raw: string | null
  try {
    raw = localStorage.getItem(key)
  } catch {
    // 存储读取失败（隐私模式等）按无记录处理
    return null
  }
  if (!raw) return null

  try {
    const parsed: unknown = JSON.parse(raw)
    if (isSnapshot(parsed)) return parsed
  } catch (error) {
    quarantineCorruptSnapshot(key, raw, error)
    return null
  }
  // 解析成功但结构不符（缺 values 区/数组）：同属损坏，留证后回退
  quarantineCorruptSnapshot(key, raw, new Error('快照结构不符（缺 values 区或为数组）'))
  return null
}

/**
 * 写会话的编辑后快照（覆盖式整包）。localStorage 满/禁用属环境故障：
 * 静默吞掉会让用户误以为已保存，抛出让调用方走统一错误上报。
 */
export function saveSessionExecutionOptions(
  threadId: string,
  snapshot: SessionFieldSnapshot,
): void {
  try {
    localStorage.setItem(storageKey(threadId), JSON.stringify(snapshot))
  } catch {
    throw new Error('本地存储不可用，会话执行设置未能保存')
  }
}

/** 删除会话时清理对应快照 */
export function clearSessionExecutionOptions(threadId: string): void {
  try {
    localStorage.removeItem(storageKey(threadId))
  } catch {
    // 清理失败不阻塞删除主流程（残留键在下次同名会话保存时被覆写）
  }
}
