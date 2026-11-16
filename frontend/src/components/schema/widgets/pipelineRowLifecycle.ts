/**
 * 管道行视图模型的生命周期/归因五键共享映射。
 *
 * PipelineManagerWidget 三处行构建块（runs 槽位行 / 任务派生行 / state 独有
 * 行）的同构六键（stateEnded/rawError/stopReason/failureClass/failureReason/
 * suspendedReason）单点防漂移；冻结件行数清偿的拆分位（>1000 行文件冻结，
 * 只许缩小）。输入宽松（可空）：三处调用点的 st 形态不一（槽位行可为 undefined）。
 *
 * 泛型保窄：E/F/G 等由实参推断（如 failureClass 的字面量联合），返回类型
 * 不宽化为 string——行视图模型（PipelineViewEntry）按窄类型收敛，宽化即
 * tsc -b 红（独立 CI 轮 5 实锤）。
 */
export function pipelineRowLifecycleFields<
  E extends boolean | undefined,
  R extends string | null | undefined,
  S extends string | null | undefined,
  F extends string | null | undefined,
  G extends string | null | undefined,
  U extends string | null | undefined,
>(st: {
  ended?: E
  rawError?: R
  stopReason?: S
  failureClass?: F
  failureReason?: G
  suspendedReason?: U
} | undefined | null) {
  return {
    stateEnded: st?.ended,
    rawError: st?.rawError,
    stopReason: st?.stopReason,
    failureClass: st?.failureClass,
    failureReason: st?.failureReason,
    // 挂起归因（run 域 suspended_reason；缺省不渲染挂起原因行——P-23 片2）
    suspendedReason: st?.suspendedReason,
  }
}
