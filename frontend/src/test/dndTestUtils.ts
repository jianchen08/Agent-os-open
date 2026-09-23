/**
 * DnD 测试共用的 dataTransfer 桩（jsdom 未实现 DataTransfer 构造器）。
 * WorkspacePanel / AgentTabItem 的拖拽换位用例同源，避免逐字复制触发
 * jscpd 克隆门禁（min-tokens 50）。
 */

/** 构造可 set/get 的 dataTransfer 桩；seed 预置载荷（模拟跨窗口拖拽的现成数据） */
export function makeDataTransfer(seed: Record<string, string> = {}): DataTransfer {
  const data: Record<string, string> = { ...seed }
  return {
    effectAllowed: 'none',
    dropEffect: 'none',
    setData: (type: string, value: string) => {
      data[type] = value
    },
    getData: (type: string) => data[type] ?? '',
  } as unknown as DataTransfer
}
