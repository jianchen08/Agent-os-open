/** 刷新后后台全量对账（缓存秒开 + 静默权威替换）。
 * 从 pipelineMessageStore 抽出：巨型文件只许缩小（frontend-any-baseline 大文件冻结）。
 * 循环引用安全：store 仅在函数体内取值（调用期已初始化）。 */
import { loggers } from '@/utils/logger'
import { usePipelineMessageStore } from './pipelineMessageStore'

const logger = loggers.sessionStore

/** 刷新后后台全量对账去重（同一管道只对账一次；流式结束前的对账推迟） */
const _reconcilingPipelines = new Set<string>()

/**
 * 刷新后后台静默全量对账（auto 首次进入且本地有 IndexedDB 缓存时，
 * 页面立即用缓存渲染（秒开），全量 API 对账放后台执行——initFromAPI 权威替换
 * 能修正刷新前流式断线留下的空洞/残影（增量补漏 after_sequence 拉不到已加载
 * 区间内的缺失消息）。对账无变化时对 UI 零影响。失败不再静默：缓存与 DB
 * 的分叉（空洞/残影）无人修正会永久留存——warn 后借既有 fetchMessages 全量
 * 重拉一次，重拉仍失败保留缓存现状待下次进入/WS 重连重试（fetchMessages
 * 自带失败日志）。流式进行中跳过——init 全量替换会清流式占位。
 */
export async function reconcileFromAPI(pipelineId: string, threadId: string): Promise<void> {
  if (_reconcilingPipelines.has(pipelineId)) return
  const store = usePipelineMessageStore.getState()
  if (store.isStreaming(pipelineId)) return
  _reconcilingPipelines.add(pipelineId)
  const reconcileOnce = async (): Promise<void> => {
    await store.fetchMessages(pipelineId, { threadId })
    usePipelineMessageStore.setState((s) => ({
      reconciledByPipeline: { ...s.reconciledByPipeline, [pipelineId]: true },
    }))
  }
  try {
    await reconcileOnce()
  } catch (error) {
    const err = error as { status?: number; code?: string; message?: string }
    const errInfo = err?.status ?? err?.code ?? err?.message ?? String(error)
    logger.warn(
      '[pipelineMessageStore.reconcileFromAPI] 全量对账失败，强制重拉一次: pipelineId=%s err=%s',
      pipelineId,
      errInfo,
    )
    try {
      await reconcileOnce()
    } catch {
      // 重拉仍失败：保留缓存现状，待下次进入/WS 重连重试（fetchMessages 已有失败日志）
    }
  } finally {
    _reconcilingPipelines.delete(pipelineId)
  }
}
