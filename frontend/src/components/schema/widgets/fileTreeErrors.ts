/**
 * 文件树加载失败的错误文案工具：失败原因提取 + 错误态标题按数据源分域。
 */

/** 请求失败的可读原因：后端业务信封（error/detail）优先于 axios 概况消息
 *  （"Request failed with status code 404"）——归属闸等 404 的真实原因在
 *  响应体里，截断它会让用户只看到状态码概况。兜底文案不指名数据域
 *  （任务树/文件树由错误态标题按数据源分域），避免与标题张冠李戴。 */
export function extractRequestError(e: unknown): string {
  const data = (e as { response?: { data?: { error?: unknown; detail?: unknown } } } | null)
    ?.response?.data
  if (typeof data?.error === 'string' && data.error) return data.error
  if (typeof data?.detail === 'string' && data.detail) return data.detail
  return e instanceof Error ? e.message : '加载失败'
}

/** 错误态文案按数据源分域：加载对象是任务树（task:// 等域）还是工作区
 *  文件树（workspace://）——失败文案与实际加载对象一致，项目工作区打不开
 *  不得报「任务树加载失败」 */
export function loadErrorDomain(dataSource: string | undefined): string {
  return dataSource?.startsWith('workspace://') ? '文件树' : '任务树'
}
