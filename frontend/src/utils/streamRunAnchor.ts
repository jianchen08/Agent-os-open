/**
 * run 级执行起点登记（2026-11-07 用户纠偏：重新生成后计时还是旧时间）。
 *
 * 「重新生成 / 回退重跑 / 编辑重发」都是内核截断到最后一条 user 消息后重跑——
 * 不产生新 user 消息，旧 user 消息时间戳不变，按「最近前置 user 消息」解析的
 * 流式计时锚点会停在原始发送时刻。本模块按管道登记 run 实际开始执行的时刻
 * （stream_start 时内核 未流式→流式 的跳变点，重跑即重跑那一刻），显示锚点
 * 取 user 消息时刻与 run 起点中较新者——重跑锚点归位，队列续发（run 未换）
 * 仍归位到各自触发消息。
 *
 * 运行时态：模块内存，刷新即失（回退「最近前置 user 消息」解析）；run 终止
 * （terminatePipeline）清除，上限外按插入序清扫。
 */

const runStartedAtByPipeline = new Map<string, number>()

/** 上限：条目数按管道计，超限删最早的 1/4（插入序≈登记序，活跃管道会被
 *  下一次 run 开始覆写，不会被误清成旧值——旧值被清只回落 user 消息锚点） */
const RUN_ANCHOR_CAP = 200

/** 登记 run 执行起点（每次 run 真开始覆盖写） */
export function noteRunStartedAt(pipelineId: string, at: number): void {
  runStartedAtByPipeline.set(pipelineId, at)
  if (runStartedAtByPipeline.size > RUN_ANCHOR_CAP) {
    const victims = Math.ceil(RUN_ANCHOR_CAP / 4)
    const keys = runStartedAtByPipeline.keys()
    for (let i = 0; i < victims; i++) {
      const next = keys.next()
      if (next.done) break
      runStartedAtByPipeline.delete(next.value)
    }
  }
}

/** run 终止清除（terminatePipeline 调用） */
export function clearRunStartedAt(pipelineId: string): void {
  runStartedAtByPipeline.delete(pipelineId)
}

/** 查 run 执行起点；无登记返回 undefined */
export function getRunStartedAt(pipelineId: string): number | undefined {
  return runStartedAtByPipeline.get(pipelineId)
}
