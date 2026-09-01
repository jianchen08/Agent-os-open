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
import re
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

    _TASK_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")

    def _validate_task_id(self, task_id: str) -> None:
        """F-ISO-1: task_id 白名单——仅字母/数字/-/_，防穿越/分隔符/绝对路径。"""
        if not task_id or not self._TASK_ID_PATTERN.fullmatch(task_id):
            raise ValueError(f"非法 task_id（仅允许字母/数字/-/_）: {task_id!r}")

    def _validate_relative_path(self, path: str) -> None:
        """F-ISO-1: 相对路径校验——拒绝绝对路径与含 ``..`` 段，抛 ValueError。

        双平台语义补位：``Path.is_absolute`` 依赖当前 OS 的解析语义——
        Windows 对 ``/etc/passwd``（无盘符 Unix 绝对路径）返回 False，而
        Linux 对 ``C:\\windows\\...``（无前导分隔符）返回 True 但 parts 不同——
        均可能漏判。故按「主机语义 + 字面量规则」双向判定：
        - 前导 ``/``/``\\``：任一平台的 Unix 绝对路径；
        - 盘符前缀（``X:`` 开头）：Windows 绝对路径字面量（在 Linux 上
          ``PureWindowsPath.is_absolute`` 语义才为 True）；
        - ``..`` 段：用 PurePosixPath 切（Linux 侧 ``Path`` 对 ``C:\\windows``
          会把反斜杠当文件名字符，parts 检测须双语义兜底）。
        """
        if not path:
            return
        if path.lstrip().startswith(("/", "\\")):
            raise ValueError(f"非法路径（绝对路径或含穿越段不允许）: {path!r}")
        p = Path(path)
        if p.is_absolute() or ".." in p.parts:
            raise ValueError(f"非法路径（绝对路径或含穿越段不允许）: {path!r}")
        # Linux 上 Windows 盘符路径（C:\...）不构成 is_absolute——按字面
        # 盘符规则补判（跨平台字面量：盘符冒号 + 分隔符出现即视为 Windows 路径）。
        if len(path) >= 2 and path[0].isalpha() and path[1] == ":":
            raise ValueError(f"非法路径（绝对路径或含穿越段不允许）: {path!r}")

    @staticmethod
    def _is_safe_relative_path(path: str) -> bool:
        """F-ISO-1: 相对路径安全检查（返回 bool，供 restore 的 manifest 校验）。

        与 _validate_relative_path 同款双平台字面量规则（Linux 上
        ``Path('C:\\\\windows').is_absolute()`` 为 False 的漏判由盘符前缀
        检查兜底）。
        """
        if not path:
            return True
        p = Path(path)
        if p.is_absolute() or path.lstrip().startswith(("/", "\\")) or ".." in p.parts:
            return False
        # Windows 盘符字面量（Linux 侧解析为普通目录名）
        return not (len(path) >= 2 and path[1] == ":" and path[0].isalpha())

    @staticmethod
    def _rel_or_none(file_path: Path, base: Path) -> str | None:
        """文件相对 base 的路径字符串；不在 base 下返回 None。"""
        try:
            return str(file_path.relative_to(base))
        except ValueError:
            return None

    def _collect_workspace_files(self, workspace_path: Path) -> list[str]:
        """枚举工作目录下应备份的文件（rglob 全量，跳过 ignore 规则）。

        相对路径优先相对于 workspace 计算——workspace 可能是 project_root 的
        worktree（兄弟目录而非子目录）；两者皆不在时用绝对路径兜底，不中断
        备份流程。
        """
        collected: list[str] = []
        for file_path in workspace_path.rglob("*"):
            if not (file_path.is_file() and not self._should_ignore(file_path)):
                continue
            rel = self._rel_or_none(file_path, workspace_path)
            if rel is None:
                rel = self._rel_or_none(file_path, self.project_root)
            collected.append(rel if rel is not None else str(file_path))
        return collected

    def _backup_one_file(
        self,
        checkpoint_path: Path,
        backup_path: Path,
        workspace_path: Path,
        file_rel_path: str,
    ) -> CheckpointFile | None:
        """备份单个文件（workspace 解析优先、project_root 回退），失败返回 None 留痕。"""
        # 先尝试从 workspace 解析（worktree 场景），再回退 project_root
        original_file = workspace_path / file_rel_path
        if not original_file.exists():
            original_file = self.project_root / file_rel_path
        if not original_file.exists():
            return None

        try:
            checksum = self._calculate_checksum(original_file)

            backup_file = backup_path / file_rel_path
            backup_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(original_file, backup_file)

            return CheckpointFile(
                original_path=file_rel_path,
                backup_path=str(backup_file.relative_to(checkpoint_path)),
                checksum=checksum,
                size=original_file.stat().st_size,
                modified_at=datetime.fromtimestamp(original_file.stat().st_mtime, UTC).isoformat(),
            )
        except Exception as e:
            logger.error(f"[CheckpointManager] 备份文件失败 | file={file_rel_path} | error={e}")
            return None

    def create_checkpoint(
        self,
        task_id: str,
        workspace: str,
        files_to_backup: list[str] | None = None,
    ) -> Checkpoint:
        """创建检查点"""
        # F-ISO-1: task_id 白名单 + files_to_backup 路径校验（外部不可信输入）
        self._validate_task_id(task_id)
        if files_to_backup is not None:
            for _frp in files_to_backup:
                self._validate_relative_path(_frp)
        checkpoint_path = self.checkpoint_dir / task_id
        backup_path = checkpoint_path / "files"

        backup_path.mkdir(parents=True, exist_ok=True)

        checkpoint = Checkpoint(
            task_id=task_id,
            workspace=workspace,
            created_at=datetime.now(UTC).isoformat(),
        )

        workspace_path = self.project_root / workspace

        if not workspace_path.exists():
            logger.warning(f"[CheckpointManager] 工作目录不存在，跳过备份 | workspace={workspace}")
            self._save_manifest(checkpoint_path, checkpoint)
            return checkpoint

        # 没有指定文件列表 → 备份整个工作目录
        targets = files_to_backup if files_to_backup is not None else self._collect_workspace_files(workspace_path)

        for file_rel_path in targets:
            record = self._backup_one_file(checkpoint_path, backup_path, workspace_path, file_rel_path)
            if record is not None:
                checkpoint.files.append(record)

        self._save_manifest(checkpoint_path, checkpoint)

        logger.info(
            f"[CheckpointManager] 检查点已创建 | "
            f"task_id={task_id} | workspace={workspace} | files={len(checkpoint.files)}"
        )

        return checkpoint

    def restore_checkpoint(self, task_id: str) -> bool:
        """从检查点恢复"""
        self._validate_task_id(task_id)
        checkpoint_path = self.checkpoint_dir / task_id
        manifest_path = checkpoint_path / "manifest.json"

        if not manifest_path.exists():
            logger.warning(f"[CheckpointManager] 检查点不存在 | task_id={task_id}")
            return False

        checkpoint = self._load_manifest(manifest_path)

        # F-ISO-1: manifest 篡改防护——任一 original_path/backup_path 越界即整体拒绝，零落盘
        for file_record in checkpoint.files:
            if not self._is_safe_relative_path(file_record.original_path):
                logger.error(
                    f"[CheckpointManager] manifest 含越界 original_path，拒绝恢复 | {file_record.original_path}"
                )
                return False
            if not self._is_safe_relative_path(file_record.backup_path):
                logger.error(
                    f"[CheckpointManager] manifest 含越界 backup_path，拒绝恢复 | {file_record.backup_path}"
                )
                return False

        # 恢复文件（目标路径语义对齐 create：先 workspace 下解析，再回退 project_root）
        workspace_path = self.project_root / checkpoint.workspace
        restored_count = 0
        for file_record in checkpoint.files:
            original_file = workspace_path / file_record.original_path
            if not original_file.parent.exists():
                original_file = self.project_root / file_record.original_path
            backup_file = checkpoint_path / file_record.backup_path

            if backup_file.exists():
                try:
                    original_file.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(backup_file, original_file)
                    restored_count += 1
                except Exception as e:
                    logger.error(f"[CheckpointManager] 恢复文件失败 | file={file_record.original_path} | error={e}")

        checkpoint.status = "restored"
        self._save_manifest(checkpoint_path, checkpoint)

        logger.info(
            f"[CheckpointManager] 检查点已恢复 | task_id={task_id} | restored={restored_count}/{len(checkpoint.files)}"
        )

        return True

    def cleanup_checkpoint(self, task_id: str) -> bool:
        """清理检查点"""
        self._validate_task_id(task_id)
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
        self._validate_task_id(task_id)
        manifest_path = self.checkpoint_dir / task_id / "manifest.json"

        if not manifest_path.exists():
            return None

        return self._load_manifest(manifest_path)

    def list_checkpoints(self) -> list[dict[str, Any]]:
        """列出所有检查点"""
        checkpoints: list[dict[str, Any]] = []

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
        if file_path.name.startswith("."):
            return True

        if "__pycache__" in file_path.parts:
            return True

        if "node_modules" in file_path.parts:
            return True

        if ".git" in file_path.parts:
            return True

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
