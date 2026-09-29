/**
 * 管道级执行绑定（B8 管道标签，2026-09-29）
 *
 * 会话内子管道的模式绑定：{mode, pipelineConfigId?, agentId?} 按 pipelineId
 * 存储（localStorage，session-exec-options 同款形态）。发送链优先序：
 * **管道绑定 > 会话快照 modeBinding**——子管道的模式参数不污染会话出生语义。
 */
import { generateUUID } from '@/utils/uuid'

export interface PipelineExecutionBinding {
  mode: string
  pipelineConfigId?: string
  agentId?: string
}

const PREFIX = 'pipeline-exec-options:'

function storageKey(pipelineId: string): string {
  return `${PREFIX}${pipelineId}`
}

/** 读管道绑定；无记录/损坏返回 null（调用方回落会话级） */
export function loadPipelineBinding(pipelineId: string): PipelineExecutionBinding | null {
  try {
    const raw = localStorage.getItem(storageKey(pipelineId))
    if (!raw) return null
    const parsed = JSON.parse(raw) as { mode?: unknown }
    if (typeof parsed?.mode !== 'string' || !parsed.mode) return null
    return parsed as PipelineExecutionBinding
  } catch {
    return null
  }
}

/** 写管道绑定（pipelineId 缺席时生成——开管道前端尚未拿到回执的预写形态） */
export function savePipelineBinding(
  pipelineId: string | undefined,
  binding: PipelineExecutionBinding,
): string {
  const id = pipelineId || generateUUID()
  try {
    localStorage.setItem(storageKey(id), JSON.stringify(binding))
  } catch {
    throw new Error('本地存储不可用，管道绑定未能保存')
  }
  return id
}

/** 清管道绑定（子 Tab 关闭时） */
export function clearPipelineBinding(pipelineId: string): void {
  try {
    localStorage.removeItem(storageKey(pipelineId))
  } catch {
    // 清理失败不阻塞（残留键同 id 复用时被覆写）
  }
}
