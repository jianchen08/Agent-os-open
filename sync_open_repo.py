#!/usr/bin/env python3
# ============================================================
# sync_open_repo.py — 从源仓库同步代码到开源仓库(保留历史)
# ============================================================
#
# 作用:
#   1. 只读源仓库(绝不修改源仓库任何文件)
#   2. 从源仓库当前快照筛出文件,剔除过程文档,只保留开源相关内容
#   3. 用筛选后的文件覆盖开源本地仓库(agent-os-open)工作区
#   4. 在 main 分支上创建增量提交(保留外部 PR 的历史)
#   5. 推送到 GitHub + Gitee 两个远端(非强推,远端有新提交时提示人工 rebase)
#
# 同步策略(保留历史,支持外部 PR):
#   - 增量提交+普通推送,外部贡献者的 commit 永久保留。
#   - 若远端有外部 PR 合入的新提交(non-fast-forward),推送会失败,
#     需人工 `git pull --rebase` 后再推,避免自动覆盖贡献。
#
# 保留的文档(开源面向用户):
#   - docs/ARCHITECTURE.md      架构文档
#   - docs/vision.md            项目愿景
#   - docs/guides/              使用指南目录
#
# 剔除的内容(过程/内部文档,不开源):
#   - docs/working/             整个过程区(含 private 专利/竞品/简历/设计草稿/评审报告等)
#   - docs/tasks/               迁移任务拆解
#   - docs/0.2架构迁移_checkpoints.md
#   - docs/agent_prompts_viewer.html
#   - docs/ 下其余非保留项
#   - sync_open_repo.py         本脚本自身(工具不入库)
#   - *.db / *.db-*             运行时数据库(含未跟踪备份)
#   - data/ logs/ .zcode/ dsh_plugins/ .ai_workspaces/   运行时/本地目录
#   - agent-research/           第三方仓库研究数据导出(含外部作者邮箱)
#   - outputs/                  调试/运行产物
#   - .project/                 过程设计文档
#   - 根目录调试残留与运行时标记(resp*.json/_procs*.json/.kernel_pid*/.ports_02 等)
#
# 用法:
#   python sync_open_repo.py --dst D:\path\to\agent-os-open
#   python sync_open_repo.py --dst D:\path\to\agent-os-open --no-push   # 只同步不推送(先检查)
#   python sync_open_repo.py --dst D:\path\to\agent-os-open --dry-run   # 只打印将做什么,不实际执行
#
# 路径来源(不再硬编码本机绝对路径):
#   --src   源仓库路径;缺省用环境变量 SYNC_SRC_REPO,再缺省用脚本所在目录
#   --dst   开源仓库路径;必须由 --dst 或环境变量 SYNC_DST_REPO 提供
#
# ============================================================

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

REMOTES = {
    "origin (GitHub)": "origin",
    "gitee": "gitee",
}
COMMIT_AUTHOR_NAME = "Agent OS"
COMMIT_AUTHOR_EMAIL = "agent@local"


def run(cmd, cwd=None, check=True, capture=False):
    """执行命令,返回结果。"""
    r = subprocess.run(cmd, cwd=cwd, capture_output=capture, text=True, encoding="utf-8", errors="replace", check=False)
    if check and r.returncode != 0:
        print(f"  ✗ 命令失败: {' '.join(cmd)}")
        if capture:
            if r.stdout:
                print(r.stdout[-500:])
            if r.stderr:
                print(r.stderr[-500:])
        sys.exit(1)
    return r


def get_source_files(src_repo):
    """从源仓库获取文件清单(已跟踪 + 未忽略的未跟踪文件),用 git -z 规避中文转义。"""
    tracked = (
        subprocess.check_output(["git", "ls-files", "-z"], cwd=src_repo).decode("utf-8", "surrogateescape").split("\0")
    )
    others = (
        subprocess.check_output(["git", "ls-files", "-z", "--others", "--exclude-standard"], cwd=src_repo)
        .decode("utf-8", "surrogateescape")
        .split("\0")
    )
    allf = sorted({t for t in tracked + others if t})
    return allf


# 公开仓 CI 门禁依赖的数据文件（与全史发布脚本 opensource_publish_history_20260902.py
# 的 _ALLOWLIST 同源）：scripts/check_test_traceability.py 硬编码读取这两个路径，
# 缺失即 CI 红门禁 FileNotFoundError（2026-09-04 65c9d54f sync 曾回归删除，实证实锤）。
# 此处不放行，clear_dst+重建的同步方式每轮都会把它们删掉。
_SYNC_ALLOWLIST = {
    "docs/working/test_traceability.md",
    "reports/audit_round3/T5_tests.md",
}


def is_excluded(path):
    """判断一个文件是否应从开源仓库中剔除。"""
    # CI 门禁数据文件优先于目录级剔除规则
    if path in _SYNC_ALLOWLIST:
        return False
    # 过程文档:整个 working 区(含 private 专利/竞品/简历等)
    if path.startswith("docs/working/"):
        return True
    # 迁移任务拆解
    if path.startswith("docs/tasks/"):
        return True
    # 内部产物
    if path == "docs/0.2架构迁移_checkpoints.md":
        return True
    if path == "docs/agent_prompts_viewer.html":
        return True
    # docs/ 下:只保留 ARCHITECTURE.md / vision.md / guides/
    if path.startswith("docs/"):
        rel = path[5:]  # 去掉 'docs/'
        if rel == "ARCHITECTURE.md":
            return False
        if rel == "vision.md":
            return False
        if rel.startswith("guides/"):
            return False
        # 其余 docs/ 下的一律剔除(.gitignore 等)
        return True
    # 跳过本脚本自身(工具不入库)
    if path == "sync_open_repo.py":
        return True
    if path.startswith(".git/"):
        return True
    # 一次性内部 BUG-FIX / 数据修复脚本(含内部 BUG 单号与事故细节,不入开源)
    if path in ():
        return True
    # 运行时数据库(含源文件清单里的未跟踪 *.db / *.db-* 备份——数据不外泄)
    if re.search(r"\.db(-|$)", path):
        return True
    # 运行时产物目录(gitignore 已挡跟踪面;此处挡"未忽略的未跟踪"漏网)
    if path.startswith(("data/", "logs/", ".zcode/", "dsh_plugins/", ".ai_workspaces/")):
        return True
    # 第三方仓库研究数据导出(含外部作者邮箱等他人数据)
    if path.startswith("agent-research/"):
        return True
    # 调试/运行产物(诊断脚本、临时项目目录)
    if path.startswith("outputs/"):
        return True
    # 过程设计文档(.gitignore 注释口径=本地工作区)
    if path.startswith(".project/"):
        return True
    # 内部 QA 审计产物(traceability 门禁源数据,源仓保留)
    if path.startswith("reports/"):
        return True
    # 根目录散落的调试残留(历史误提交,不入开源)
    if path in (
        "resp.json",
        "resp2.json",
        "test_workspace_named.txt",
        "_procs.json",
        "_procs2.json",
    ):
        return True
    if path.startswith(".e2e_child/") or path == ".e2e_child":
        return True
    if path.startswith(".e2e_final_thread/") or path == ".e2e_final_thread":
        return True
    # 运行时标记文件(内核 pid / 端口探测)
    if path in (".kernel_pid", ".kernel_pid_now", ".ports_02"):
        return True
    return False


def clear_dst(dst_repo):
    """清空目标目录(保留 .git)。"""
    for full in Path(dst_repo).iterdir():
        if full.name == ".git":
            continue
        if full.is_dir():
            shutil.rmtree(full)
        else:
            full.unlink()


def sanitize_for_open_source(rel, content):
    """开源化处理:把源仓库的个人配置替换为通用值。

    源仓库是维护者正在使用的项目,含个人路径/个人默认模型等配置;
    开源仓库应给陌生用户开箱即用的通用配置。这里对特定文件做内容替换。
    返回处理后的内容(bytes)。无法处理的文件原样返回。
    """
    try:
        text = content.decode("utf-8")
    except Exception:
        return content

    changed = False

    # config/isolation/isolation_config.yaml:
    #   workspace.root 的绝对本机路径(D:/xxx / C:/xxx / /home/xxx)→ 相对路径 .ai_workspaces
    if rel == "config/isolation/isolation_config.yaml":
        import re

        new = re.sub(
            r"((?:\r?\n|^)\s*root:\s*)[A-Za-z]:[\\/][^\r\n]*",
            r"\1.ai_workspaces",
            text,
        )
        if new != text:
            text = new
            changed = True

    # config/models/llm.yaml:
    #   defaults.chat 的个人默认模型 → glm-5.2(开源示例默认,用户按 .env.example 配 ZHIPU_API_KEY 即可用)
    #   （chat 与 defaults 之间可能有 call_timeout 等兄弟键，惰性跨键匹配）
    if rel == "config/models/llm.yaml":
        import re

        new = re.sub(
            r"(defaults:.*?\r?\n\s*chat:\s*)[^\r\n]+",
            r"\1glm-5.2",
            text,
            count=1,
            flags=re.DOTALL,
        )
        if new != text:
            text = new
            changed = True

    if changed:
        print(f"  [开源化] {rel}: 已替换个人配置为通用值")
    return text.encode("utf-8")


def copy_files(src_repo, dst_repo, filelist):
    """把文件列表从源复制到目标。返回(已复制数, 跳过数)。

    对特定配置文件会调用 sanitize_for_open_source 做开源化处理。
    """
    copied = 0
    skipped = 0
    for rel in filelist:
        s = os.path.join(src_repo, rel)
        d = os.path.join(dst_repo, rel)
        if not os.path.isfile(s):
            skipped += 1
            continue
        os.makedirs(os.path.dirname(d), exist_ok=True)
        with open(s, "rb") as f:
            content = f.read()
        content = sanitize_for_open_source(rel, content)
        with open(d, "wb") as f:
            f.write(content)
        copied += 1
    return copied, skipped


def create_sync_commit(dst_repo, src_head):
    """在开源仓库当前分支(main)上创建增量提交,保留完整历史。

    与旧版 create_orphan_commit 的区别:
      - 旧版:每次 --orphan 重建历史 + --force 推送,会抹掉外部 PR 的 commit
      - 新版:在现有 main 上正常 add + commit,保留外部贡献者的提交历史
    若工作区无变化则跳过提交(避免空提交污染历史)。

    提交信息只带源 HEAD 短 SHA（一行式）：源提交信息可能含内部审计/风险叙述，
    原文带进公开仓等于对外发布内部漏洞清单（2026-09-04 面试审查结论）。
    """
    run(["git", "checkout", "main"], cwd=dst_repo, check=False)
    run(["git", "add", "-A"], cwd=dst_repo)
    env = dict(
        os.environ,
        GIT_AUTHOR_NAME=COMMIT_AUTHOR_NAME,
        GIT_AUTHOR_EMAIL=COMMIT_AUTHOR_EMAIL,
        GIT_COMMITTER_NAME=COMMIT_AUTHOR_NAME,
        GIT_COMMITTER_EMAIL=COMMIT_AUTHOR_EMAIL,
    )
    # 无变化则不提交
    status = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=dst_repo, env=env, check=False)
    if status.returncode == 0:
        print("  工作区无变化,跳过提交")
        return False
    commit_msg = f"sync: 从源仓库同步 ({src_head})"
    subprocess.run(["git", "commit", "-m", commit_msg], cwd=dst_repo, env=env, capture_output=True, check=False)
    return True


def push(dst_repo, dry_run=False):
    """推送开源仓库 main 到所有远端(保留历史,非强推)。

    与旧版 force_push 的区别:
      - 旧版:--force 覆盖远端,会抹掉外部 PR 的 commit
      - 新版:普通 push;若远端有外部 PR 合入的新提交(non-fast-forward),
        会推送失败并提示先 rebase/pull,由人工介入(避免自动覆盖贡献)
    """
    results = {}
    for label, remote_name in REMOTES.items():
        if dry_run:
            print(f"  [dry-run] 将推送到 {label} ({remote_name})")
            results[label] = True
            continue
        r = subprocess.run(
            ["git", "push", remote_name, "main"],
            cwd=dst_repo,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        ok = r.returncode == 0
        results[label] = ok
        if ok:
            print(f"  ✓ {label} 推送成功")
        else:
            print(f"  ✗ {label} 推送失败(可能远端有外部 PR 新提交)")
            print(
                f"    请手动处理: cd {dst_repo} && git pull --rebase {remote_name} main && git push {remote_name} main"
            )
            if r.stderr:
                print(f"    {r.stderr[-400:]}")
    return results


def main():
    parser = argparse.ArgumentParser(description="同步源仓库到开源仓库(保留历史)")
    parser.add_argument(
        "--src",
        default=os.environ.get("SYNC_SRC_REPO") or os.path.dirname(os.path.abspath(__file__)),
        help="源仓库路径(默认: 环境变量 SYNC_SRC_REPO, 再缺省为脚本所在目录)",
    )
    parser.add_argument(
        "--dst",
        default=os.environ.get("SYNC_DST_REPO"),
        help="开源仓库路径(必填: --dst 或环境变量 SYNC_DST_REPO)",
    )
    parser.add_argument("--no-push", action="store_true", help="只同步本地不推送远端")
    parser.add_argument("--dry-run", action="store_true", help="只打印将做什么,不实际执行")
    args = parser.parse_args()

    src_repo = os.path.abspath(args.src)
    dst_repo = os.path.abspath(args.dst) if args.dst else ""
    if not dst_repo:
        parser.error("缺少开源仓库路径: 请用 --dst 指定, 或设置环境变量 SYNC_DST_REPO")

    print("=" * 60)
    print("  开源仓库同步工具")
    print("=" * 60)
    print(f"  源仓库: {src_repo}")
    print(f"  目标:   {dst_repo}")
    print(f"  远端:   {', '.join(REMOTES.keys())}")
    print("=" * 60)

    # 0. 检查（.git 为文件 = git worktree 检出，同样可作同步源）
    if not (os.path.isdir(os.path.join(src_repo, ".git")) or os.path.isfile(os.path.join(src_repo, ".git"))):
        print(f"✗ 源仓库不存在或不是 git 仓库: {src_repo}")
        sys.exit(1)
    if not os.path.isdir(os.path.join(dst_repo, ".git")):
        print(f"✗ 目标仓库不存在或不是 git 仓库: {dst_repo}")
        sys.exit(1)

    # 1. 源仓库 HEAD
    head = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], cwd=src_repo, text=True).strip()
    head_msg = subprocess.check_output(["git", "log", "-1", "--format=%s"], cwd=src_repo, text=True).strip()
    print(f"\n[1/5] 源仓库 HEAD: {head} {head_msg}")

    # 2. 生成文件清单
    print("\n[2/5] 生成文件清单...")
    allf = get_source_files(src_repo)
    final = [p for p in allf if not is_excluded(p)]
    excluded = [p for p in allf if is_excluded(p)]
    print(f"  源仓库总文件: {len(allf)}")
    print(f"  剔除(过程文档等): {len(excluded)}")
    print(f"  保留(将同步): {len(final)}")

    # docs 保留明细
    docs_kept = [p for p in final if p.startswith("docs/")]
    print(f"  docs/ 保留: {len(docs_kept)} 个")
    for d in docs_kept:
        print(f"    ✓ {d}")

    if args.dry_run:
        print("\n[dry-run] 到此为止,未实际执行同步/提交/推送。")
        return

    # 3. 清空目标 + 复制
    print("\n[3/5] 同步文件到开源仓库...")
    clear_dst(dst_repo)
    copied, skipped = copy_files(src_repo, dst_repo, final)
    print(f"  已复制: {copied} 文件, 跳过: {skipped}")

    # 4. 增量提交(保留历史,支持外部 PR)
    print("\n[4/5] 创建增量同步提交(保留历史)...")
    create_sync_commit(dst_repo, head)
    new_head = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], cwd=dst_repo, text=True).strip()
    print(f"  当前 HEAD: {new_head}")
    commit_count = subprocess.check_output(["git", "rev-list", "--count", "HEAD"], cwd=dst_repo, text=True).strip()
    print(f"  历史提交数: {commit_count} (保留外部 PR 的历史)")

    # 5. 推送(非强推,远端有外部提交时会失败并提示人工 rebase)
    if args.no_push:
        print("\n[5/5] 跳过推送(--no-push)")
    else:
        print("\n[5/5] 推送到远端(保留历史)...")
        push(dst_repo)

    print("\n" + "=" * 60)
    print("  完成!")
    print("=" * 60)


if __name__ == "__main__":
    main()
