"""
检查点管理器

暴露接口：
- create_checkpoint(self, task_id: str, workspace: str, files_to_backup: list[str] | None) -> Checkpoint：create_checkpoint功能
- restore_checkpoint(self, task_id: str) -> bool：restore_checkpoint功能
- cleanup_checkpoint(self, task_id: str) -> bool：cleanup_checkpoint功能
- get_checkpoint(self, task_id: str) -> Checkpoint | None：get_checkpoint功能
- list_checkpoints(self) -> list[dict[str, Any]]：list_checkpoints功能
- CheckpointFile：CheckpointFile类
- Checkpoint：Checkpoint类
- CheckpointManager：CheckpointManager类
"""

import hashlib
import json
import logging
import shutil
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class CheckpointFile:
    """检查点文件记录

    记录单个文件的备份信息
    """

    original_path: str  # 原始文件相对路径
    backup_path: str  # 备份文件相对路径
    checksum: str  # 文件校验和（SHA256）
    size: int  # 文件大小（字节）
    modified_at: str  # 最后修改时间


@dataclass
class Checkpoint:
    """检查点

    记录任务执行前的文件状态，用于失败时回滚
    """

    task_id: str  # 任务 ID
    workspace: str  # 工作目录
    created_at: str  # 创建时间
    files: list[CheckpointFile] = field(default_factory=list)  # 备份文件列表
    status: str = "active"  # 状态: active | restored | cleaned


class CheckpointManager:
    """检查点管理器

    管理 HOST 模式下的文件检查点，提供：
    - 创建检查点：备份工作目录下的所有文件
    - 恢复检查点：从备份恢复文件
    - 清理检查点：删除备份文件

    使用场景：
    - 任务开始前创建检查点
    - 任务成功后清理检查点
    - 任务失败后从检查点恢复
    """

    CHECKPOINT_DIR = ".checkpoints"

    def __init__(self, project_root: str):
        """初始化检查点管理器"""
        self.project_root = Path(project_root).resolve()
        self.checkpoint_dir = self.project_root / self.CHECKPOINT_DIR

    def create_checkpoint(
        self,
        task_id: str,
        workspace: str,
        files_to_backup: list[str] | None = None,
    ) -> Checkpoint:
        """创建检查点"""
        checkpoint_path = self.checkpoint_dir / task_id
        backup_path = checkpoint_path / "files"

        # 创建备份目录
        backup_path.mkdir(parents=True, exist_ok=True)

        # 创建检查点对象
        checkpoint = Checkpoint(
            task_id=task_id,
            workspace=workspace,
            created_at=datetime.now(UTC).isoformat(),
        )

        workspace_path = self.project_root / workspace

        # 如果工作目录不存在，返回空检查点
        if not workspace_path.exists():
            logger.warning(f"[CheckpointManager] 工作目录不存在，跳过备份 | workspace={workspace}")
            self._save_manifest(checkpoint_path, checkpoint)
            return checkpoint

        # 如果没有指定文件列表，备份整个工作目录
        if files_to_backup is None:
            files_to_backup = []
            for file_path in workspace_path.rglob("*"):
                if file_path.is_file() and not self._should_ignore(file_path):
                    # 优先相对于 workspace 计算，因为 workspace 可能是
                    # project_root 的 worktree（兄弟目录而非子目录），
                    # 此时相对于 project_root 会抛 ValueError。
                    try:
                        rel = file_path.relative_to(workspace_path)
                    except ValueError:
                        try:
                            rel = file_path.relative_to(self.project_root)
                        except ValueError:
                            # 既不在 workspace 也不在 project_root 下，
                            # 用绝对路径兜底，避免中断整个备份流程
                            rel = file_path
                    files_to_backup.append(str(rel))

        # 备份文件
        for file_rel_path in files_to_backup:
            # 先尝试从 workspace 解析（worktree 场景），再回退 project_root
            original_file = workspace_path / file_rel_path
            if not original_file.exists():
                original_file = self.project_root / file_rel_path
            if not original_file.exists():
                continue

            try:
                # 计算校验和
                checksum = self._calculate_checksum(original_file)

                # 复制到备份目录
                backup_file = backup_path / file_rel_path
                backup_file.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(original_file, backup_file)

                # 记录文件信息
                checkpoint.files.append(
                    CheckpointFile(
                        original_path=file_rel_path,
                        backup_path=str(backup_file.relative_to(checkpoint_path)),
                        checksum=checksum,
                        size=original_file.stat().st_size,
                        modified_at=datetime.fromtimestamp(original_file.stat().st_mtime, UTC).isoformat(),
                    )
                )
            except Exception as e:
                logger.error(f"[CheckpointManager] 备份文件失败 | file={file_rel_path} | error={e}")

        # 保存清单
        self._save_manifest(checkpoint_path, checkpoint)

        logger.info(
            f"[CheckpointManager] 检查点已创建 | "
            f"task_id={task_id} | workspace={workspace} | files={len(checkpoint.files)}"
        )

        return checkpoint

    def restore_checkpoint(self, task_id: str) -> bool:
        """从检查点恢复"""
        checkpoint_path = self.checkpoint_dir / task_id
        manifest_path = checkpoint_path / "manifest.json"

        if not manifest_path.exists():
            logger.warning(f"[CheckpointManager] 检查点不存在 | task_id={task_id}")
            return False

        # 加载检查点
        checkpoint = self._load_manifest(manifest_path)

        # 恢复文件
        restored_count = 0
        for file_record in checkpoint.files:
            original_file = self.project_root / file_record.original_path
            backup_file = checkpoint_path / file_record.backup_path

            if backup_file.exists():
                try:
                    original_file.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(backup_file, original_file)
                    restored_count += 1
                except Exception as e:
                    logger.error(f"[CheckpointManager] 恢复文件失败 | file={file_record.original_path} | error={e}")

        # 更新状态
        checkpoint.status = "restored"
        self._save_manifest(checkpoint_path, checkpoint)

        logger.info(
            f"[CheckpointManager] 检查点已恢复 | task_id={task_id} | restored={restored_count}/{len(checkpoint.files)}"
        )

        return True

    def cleanup_checkpoint(self, task_id: str) -> bool:
        """清理检查点"""
        checkpoint_path = self.checkpoint_dir / task_id

        if not checkpoint_path.exists():
            logger.warning(f"[CheckpointManager] 检查点不存在，无需清理 | task_id={task_id}")
            return True

        try:
            shutil.rmtree(checkpoint_path)
            logger.info(f"[CheckpointManager] 检查点已清理 | task_id={task_id}")
            return True
        except Exception as e:
            logger.error(f"[CheckpointManager] 清理检查点失败 | task_id={task_id} | error={e}")
            return False

    def get_checkpoint(self, task_id: str) -> Checkpoint | None:
        """获取检查点信息"""
        manifest_path = self.checkpoint_dir / task_id / "manifest.json"

        if not manifest_path.exists():
            return None

        return self._load_manifest(manifest_path)

    def list_checkpoints(self) -> list[dict[str, Any]]:
        """列出所有检查点"""
        checkpoints = []

        if not self.checkpoint_dir.exists():
            return checkpoints

        for task_dir in self.checkpoint_dir.iterdir():
            if task_dir.is_dir():
                manifest_path = task_dir / "manifest.json"
                if manifest_path.exists():
                    checkpoint = self._load_manifest(manifest_path)
                    checkpoints.append(
                        {
                            "task_id": checkpoint.task_id,
                            "workspace": checkpoint.workspace,
                            "created_at": checkpoint.created_at,
                            "status": checkpoint.status,
                            "file_count": len(checkpoint.files),
                        }
                    )

        return checkpoints

    def _should_ignore(self, file_path: Path) -> bool:
        """判断文件是否应该被忽略"""
        # 忽略隐藏文件
        if file_path.name.startswith("."):
            return True

        # 忽略 __pycache__ 目录
        if "__pycache__" in file_path.parts:
            return True

        # 忽略 node_modules 目录
        if "node_modules" in file_path.parts:
            return True

        # 忽略 .git 目录
        if ".git" in file_path.parts:
            return True

        # 忽略检查点目录
        return self.CHECKPOINT_DIR in file_path.parts

    def _calculate_checksum(self, file_path: Path) -> str:
        """计算文件校验和"""
        return hashlib.sha256(file_path.read_bytes()).hexdigest()

    def _save_manifest(self, checkpoint_path: Path, checkpoint: Checkpoint) -> None:
        """保存检查点清单"""
        manifest_path = checkpoint_path / "manifest.json"
        manifest_path.write_text(
            json.dumps(self._checkpoint_to_dict(checkpoint), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    def _load_manifest(self, manifest_path: Path) -> Checkpoint:
        """加载检查点清单"""
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        return Checkpoint(
            task_id=data["task_id"],
            workspace=data["workspace"],
            created_at=data["created_at"],
            status=data.get("status", "active"),
            files=[
                CheckpointFile(
                    original_path=f["original_path"],
                    backup_path=f["backup_path"],
                    checksum=f["checksum"],
                    size=f["size"],
                    modified_at=f["modified_at"],
                )
                for f in data.get("files", [])
            ],
        )

    def _checkpoint_to_dict(self, checkpoint: Checkpoint) -> dict[str, Any]:
        """检查点转字典"""
        return {
            "task_id": checkpoint.task_id,
            "workspace": checkpoint.workspace,
            "created_at": checkpoint.created_at,
            "status": checkpoint.status,
            "files": [
                {
                    "original_path": f.original_path,
                    "backup_path": f.backup_path,
                    "checksum": f.checksum,
                    "size": f.size,
                    "modified_at": f.modified_at,
                }
                for f in checkpoint.files
            ],
        }
