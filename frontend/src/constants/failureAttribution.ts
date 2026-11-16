/**
 * 任务失败归因展示映射（P-21 任务失败归因透出）。
 *
 * 后端真值：tasks 插件在任务失败终态随写 ``task.failure_class``（分类词，
 * 现役词汇 run_stopped/run_aborted，来源 plugins/shared/tasks/reconcile.py
 * failure_attribution）与 ``task.failure_reason``（已译人话全文）。本文件只持
 * failure_class → 中文分类标签；failure_reason 是后端已译文本，消费面原样
 * 透传不二次映射。
 */

/** failure_class → 中文分类标签（与 reconcile._FAILURE_CLASS_* 两词对账） */
export const FAILURE_CLASS_LABELS: Record<string, string> = {
  // 有 run_stop_reason 署名的运行终止（详因在 failure_reason / 停止原因行）
  run_stopped: '运行终止',
  // 无署名的运行终止（进程异常终止/启动清扫等未记录形态）
  run_aborted: '原因未记录',
}

/**
 * failure_class → 展示文案：词表内「人话（原词）」，词表外原词透传
 * （插件/内核新增分类词不猜语义，与 stopReasonLabel 同口径）；空值回退空串。
 */
export function failureClassLabel(raw: string): string {
  if (!raw) return ''
  const described = FAILURE_CLASS_LABELS[raw]
  return described ? `${described}（${raw}）` : raw
}
