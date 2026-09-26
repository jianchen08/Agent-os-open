/** @feature FP-T12 前端适配 | @ci: frontend-test */
/**
 * 管道编辑器组件测试家族共用的录制桩与工厂。
 * ops 录制桩记录 set/remove/insert/move 调用路径，供测试断言编辑器写入了哪个路径。
 */
import { vi } from 'vitest'
import type { PipelineEditorOps } from '@/services/pipeline/model'

export function makeOps() {
  const calls: Array<{ op: keyof PipelineEditorOps; args: unknown[] }> = []
  const ops: PipelineEditorOps = {
    set: vi.fn((...args: unknown[]) => calls.push({ op: 'set', args })),
    remove: vi.fn((...args: unknown[]) => calls.push({ op: 'remove', args })),
    insert: vi.fn((...args: unknown[]) => calls.push({ op: 'insert', args })),
    move: vi.fn((...args: unknown[]) => calls.push({ op: 'move', args })),
  }
  return { ops, calls }
}
