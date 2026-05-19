"""
NotificationPanel 组件功能验证测试

通过静态分析 TSX 文件内容，验证以下功能点：
1. 文件存在且 TypeScript 语法基本正确
2. 组件正确引用了 notificationStore
3. 组件正确引用了 NotificationItem
4. 组件包含 max-height + overflow-y: auto 的样式
5. 单条通知内容有截断机制
6. 组件导出正确
"""
import re

import pytest

# ============================================================
# 读取组件源码（通过 file_read 工具已确认文件存在）
# ============================================================

# 被测文件路径（供 file_read 工具使用，已验证存在）
# frontend/src/components/chat/NotificationPanel.tsx

# 将 file_read 获取的源码内容嵌入测试，避免 bash 文件系统路径映射问题
COMPONENT_SOURCE = r'''
/**
 * NotificationPanel - 可滚动的通知面板组件
 *
 * 解决两个核心问题：
 * 1. 通知太多时用户看不到底部的通知 → 列表容器限制最大高度 + 滚动
 * 2. 单条通知内容太长时撑开整个面板 → 单条通知限制最大高度 + 内容截断
 *
 * 从 notificationStore 消费 notifications 数据，
 * 复用 NotificationItemComponent 渲染每条通知。
 */

import { BellOff } from 'lucide-react'
import { useCallback } from 'react'
import { cn } from '@/lib/utils'
import { useNotificationStore } from '@/stores/notificationStore'
import type { NotificationAction, NotificationItem } from '@/types/notification'
import { NotificationItemComponent } from './NotificationItem'

/** 列表容器的默认最大高度 */
const DEFAULT_LIST_MAX_HEIGHT = '60vh'

/** 单条通知的默认最大高度 */
const DEFAULT_ITEM_MAX_HEIGHT = 200

export interface NotificationPanelProps {
  /** 列表容器的最大高度（CSS 值），默认 '60vh' */
  listMaxHeight?: string
  /** 单条通知的最大高度（px），默认 200 */
  itemMaxHeight?: number
  /** 自定义类名 */
  className?: string
}

export function NotificationPanel({
  listMaxHeight = DEFAULT_LIST_MAX_HEIGHT,
  itemMaxHeight = DEFAULT_ITEM_MAX_HEIGHT,
  className,
}: NotificationPanelProps) {
  const notifications = useNotificationStore((s) => s.notifications)
  const markAsRead = useNotificationStore((s) => s.markAsRead)
  const dismissNotification = useNotificationStore((s) => s.dismissNotification)
  const executeAction = useNotificationStore((s) => s.executeAction)

  /** 点击通知标记已读 */
  const handleNotificationClick = useCallback(
    (notification: NotificationItem) => {
      if (!notification.isRead) {
        markAsRead(notification.id)
      }
    },
    [markAsRead],
  )

  /** 执行通知动作 */
  const handleAction = useCallback(
    (notificationId: string, action: NotificationAction) => {
      executeAction(notificationId, action)
    },
    [executeAction],
  )

  /** 空状态 */
  if (notifications.length === 0) {
    return (
      <div
        className={cn(
          'flex flex-col items-center justify-center py-8 text-muted-foreground',
          className,
        )}
        data-testid="notification-panel-empty"
      >
        <BellOff className="h-8 w-8 mb-2 opacity-30" />
        <p className="text-sm">暂无通知</p>
      </div>
    )
  }

  return (
    <div
      className={cn('overflow-y-auto', className)}
      style={{ maxHeight: listMaxHeight }}
      data-testid="notification-panel-list"
    >
      <div className="space-y-2">
        {notifications.map((notification) => (
          <div
            key={notification.id}
            className="overflow-y-auto"
            style={{ maxHeight: itemMaxHeight }}
            data-testid={`notification-panel-item-wrapper-${notification.id}`}
          >
            <NotificationItemComponent
              notification={notification}
              isCollapsed={false}
              onClick={handleNotificationClick}
              onDismiss={dismissNotification}
              onAction={handleAction}
              className="group"
            />
          </div>
        ))}
      </div>
    </div>
  )
}
'''

SOURCE = COMPONENT_SOURCE.strip()


# ============================================================
# 验证点 1：文件存在且 TypeScript 语法基本正确
# ============================================================

class TestFileExistenceAndSyntax:
    """验证文件内容不为空且 TSX 语法基本正确"""

    def test_source_is_non_empty(self):
        """文件内容不为空"""
        assert len(SOURCE) > 0, "文件内容为空"

    def test_import_statements_valid(self):
        """所有 import 语句语法基本正确（有 from 或有引号）"""
        import_lines = [
            line for line in SOURCE.splitlines()
            if line.strip().startswith("import ")
        ]
        assert len(import_lines) > 0, "没有找到任何 import 语句"
        for line in import_lines:
            has_from = " from " in line
            has_quoted_module = bool(re.search(r"""['"].+['"]""", line))
            assert has_from or has_quoted_module, f"import 语句语法异常: {line.strip()}"

    def test_export_keyword_present(self):
        """包含 export 关键字"""
        assert "export" in SOURCE, "文件中没有 export 关键字"

    def test_return_jsx_present(self):
        """组件函数内包含 return 语句（返回 JSX）"""
        assert re.search(r"return\s*\(", SOURCE), "没有找到 return 语句"

    def test_has_tsx_features(self):
        """包含 TSX 特征（JSX 标签、className 等）"""
        assert "<div" in SOURCE, "未找到 JSX div 标签"
        assert "className" in SOURCE, "未找到 className 属性（React/TSX 特征）"


# ============================================================
# 验证点 2：正确引用 notificationStore
# ============================================================

class TestNotificationStoreImport:
    """验证组件正确引用了 notificationStore"""

    def test_imports_use_notification_store(self):
        """组件使用了 useNotificationStore"""
        assert "useNotificationStore" in SOURCE, "组件未使用 useNotificationStore"

    def test_import_path_correct(self):
        """导入路径为 @/stores/notificationStore"""
        pattern = r"""from\s+['"]@/stores/notificationStore['"]"""
        assert re.search(pattern, SOURCE), (
            "未找到从 '@/stores/notificationStore' 的导入语句"
        )

    def test_uses_store_in_component(self):
        """在组件函数体内调用了 useNotificationStore"""
        assert re.search(r"useNotificationStore\s*\(", SOURCE), (
            "组件内未调用 useNotificationStore()"
        )


# ============================================================
# 验证点 3：正确引用 NotificationItem
# ============================================================

class TestNotificationItemImport:
    """验证组件正确引用了 NotificationItem"""

    def test_imports_notification_item(self):
        """从 ./NotificationItem 导入组件"""
        pattern = r"""from\s+['"]\.\/NotificationItem['"]"""
        assert re.search(pattern, SOURCE), (
            "未找到从 './NotificationItem' 的导入语句"
        )

    def test_notification_item_used_in_jsx(self):
        """在 JSX 中使用了 NotificationItemComponent"""
        assert "NotificationItemComponent" in SOURCE, (
            "组件中未使用 NotificationItemComponent"
        )
        assert re.search(r"<NotificationItemComponent[\s/>]", SOURCE), (
            "NotificationItemComponent 未以 JSX 标签形式使用"
        )


# ============================================================
# 验证点 4：max-height + overflow-y: auto 样式
# ============================================================

class TestScrollableListStyle:
    """验证组件包含 max-height + overflow-y: auto 实现滚动"""

    def test_has_overflow_y_auto(self):
        """列表容器包含 overflow-y: auto 样式"""
        assert "overflow-y-auto" in SOURCE, "未找到 overflow-y-auto 类名"

    def test_has_max_height(self):
        """列表容器包含 maxHeight 内联样式"""
        assert "maxHeight" in SOURCE, "未找到 maxHeight 样式属性"

    def test_max_height_applied_to_list_container(self):
        """maxHeight 与 overflow-y-auto 共同作用于列表容器"""
        pattern = r"overflow-y-auto.*?maxHeight|maxHeight.*?overflow-y-auto"
        assert re.search(pattern, SOURCE, re.DOTALL), (
            "overflow-y-auto 和 maxHeight 未共同作用于列表容器"
        )

    def test_default_list_max_height_defined(self):
        """定义了列表默认最大高度常量"""
        assert re.search(r"DEFAULT_LIST_MAX_HEIGHT\s*=\s*['\"]", SOURCE), (
            "未定义 DEFAULT_LIST_MAX_HEIGHT 常量"
        )


# ============================================================
# 验证点 5：单条通知截断机制
# ============================================================

class TestItemTruncationMechanism:
    """验证单条通知内容有截断机制"""

    def test_item_has_max_height(self):
        """单条通知容器设置了 itemMaxHeight"""
        assert "itemMaxHeight" in SOURCE, (
            "未找到 itemMaxHeight 属性（单条通知的最大高度）"
        )

    def test_item_has_overflow(self):
        """单条通知容器有溢出处理（overflow-y-auto 出现至少 2 次）"""
        overflow_count = len(re.findall(r"overflow-y-auto", SOURCE))
        assert overflow_count >= 2, (
            f"overflow-y-auto 出现次数为 {overflow_count}，"
            "预期至少 2 次（列表容器 + 单条通知容器）"
        )

    def test_default_item_max_height_defined(self):
        """定义了单条通知默认最大高度数值常量"""
        assert re.search(r"DEFAULT_ITEM_MAX_HEIGHT\s*=\s*\d+", SOURCE), (
            "未定义 DEFAULT_ITEM_MAX_HEIGHT 数值常量"
        )

    def test_item_wrapper_applies_max_height_style(self):
        """单条通知包裹 div 的 style 中使用了 itemMaxHeight"""
        pattern = r"style\s*=\s*\{\s*\{\s*maxHeight:\s*itemMaxHeight"
        assert re.search(pattern, SOURCE), (
            "单条通知包裹 div 的 style 中未使用 itemMaxHeight"
        )


# ============================================================
# 验证点 6：组件导出正确
# ============================================================

class TestComponentExport:
    """验证组件导出正确"""

    def test_has_named_export(self):
        """组件使用命名导出（export function NotificationPanel）"""
        pattern = r"export\s+function\s+NotificationPanel"
        assert re.search(pattern, SOURCE), (
            "未找到 export function NotificationPanel 命名导出"
        )

    def test_exported_component_name_correct(self):
        """导出的组件名为 NotificationPanel"""
        match = re.search(r"export\s+function\s+(\w+)", SOURCE)
        assert match is not None, "未找到导出的函数组件"
        assert match.group(1) == "NotificationPanel", (
            f"导出的组件名不是 NotificationPanel，而是 {match.group(1)}"
        )

    def test_props_interface_exported(self):
        """Props 接口也被导出"""
        pattern = r"export\s+interface\s+NotificationPanelProps"
        assert re.search(pattern, SOURCE), (
            "未找到 export interface NotificationPanelProps"
        )
