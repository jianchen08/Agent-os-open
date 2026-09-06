#!/usr/bin/env python3
"""提交信息卫生闸——敏感词提交信息在进入公开主链前脱敏（开源仓 sync 流程第 4 步）。

背景（2026-09-04 开源仓面试视角审查 / D11①）：公开仓提交信息曾内嵌源仓提交全文，
把内部漏洞审计叙述（漏洞点位、行号索引、默认凭据字面量、私人材料痕迹）发布到了
公开主链。sync 生成的一行式增量提交信息本身安全，但任何把源仓提交信息带进公开链
的路径（旧版 sync 内嵌格式、手工 replay/cherry-pick、历史重放发布）都需要一道
前置闸。

在 sync 流程中的位置（既有惯例的扩展）：
  1. 源仓 HEAD worktree 作 src
  2. 开源仓 reset 到 gitee/main（只落增量）
  3. sync 落增量提交（一行式信息，不带源提交正文）
  4. 本脚本 --scan（门禁）：gitee/main..HEAD 区间命中 block 级规则即拒绝推送   ← 本步
  5. 先 gitee 后 origin 推送

用法：
  python scripts/check_commit_msg_hygiene.py --scan [RANGE]        # 门禁模式（默认区间 gitee/main..HEAD），命中 block 即退出 1
  python scripts/check_commit_msg_hygiene.py --repo PATH --ref REF --last N
                                                                   # 只读演练：扫描另一仓库某分支最近 N 条提交信息（不修改该仓）
  python scripts/check_commit_msg_hygiene.py --messages-file FILE  # 演练模式：对文本文件逐块扫描（块以一行 "-----8<-----" 分隔）
  python scripts/check_commit_msg_hygiene.py --rewrite RANGE       # 改写未推送区间的提交信息（内联 [REDACTED:规则]），已推送提交存在则拒绝
  python scripts/check_commit_msg_hygiene.py --sanitize-stdin      # 内部用途：stdin 读一条信息、stdout 写脱敏结果（供 filter-branch --msg-filter 调用）

可选增强：
  --words-file PATH  额外敏感词表（一行一条；"re:" 前缀=正则，"warn:" 前缀=warn 级，
                     其余为 block 级字面量）。真实凭据字面量只放这里，不入库——
                     默认查找 scripts/.commit_msg_sensitive_words.local 与
                     环境变量 AGENTOS_MSG_SENSITIVE_WORDS 指向的文件。

规则分级：
  block = 命中即拒绝进入公开主链（凭据字面量 / 私人材料 / 内部路径 / 审计叙述与漏洞索引）
  warn  = 内部运维叙述（台账/派发/编排等），不拦截，提示操作者自行斟酌

输出中的命中证据一律截断脱敏（保留前 6 字符），避免敏感词本体出现在闸门输出里。
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from dataclasses import dataclass

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

MSG_DELIMITER = "-----8<-----"
REDACT_MARK = "[已隐去]"


@dataclass(frozen=True)
class Rule:
    rule_id: str
    severity: str  # "block" | "warn"
    pattern: re.Pattern
    description: str


def _r(rule_id: str, severity: str, expr: str, description: str) -> Rule:
    return Rule(rule_id, severity, re.compile(expr), description)


# 结构化规则：形态与内部文档路径，不含任何真实凭据字面量（真实字面量走 --words-file，
# 已知产品默认口令除外——它本就随 .env.example/README 公开，列此防"默认凭据"类叙述再现）。
RULES: list[Rule] = [
    # ---- 凭据字面量形态（gitleaks 式通配，非具体密钥） ----
    _r("cred-literal", "block", r"\bsk-(ant-)?[A-Za-z0-9_\-]{16,}\b", "OpenAI/Anthropic 风格 key 字面量"),
    _r("cred-literal", "block", r"\bgh[pousr]_[A-Za-z0-9]{30,}\b", "GitHub token 字面量"),
    _r("cred-literal", "block", r"\bgithub_pat_[A-Za-z0-9_]{40,}\b", "GitHub PAT 字面量"),
    _r("cred-literal", "block", r"\bAKIA[0-9A-Z]{16}\b", "AWS access key 字面量"),
    _r("cred-literal", "block", r"\bAIza[0-9A-Za-z_\-]{35}\b", "Google API key 字面量"),
    _r("cred-literal", "block", r"\bxox[baprs]-[A-Za-z0-9\-]{10,}\b", "Slack token 字面量"),
    _r("cred-literal", "block", r"admin12345", "产品默认口令字面量（公开文档既有值，禁止再入提交信息）"),
    _r(
        "cred-assignment",
        "block",
        r"(?i)\b(password|passwd|pwd|secret|api[_-]?key|access[_-]?token)\s*[=:]\s*[\"']?[A-Za-z0-9_\-+/=]{8,}",
        "凭据赋值形态",
    ),
    # ---- 私人材料引用 ----
    _r("private-material", "block", r"docs/working/private\b|docs/working[/\\]private", "私人材料目录引用"),
    _r("private-material", "block", r"简历|私人简历|cover[_-]?letter|portfolio|/resume/", "简历/私人材料痕迹"),
    # ---- 内部路径引用（公开仓不存在这些路径，引用即内部结构泄漏） ----
    _r(
        "internal-path",
        "block",
        r"docs/working/|docs/tasks/|docs/decisions/|agent-research/|reports/audit|\.project/",
        "内部仓过程区路径引用",
    ),
    # ---- 审计叙述与漏洞索引（D11① 类：漏洞点位/行号/轮次索引） ----
    _r(
        "audit-narrative",
        "block",
        r"风险审计|审计统合|审计\s*§|审计[高中低]置信|第[一二三四五六七八九十\d]+轮|漏洞利用|渗透测试|"
        r"未鉴权|越权|权限绕过|白名单外默认放行|默认凭据|明文比较|U\d{1,2}\s*[【(（]",
        "漏洞审计叙述/风险索引",
    ),
    # ---- 内部运维叙述（提示级：不拦截，操作者自行斟酌） ----
    _r(
        "ops-narrative",
        "warn",
        r"台账|派发|重派|挂账|在飞|编排者|orchestration|W\d{1,3}-\d{1,3}\b",
        "内部运维/编排叙述词",
    ),
]


def load_extra_rules(words_file: str | None) -> list[Rule]:
    """加载额外敏感词表：每行一条；re: 前缀=正则，warn: 前缀=warn 级，其余 block 级字面量。"""
    candidates = []
    if words_file:
        candidates.append(words_file)
    env_path = os.environ.get("AGENTOS_MSG_SENSITIVE_WORDS")
    if env_path:
        candidates.append(env_path)
    default_local = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".commit_msg_sensitive_words.local")
    candidates.append(default_local)
    for path in candidates:
        if path and os.path.isfile(path):
            rules = []
            with open(path, encoding="utf-8") as f:
                for line in f:
                    expr = line.strip()
                    if not expr or expr.startswith("#"):
                        continue
                    severity = "block"
                    if expr.startswith("warn:"):
                        severity, expr = "warn", expr[5:]
                    if expr.startswith("re:"):
                        expr = expr[3:]
                    else:
                        expr = re.escape(expr)
                    rules.append(_r("words-file", severity, expr, f"外部词表 {os.path.basename(path)}"))
            return rules
    return []


def mask_evidence(text: str, start: int, end: int) -> str:
    """命中证据截断脱敏：保留命中前文 20 字符 + 命中片段前 6 字符 + 隐去标记。"""
    head = text[max(0, start - 20) : start].replace("\n", " ")
    hit = text[start:end]
    shown = hit[:6] + REDACT_MARK if len(hit) > 6 else REDACT_MARK
    return f"...{head}{shown}..."


def scan_text(text: str, rules: list[Rule]) -> list[tuple[Rule, int, int]]:
    hits: list[tuple[Rule, int, int]] = []
    for rule in rules:
        for m in rule.pattern.finditer(text):
            hits.append((rule, m.start(), m.end()))
    return hits


def sanitize_message(text: str, rules: list[Rule]) -> str:
    """内联脱敏：命中片段替换为 [REDACTED:规则id]，消息结构（行/段落）不动。"""
    for rule in rules:
        if rule.severity != "block":
            continue
        text = rule.pattern.sub(f"[REDACTED:{rule.rule_id}]", text)
    return text


def _git(*args: str, cwd: str) -> str:
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失败: {r.stderr.strip()[:300]}")
    return r.stdout


def collect_from_repo(repo: str, ref: str, last: int) -> list[tuple[str, str]]:
    """只读收集另一仓库的提交（短 SHA, 完整信息）。"""
    out = _git("log", "-n", str(last), "--date=short", "--format=%h%x00%B%x00ENDMSG", ref, cwd=repo)
    commits = []
    for block in out.split("\x00ENDMSG"):
        block = block.strip("\n")
        if not block.strip():
            continue
        sha, _, msg = block.partition("\x00")
        commits.append((sha.strip(), msg.strip("\n")))
    return commits


def report(commits: list[tuple[str, str]], rules: list[Rule], title: str) -> int:
    """逐提交扫描并打印报告；返回 block 命中提交数。"""
    print(f"\n===== {title}（{len(commits)} 条提交，规则 {len(rules)} 条）=====")
    blocked = 0
    for sha, msg in commits:
        hits = scan_text(msg, rules)
        block_hits = [(r, s, e) for r, s, e in hits if r.severity == "block"]
        warn_hits = [(r, s, e) for r, s, e in hits if r.severity == "warn"]
        if not hits:
            print(f"[通过] {sha} {msg.splitlines()[0][:60] if msg.strip() else '(空)'}")
            continue
        if block_hits:
            blocked += 1
        subject = msg.splitlines()[0][:60] if msg.strip() else "(空)"
        print(f"[{'拦截' if block_hits else '提示'}] {sha} {subject}")
        for rule, s, e in block_hits:
            print(f"    [block:{rule.rule_id}] {rule.description} → {mask_evidence(msg, s, e)}")
        for rule, s, e in warn_hits:
            print(f"    [warn:{rule.rule_id}] {rule.description} → {mask_evidence(msg, s, e)}")
        if block_hits:
            print(
                f"    [脱敏改写预览] {sanitize_message(subject, rules)}"
                f"{' …（正文同步内联脱敏）' if msg.count(chr(10)) else ''}"
            )
    print(f"----- 小结：block 拦截 {blocked} 条，其余通过/提示 -----")
    return blocked


def published_check(repo: str, commits: list[tuple[str, str]]) -> list[str]:
    """返回区间内已被任一远端分支包含的提交（这些不可用 --rewrite 改写）。"""
    published = []
    for sha, _ in commits:
        refs = _git("branch", "-r", "--contains", sha, cwd=repo).strip()
        if refs:
            published.append(f"{sha} ← {refs.splitlines()[0]}")
    return published


def main() -> int:
    parser = argparse.ArgumentParser(description="提交信息卫生闸（公开主链前置脱敏）")
    parser.add_argument(
        "--scan",
        nargs="?",
        const="gitee/main..HEAD",
        default=None,
        metavar="RANGE",
        help="门禁模式：扫描区间（默认 gitee/main..HEAD），block 命中退出 1",
    )
    parser.add_argument("--repo", help="演练模式：目标仓库路径（只读）")
    parser.add_argument("--ref", default="HEAD", help="演练模式：目标引用（默认 HEAD）")
    parser.add_argument("--last", type=int, default=20, help="演练模式：最近 N 条（默认 20）")
    parser.add_argument("--messages-file", help="演练模式：从文本文件读提交信息（块间以 %s 分隔）" % MSG_DELIMITER)
    parser.add_argument("--rewrite", metavar="RANGE", help="改写模式：内联脱敏未推送区间的提交信息")
    parser.add_argument("--sanitize-stdin", action="store_true", help="内部：stdin→stdout 单条脱敏")
    parser.add_argument("--words-file", help="额外敏感词表（不入库）")
    args = parser.parse_args()

    rules = RULES + load_extra_rules(args.words_file)

    if args.sanitize_stdin:
        sys.stdout.write(sanitize_message(sys.stdin.read(), rules))
        return 0

    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    if args.rewrite:
        repo = here
        commits = collect_from_repo(repo, args.rewrite, 100000)
        if not commits:
            print(f"区间 {args.rewrite} 无提交，无事可做")
            return 0
        published = published_check(repo, commits)
        if published:
            print("✗ 区间内含已推送提交，--rewrite 拒绝执行（改写已发布历史需强推+历史重写授权）：")
            for line in published:
                print(f"    {line}")
            return 2
        dirty = _git("status", "--porcelain", cwd=repo).strip()
        if dirty:
            print("✗ 工作区不净，先提交或还原后再改写（EOL 幻影可 git -c core.autocrlf=false status 复核为空）")
            return 2
        oldest = commits[-1][0]
        env = dict(os.environ)
        script = os.path.abspath(__file__)
        cmd = [
            "git",
            "filter-branch",
            "-f",
            "--msg-filter",
            f'"{sys.executable}" "{script}" --sanitize-stdin',
            "--",
            f"{oldest}^..HEAD",
        ]
        print(f"改写区间 {args.rewrite}（{len(commits)} 条，最旧 {oldest}）…")
        r = subprocess.run(" ".join(cmd), shell=True, cwd=repo, env=env)
        return r.returncode

    if args.scan is not None:
        commits = collect_from_repo(here, args.scan, 100000)
        blocked = report(commits, rules, f"门禁扫描 {args.scan}")
        if blocked:
            print("✗ 存在 block 级命中：先 --rewrite 脱敏或修信息后再推送（先 gitee 后 origin）。")
            return 1
        print("✓ 无 block 级命中，可推送（先 gitee 后 origin）。")
        return 0

    if args.messages_file:
        with open(args.messages_file, encoding="utf-8") as f:
            content = f.read()
        commits = []
        for i, block in enumerate(content.split(MSG_DELIMITER)):
            block = block.strip("\n")
            if block.strip():
                commits.append((f"msg#{i + 1}", block))
        blocked = report(commits, rules, f"文本演练 {args.messages_file}")
        return 1 if blocked else 0

    if args.repo:
        commits = collect_from_repo(args.repo, args.ref, args.last)
        blocked = report(commits, rules, f"演练 {args.repo} @{args.ref} 最近 {len(commits)} 条")
        return 1 if blocked else 0

    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
