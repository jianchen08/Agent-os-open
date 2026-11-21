// @feature: FP-0.2.可观测性 dsh_adapter DSH兼容四件垫片 | @vision: V3 可嵌入
/** L4 真实进程往返车道夹具：inject agentos 服务面的最小消费插件。
 * 按 exec.arguments.op 分派到 loop.status/stop/inject，把服务代理返回值并入
 * decision（probeStatus/probeStop/probeInject）——Python 侧从 emit_hook 的
 * decision 读回，值与内核 caller 桩预设一致即证明真实进程级往返成立。
 * 插件零特殊依赖（不 import 任何包），boot 后由 loadPlanPackages 之外的
 * packages 计划装载（本夹具经 bootPlan.packages 装载）。 */
export const name = 'agentos-roundtrip-consumer'
export const inject = ['agentos']

export function apply (scope) {
  scope.on('tools/post-execute', async (exec, result, next) => {
    const d = await next()
    const args = exec?.arguments ?? {}
    if (args.op === 'status') {
      return { ...d, probeStatus: await scope.agentos.loop.status(args.taskId) }
    }
    if (args.op === 'stop') {
      return { ...d, probeStop: await scope.agentos.loop.stop(args.taskId) }
    }
    if (args.op === 'inject') {
      return { ...d, probeInject: await scope.agentos.loop.inject(args.taskId, args.payload) }
    }
    return d
  })
}
