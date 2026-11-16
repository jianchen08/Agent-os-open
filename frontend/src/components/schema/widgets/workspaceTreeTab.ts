import { useLayoutModeStore } from '@/stores/layoutModeStore'

/** 打开工作空间文件树标签（0.1 任务树节点同款：workspace://<taskId> 数据源，
 *  标签内 FiveSpaceLayout 另有"打开文件夹"按钮调后端 explorer.exe 打开目录）。
 *  FileTreeWidget 与 PipelineManagerWidget 共用入口——已存在则激活，否则创建。
 *  ownerSessionId：会话派生页签的归属会话（工作区「只看当前会话」过滤键，
 *  缺省 = 全局页签任何会话下可见）。 */
export function openWorkspaceTreeTab(nodeId: string, title: string, ownerSessionId?: string): void {
  const layoutStore = useLayoutModeStore.getState()
  const tabId = `ws-tree-${nodeId}`
  const existingTab = layoutStore.workspaceTabs.find((t) => t.id === tabId)
  if (existingTab) {
    layoutStore.setActiveTab(tabId)
    return
  }
  layoutStore.addWorkspaceTab({
    id: tabId,
    title: title || '工作空间',
    icon: '📁',
    moduleId: '__dynamic__',
    component: 'file_tree',
    dataSource: `workspace://${nodeId}`,
    ownerSessionId,
    isActive: true,
    isPinned: false,
  })
}
