"""技能类提案安检校验器（SkillSpector 式静态安检，纯函数，机械执行）。

原则 7（ADR 2026-10-09-mode-system-consolidation 决策 7）：出厂 skills/ 运行期
唯一写路径 = 自进化提案管道——管道因此是技能进入系统的唯一闸门，安检在本层
fail-closed：技能类提案（target 在 skills/ 下）任一命中即拒绝，违规信息指明
命中项。

检查面（数据驱动：模式表追加 ``(label, pattern)`` 条目即扩面）：

1. 提示注入模式（SKILL.md 与脚本附件同扫）：角色劫持指令 /
   「忽略之前的规则」类指令 / 隐藏指令标记（零宽字符、注释藏令）；
2. 命令面危险模式（同扫）：凭据访问 / 外传网络调用 / 破坏性删除 / 提权。
   凭据与破坏面模式以访问动词/精确目标锚定——``process.env.CI``、测试断言
   ``Invalid credentials``、``rm -rf /tmp/*`` 等出厂技能合法内容不误伤
   （test_skill_audit 出厂技能全量零命中车道执法）；命令词元一律带词首边界
   （rm/del/rd/curl/wget/reg），裸子串形态会把英文词（warm/board 等）误判为
   命令；
3. 脚本附件声明面核对（单文件提案语义下的两向一致性）：SKILL.md 提案 =
   声明真值落地时刻——content 引用的 scripts/ 文件必须已在磁盘（幽灵引用
   拒）；磁盘 scripts/ 下的执行面文件必须被 content 声明（未声明拒）。
   声明形态三可：``scripts/<相对路径>`` / 裸文件名 / 目录级 ``scripts/<段>/``
   （镜像类整目录附件，如 skill-dev-toolkit 的 scripts/repo/ 字节级镜像）。
   测试附件（``test_*`` / ``*_test`` / ``conftest``）与字节码缓存不是执行面，
   豁免声明。脚本附件提案只过 1/2 两面——声明由后续 SKILL.md 提案补齐
   （单文件原子性约束下，先落附件后改声明是唯一可自举顺序）。
"""

from __future__ import annotations

import os
import re
from pathlib import Path

VIOLATION_PREFIX = "skill_audit:"

# 声明面/扫描面范围（audit 的 doc_scope 参数取值）：
#   file    — 仅 content 单文件（自进化提案语义，存量默认）
#   package — 追加技能目录内全部 .md 兄弟文档（外部生态导入语义）
# 见 ADR 2026-10-17-skill-audit-package-scope。
DOC_SCOPE_FILE = "file"
DOC_SCOPE_PACKAGE = "package"
_DOC_SCOPES = frozenset({DOC_SCOPE_FILE, DOC_SCOPE_PACKAGE})

# ── 提示注入模式表（SKILL.md 与脚本附件同扫） ─────────────────────────────
INJECTION_PATTERNS: tuple[tuple[str, str], ...] = (
    (
        "角色劫持指令",
        r"(你现在是|从现在起你是|从现在开始你是|从今以后你是|你不再是|你的新(身份|角色|任务)是"
        r"|you\s+are\s+no\s+longer|you\s+must\s+now\s+(act|behave|follow)"
        r"|pretend\s+to\s+be|act\s+as\s+an?\s+(unrestricted|unfiltered|uncensored|unconstrained))",
    ),
    (
        "「忽略之前的规则」类指令",
        r"(忽略(之前|先前|此前|以上|上述|上面|前面)(的)?(所有|全部|一切)?(规则|指令|约束|设定|提示|要求|内容|安全)"
        r"|无视(之前|先前|此前|以上|上述)"
        r"|(ignore|disregard|forget|override)\s+(all\s+|any\s+|your\s+)?"
        r"(previous|prior|earlier|above|system|safety)\s+"
        r"(rules?|instructions?|prompts?|constraints?|guidelines?|policies?))",
    ),
    (
        "隐藏指令标记",
        r"([\u200b\u200c\u200d\u2060\ufeff]"
        r"|<!--\s*(ignore|disregard|system|instruction|指令|提示词|规则)"
        r"|(隐藏|真实|实际|新增)指令\s*[:：]|(hidden|secret)\s+(instruction|task|mission))",
    ),
)

# ── 命令面危险模式表 ──────────────────────────────────────────────────────
CMD_PATTERNS: tuple[tuple[str, str], ...] = (
    (
        "凭据访问",
        r"((\bcat\b|\btype\b|\bhead\b|\btail\b|\bcp\b|\bmv\b|\bmore\b|Get-Content"
        r"|读取|查看)[^;\n|]{0,60}"
        r"(\.env\b|\.netrc\b|\.git-credentials|id_rsa|id_ed25519"
        r"|\.ssh[/\\]|\.aws[/\\]|\.kube[/\\])"
        r"|(读取|窃取|导出|打印)[^;\n|]{0,20}"
        r"(凭据|密钥|私钥|口令|secret|credential|api[_-]?key|access[_-]?token)"
        r"|(\bcurl|\bwget)\s[^;\n|]{0,80}(\.env\b|id_rsa|\.netrc|\.aws[/\\]|\.ssh[/\\]))",
    ),
    (
        "外传网络调用",
        r"((\bcurl|\bwget)\s[^;\n]{0,100}"
        r"(\$\(|-d\s*@|-T\s|--upload-file|--data-binary"
        r"|webhook\.site|pastebin\.com|termbin|hastebin|0x0\.st|requestbin|ngrok"
        r"|discord(app)?\.com/api/webhooks)"
        r"|(requests|httpx)\.(post|put|request)\s*\([^;\n]{0,120}"
        r"(environ|getenv|b64encode|\bopen\()"
        r"|\bnc\s+-e\s|/dev/tcp/)",
    ),
    (
        "破坏性删除",
        r"(\brm\s+[^;\n]{0,20}\s+(--no-preserve-root\s+)?(/|~/|\$HOME|[A-Za-z]:[\\/]?)(\s|$)"
        r"|Remove-Item\s+[^;\n]{0,40}\s(~|[A-Za-z]:[\\/]?|/)(\s|$)"
        r"|(\bdel|\brd)\s+[^;\n]{0,10}/s\b"
        r"|:\(\)\s*\{\s*:\s*\|\s*:&\s*\}\s*;:"
        r"|\bmkfs(\.\w+)?\s"
        r"|\bdd\s+if=/dev/(zero|random)\s+of=/dev/[sh]d[a-z]"
        r"|\bformat\s+[A-Za-z]:|\bdiskpart\b|>\s*/dev/sd[a-z])",
    ),
    (
        "提权",
        r"(\bsudo\s+\S|\brunas\b|\bsetuid\b"
        r"|chmod\s+(777|4755|\+[s])"
        r"|icacls\s[^;\n]{0,60}/grant"
        r"|\breg\s+add\s+HKLM"
        r"|powershell\s[^;\n]{0,30}-enc(oded|odedcommand)\b"
        r"|\bbitsadmin\b|\bschtasks\s+/create\b)",
    ),
)

# 脚本附件引用形态（scripts/ 前缀 + 带扩展名文件；反斜杠形态先归一）。
# 声明还有**裸文件名形态**（如 `### clear_execution_data.py`，不带 scripts/
# 前缀）——只用于「未声明」方向的核对（裸名无路径语义，不参与幽灵引用判定）。
_SCRIPT_REF_RE = re.compile(r"scripts/[\w./-]+\.\w+")
# 目录级声明形态：``scripts/<段>/`` 且尾斜杠后**不再是路径字符**——声明该子树
# 全部执行面文件（镜像类整目录附件的声明形态，如 skill-dev-toolkit 的
# ``scripts/repo/`` 字节级镜像）。文件引用（``scripts/x/y.py``）不产生目录
# 声明：尾斜杠后跟路径字符时负向断言不成立，防止按文件名连带声明同目录兄弟件。
_SCRIPT_DIR_DECL_RE = re.compile(r"scripts/[\w.-]+/(?![\w.-])")
# 跨技能全路径引用（skills/<name>/scripts/...）按主仓根解析（如 B1 合并后
# skill-container-task-flow 引用 skill-task-chain-viewer 的脚本）。
_SKILL_PREFIXED_REF_RE = re.compile(r"\bskills/[\w./-]+\.\w+")
_TEST_ASSET_RE = re.compile(r"(^|/)(test_[^/]+|[^/]+_test|conftest)\.\w+$")
_SKIP_SUFFIXES = {".pyc", ".pyo"}
# 非执行附件格式白名单（文档/数据/配置/归档/图像）：不作为执行面要求声明。
# 判定形态是白名单（fail-closed）：未列出的扩展名与无扩展名文件一律仍计入执行面——
# 新执行格式（如 .lua/.wasm）默认要声明；.html/.svg 可承载活动内容，不豁免。
# 见 ADR 2026-10-17-script-attachment-executable-face。
_NON_EXECUTABLE_SUFFIXES = frozenset(
    {
        ".md", ".markdown", ".txt", ".rst",
        ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".csv", ".tsv", ".lock", ".xml", ".example",
        ".zip", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".whl",
        ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico",
    }
)  # fmt: skip


def _norm_target(target: str) -> str:
    return str(target).replace("\\", "/").lstrip("/")


def _scan(content: str, table: tuple[tuple[str, str], ...], kind: str) -> list[str]:
    """逐行扫模式表；命中项带行号与截断原文（拒绝信息指明命中项）。

    IGNORECASE：英文注入/命令形态大小写不定（句首 "Ignore all previous
    instructions" 是最常见写法），检测面按不区分大小写执法；出厂技能全量
    零命中车道（test_skill_audit）执法误伤面不随大小写放开而扩。
    """
    violations: list[str] = []
    for lineno, line in enumerate(content.splitlines(), 1):
        for label, pattern in table:
            if re.search(pattern, line, re.IGNORECASE):
                excerpt = line.strip()[:80]
                violations.append(f"{VIOLATION_PREFIX} {kind}[{label}] 第{lineno}行: {excerpt}")
    return violations


def _phantom_refs(content: str, skill_dir: Path, project_root: Path) -> set[str]:
    """content 引用但任何锚都解析不到的脚本附件（归一为 scripts/<相对> 形态）。

    脚本面形态只有 ``scripts/`` 前缀一种：``_SCRIPT_REF_RE`` 天然含前缀；
    ``_SKILL_PREFIXED_REF_RE`` 是宽形态匹配，会一并命中跨技能的 .md 路径
    （如 ``skills/<other>/<doc>.md``）——无 ``scripts/`` 段的命中不进脚本
    判定（该形态无脚本尾段可归一）。

    解析锚（任一命中即存在）：①本技能目录下 tail（``skill_dir/scripts/x.py``）；
    ②主仓根下的匹配原串（覆盖 ``skills/<name>/scripts/x.py`` 跨技能全路径
    引用，如 B1 合并后 skill-container-task-flow 引用 task-chain-viewer 的
    脚本）。
    """
    text = content.replace("\\", "/")
    referenced: set[str] = set()
    satisfied: set[str] = set()
    matches = {m.group(0) for m in _SCRIPT_REF_RE.finditer(text)}
    matches |= {m.group(0) for m in _SKILL_PREFIXED_REF_RE.finditer(text)}
    for ref in matches:
        idx = ref.rfind("scripts/")
        if idx < 0:
            continue
        tail = ref[idx:]
        referenced.add(tail)
        if (skill_dir / tail).is_file() or (project_root / ref).is_file():
            satisfied.add(tail)
    return referenced - satisfied


def _undeclared_scripts(content: str, skill_dir: Path) -> set[str]:
    """磁盘执行面附件中 content 未声明的部分。

    声明形态三可：``scripts/<相对路径>``、裸文件名（含跨技能全路径时取
    尾段同形归一）、目录级 ``scripts/<段>/``（尾斜杠后非路径字符——声明该
    子树全部执行面文件；文件引用不连带声明同目录兄弟件）。
    """
    text = content.replace("\\", "/")
    actual = _scripts_on_disk(skill_dir)
    declared_dirs = tuple(m.group(0) for m in _SCRIPT_DIR_DECL_RE.finditer(text))
    undeclared: set[str] = set()
    for rel in actual:
        name = rel.split("/")[-1]
        if rel in text or name in text:
            continue
        if any(rel.startswith(prefix) for prefix in declared_dirs):
            continue
        undeclared.add(rel)
    return undeclared


def _scripts_on_disk(skill_dir: Path) -> set[str]:
    """磁盘 scripts/ 下的**执行面**附件集合（``scripts/<相对路径>`` 形态）。

    执行面 = 可执行格式与未穷举格式（白名单豁免非执行附件：文档/数据/配置/归档/图像），
    测试附件与字节码缓存同样不计入。
    """
    scripts_dir = skill_dir / "scripts"
    if not scripts_dir.is_dir():
        return set()
    actual: set[str] = set()
    for path in scripts_dir.rglob("*"):
        if not path.is_file() or path.suffix in _SKIP_SUFFIXES:
            continue
        if path.suffix.lower() in _NON_EXECUTABLE_SUFFIXES:
            continue
        rel = path.relative_to(skill_dir).as_posix()
        if _TEST_ASSET_RE.search(rel):
            continue
        actual.add(rel)
    return actual


def _package_docs(skill_dir: Path) -> tuple[list[tuple[Path, str]], list[str]]:
    """技能包文档集（递归 ``*.md``，排除目标位 SKILL.md 本体）。

    Returns:
        (可读文档 [(路径, 正文)], 不可读文件名清单)——不可读项由调用方转违规
        （声明面读不全即不能判定声明是否成立，fail-closed 不静默跳过）。
    """
    if not skill_dir.is_dir():
        return [], []
    readable: list[tuple[Path, str]] = []
    unreadable: list[str] = []
    for path in sorted(skill_dir.rglob("*.md")):
        if not path.is_file() or (path.parent == skill_dir and path.name.upper() == "SKILL.MD"):
            continue
        try:
            readable.append((path, path.read_text(encoding="utf-8")))
        except (OSError, UnicodeDecodeError):
            unreadable.append(path.name)
    return readable, unreadable


def _declaration_check(
    target: str,
    content: str,
    project_root: str,
    doc_scope: str = "file",
    package_docs: list[tuple[Path, str]] | None = None,
) -> list[str]:
    """声明面核对（仅 SKILL.md 提案——声明真值落地时刻）。

    doc_scope 决定判定面：``file`` = 这份 SKILL.md 单文件（自进化提案语义：
    提案不携带附件，声明即真值；两个方向都执法）；``package`` = 技能包文档集
    （``content`` ∪ 技能目录内全部 .md，外部生态导入语义——见 ADR
    2026-10-17-skill-audit-package-scope）：
    - **声明方向保留**：磁盘执行面附件必须被本技能自己的文档点名（安全性质）；
    - **引用方向不执法**：那是提案原子性检查（先提附件、后改声明），整目录快照
      导入无先后顺序问题、在包面本无执法对象；真实第三方 SKILL.md 的
      ``scripts/…`` 串多为示例与文档片段（实测：`Example: scripts/rotate_pdf.py`、
      ``${CLAUDE_PLUGIN_ROOT}/scripts/helper.py`` 片段），执法即误伤。
    """
    if os.path.basename(target).upper() != "SKILL.MD":
        return []
    skill_dir = Path(project_root) / os.path.dirname(target)
    root = Path(project_root)
    violations: list[str] = []
    if doc_scope == DOC_SCOPE_FILE:
        for ref in sorted(_phantom_refs(str(content), skill_dir, root)):
            violations.append(
                f"{VIOLATION_PREFIX} SKILL.md 引用不存在的脚本附件 {ref}"
                "（单文件提案不携带附件：先提脚本附件，后改 SKILL.md 声明）"
            )
    surface = "\n".join([str(content), *(text for _path, text in package_docs or ())])
    undeclared_lead = "脚本附件未在 SKILL.md 声明" if doc_scope == DOC_SCOPE_FILE else "脚本附件未在技能目录文档声明"
    for path in sorted(_undeclared_scripts(surface, skill_dir)):
        violations.append(f"{VIOLATION_PREFIX} {undeclared_lead} {path}（声明面与执行面不一致）")
    return violations


def audit(
    target: str,
    content: str,
    project_root: str,
    *,
    doc_scope: str = DOC_SCOPE_FILE,
) -> list[str]:
    """技能类提案安检：返回违规清单；空列表 = 通过。

    Args:
        target: 主仓相对路径（skills/ 下任意文件）。
        content: 提案后的完整文件内容。
        project_root: 项目根（脚本附件存在性核对锚）。
        doc_scope: 声明面/扫描面范围。``file``（默认）= 仅 ``content`` 单文件
            （自进化提案语义，存量行为）；``package`` = 追加技能目录内全部
            ``.md`` 兄弟文档（声明面并集 + 逐文档扫注入/危险命令，违规带文件名；
            不可读文档即拒收）。非法值 raise ValueError（不静默回落）。

    Raises:
        ValueError: doc_scope 非法。
    """
    if doc_scope not in _DOC_SCOPES:
        raise ValueError(f"未知 doc_scope：{doc_scope!r}（合法值：{sorted(_DOC_SCOPES)}）")
    norm = _norm_target(target)
    if not norm.startswith("skills/") or not str(content).strip():
        return []
    violations = [
        *_scan(str(content), INJECTION_PATTERNS, "提示注入"),
        *_scan(str(content), CMD_PATTERNS, "危险命令"),
    ]
    package_docs: list[tuple[Path, str]] = []
    if doc_scope == DOC_SCOPE_PACKAGE:
        package_docs, unreadable = _package_docs(Path(project_root) / os.path.dirname(norm))
        violations.extend(
            f"{VIOLATION_PREFIX} 技能目录文档不可读（拒收）：{name}（声明面读不全）" for name in unreadable
        )
    violations.extend(_declaration_check(norm, str(content), project_root, doc_scope, package_docs))
    return violations
