/**
 * 在系统文件管理器/IDE 连接器中打开工作区目录（workspaces open 端点）
 *
 * 三种结果都必须有着落——静默会让用户以为已打开：业务级失败（200 信封
 * success:false + message，目录缺失/无连接器等）如实透传；成功回显后端
 * message（文件管理器窗口可能被遮挡/未弹，远程会话等）；传输层失败给
 * 重试提示。
 */

import apiClient from '@/services/api/client'
import { WORKSPACE_SERVICE_ENDPOINTS } from '@/services/api/endpoints.generated'
import { useNotificationStore } from '@/stores/notificationStore'

/**
 * 打开 containerId 对应目录并通知结果。
 *
 * @param containerId 工作区/项目登记 id（端点路径参数）
 * @param noun 目录称呼（兜底文案用，如「工作区目录」「文件夹」）
 * @param name 项目名（项目行场景；缺省=工作区场景，文案不带项目段）
 */
export async function openFolderWithFeedback(
  containerId: string,
  noun: string,
  name?: string,
): Promise<void> {
  try {
    const resp = await apiClient.post(
      WORKSPACE_SERVICE_ENDPOINTS.workspaces_open.replace('{container_task_id}', containerId),
    )
    const data = resp?.data as { success?: boolean; message?: string } | undefined
    if (data && data.success === false) {
      useNotificationStore.getState().addNotification({
        title: '打开文件夹失败',
        message: data.message || `后端未能打开${name ? `项目${noun}` : noun}`,
        priority: 'normal',
        category: 'alert',
        isBlocking: false,
        autoDismissMs: 6000,
        sourceLabel: '前端',
      })
    } else {
      useNotificationStore.getState().addNotification({
        title: '已打开文件夹',
        message: data?.message || `${name ? `项目「${name}」${noun}` : noun}已在文件管理器中打开`,
        priority: 'normal',
        category: 'info',
        isBlocking: false,
        autoDismissMs: 6000,
        sourceLabel: '前端',
      })
    }
  } catch (e) {
    console.error('[workspaceFolderOpener] 打开文件夹请求失败', e)
    useNotificationStore.getState().addNotification({
      title: '打开文件夹失败',
      message: `${name ? `项目 ${name} ` : noun}打开失败，请稍后重试`,
      priority: 'normal',
      category: 'alert',
      isBlocking: false,
      autoDismissMs: 6000,
      sourceLabel: '前端',
    })
  }
}
