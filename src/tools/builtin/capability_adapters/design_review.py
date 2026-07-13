"""
设计审查适配器

暴露接口：
- DesignReviewTool：BuiltinTool，审查页面的元素缺陷、交互Bug、无障碍问题

后端链: playwright_screenshot → figma_compare（已弃用）
新流程: navigate → snapshot → evaluate → console → screenshot
"""

import json
import logging
from typing import Any

from tools.types import (
    Tool,
    ToolCategory,
    ToolLevel,
    ToolResult,
    ToolSource,
    create_failure_result,
    create_success_result,
)

from ._base import CapabilityAdapterBase

logger = logging.getLogger(__name__)

_LAYOUT_SCRIPT = """
(() => {
  const issues = [];
  const els = document.querySelectorAll('div, section, article, main, header, footer, nav');
  for (const el of els) {
    if (el.offsetWidth === 0 || el.offsetHeight === 0) continue;
    if (el.scrollWidth > el.clientWidth + 2) {
      issues.push({type:'layout',severity:'medium',selector:_s(el),description:'Element overflows container by '+(el.scrollWidth-el.clientWidth)+'px'});
    }
    if (el.scrollHeight > el.clientHeight + 2 && getComputedStyle(el).overflow==='visible') {
      issues.push({type:'layout',severity:'low',selector:_s(el),description:'Content overflows vertically'});
    }
  }
  return issues;
})()
"""

_COLOR_SCRIPT = """
(() => {
  const issues = [];
  const targets = document.querySelectorAll('p,span,a,h1,h2,h3,h4,h5,h6,button,label,li,td,th');
  for (const el of targets) {
    if (el.offsetWidth === 0) continue;
    const s = getComputedStyle(el);
    const fg = s.color, bg = s.backgroundColor;
    if (!fg || bg==='rgba(0, 0, 0, 0)' || bg==='transparent') continue;
    const lum = (c) => { const m=c.match(/\\d+/g); return m?(0.299*+m[0]+0.587*+m[1]+0.114*+m[2])/255:0; };
    const r = Math.max((lum(fg)+0.05)/(lum(bg)+0.05),(lum(bg)+0.05)/(lum(fg)+0.05));
    if (r < 2.0) {
      issues.push({type:'color',severity:'high',selector:_s(el),description:'Low contrast ratio: '+r.toFixed(2)});
    }
  }
  return issues;
})()
"""

_TYPOGRAPHY_SCRIPT = """
(() => {
  const issues = [];
  const targets = document.querySelectorAll('p,span,a,h1,h2,h3,h4,h5,h6,button,label,li,small');
  for (const el of targets) {
    if (el.offsetWidth === 0) continue;
    const sz = parseFloat(getComputedStyle(el).fontSize);
    if (sz > 0 && sz < 10) {
      issues.push({type:'typography',severity:'medium',selector:_s(el),description:'Font size too small: '+sz+'px'});
    }
  }
  return issues;
})()
"""

_RESPONSIVE_SCRIPT = """
(() => {
  const issues = [];
  const els = document.querySelectorAll('div,section,article,main,header,footer,nav,table,img');
  for (const el of els) {
    if (el.offsetWidth === 0) continue;
    const w = getComputedStyle(el).width;
    if (w && w.endsWith('px')) {
      const px = parseFloat(w);
      if (px > window.innerWidth) {
        issues.push({type:'responsive',severity:'high',selector:_s(el),description:'Fixed width '+px+'px exceeds viewport '+window.innerWidth+'px'});
      }
    }
  }
  return issues;
})()
"""

_SPACING_SCRIPT = """
(() => {
  const issues = [];
  const els = document.querySelectorAll('button,a,input,select,textarea');
  for (const el of els) {
    if (el.offsetWidth === 0) continue;
    const s = getComputedStyle(el);
    const pad = parseFloat(s.paddingTop)+parseFloat(s.paddingBottom)+parseFloat(s.paddingLeft)+parseFloat(s.paddingRight);
    if (pad === 0 && el.textContent.trim().length > 0) {
      issues.push({type:'spacing',severity:'low',selector:_s(el),description:'Interactive element has zero padding'});
    }
  }
  return issues;
})()
"""

_ACCESSIBILITY_SCRIPT = """
(() => {
  const issues = [];
  document.querySelectorAll('img:not([alt])').forEach(el => {
    issues.push({type:'accessibility',severity:'high',selector:_s(el),description:'Image missing alt attribute'});
  });
  document.querySelectorAll('input,select,textarea').forEach(el => {
    const id=el.id;
    const hasLabel=id&&document.querySelector('label[for="'+id+'"]');
    const hasAria=el.getAttribute('aria-label')||el.getAttribute('aria-labelledby');
    if(!hasLabel&&!hasAria){
      issues.push({type:'accessibility',severity:'medium',selector:_s(el),description:'Form element missing label'});
    }
  });
  document.querySelectorAll('button,a').forEach(el => {
    if(!el.textContent.trim()&&!el.getAttribute('aria-label')&&!el.querySelector('img[alt]')){
      issues.push({type:'accessibility',severity:'high',selector:_s(el),description:'Interactive element has no accessible text'});
    }
  });
  document.querySelectorAll('[role]').forEach(el => {
    const valid=['alert','alertdialog','application','article','banner','button','cell','checkbox','columnheader','combobox','command','complementary','composite','contentinfo','definition','dialog','directory','document','feed','figure','form','grid','gridcell','group','heading','img','input','landmark','link','list','listbox','listitem','log','main','marquee','math','menu','menubar','menuitem','menuitemcheckbox','menuitemradio','navigation','none','note','option','presentation','progressbar','radio','radiogroup','range','region','roletype','row','rowgroup','rowheader','scrollbar','search','searchbox','section','sectionhead','select','separator','slider','spinbutton','status','structure','switch','tab','table','tablist','tabpanel','textbox','timer','toolbar','tooltip','tree','treegrid','treeitem','widget','window'];
    if(!valid.includes(el.getAttribute('role'))){
      issues.push({type:'accessibility',severity:'medium',selector:_s(el),description:'Invalid ARIA role: '+el.getAttribute('role')});
    }
  });
  return issues;
})()
"""

_INTERACTION_SCRIPT = """
(() => {
  const issues = [];
  document.querySelectorAll('a[href=""]').forEach(el => {
    issues.push({type:'interaction',severity:'high',selector:_s(el),description:'Empty href on link'});
  });
  document.querySelectorAll('a[href="#"]').forEach(el => {
    issues.push({type:'interaction',severity:'medium',selector:_s(el),description:'Placeholder href="#" on link'});
  });
  document.querySelectorAll('button').forEach(el => {
    const disabled=el.disabled||el.getAttribute('aria-disabled')==='true';
    if(disabled&&getComputedStyle(el).cursor==='pointer'){
      issues.push({type:'interaction',severity:'low',selector:_s(el),description:'Disabled button appears clickable'});
    }
  });
  document.querySelectorAll('input[type="submit"],input[type="button"]').forEach(el => {
    issues.push({type:'interaction',severity:'medium',selector:_s(el),description:'Use <button> instead of <input type="submit/button"> for better accessibility'});
  });
  document.querySelectorAll('[onclick]').forEach(el => {
    if(!el.getAttribute('role')&&!['button','a','input','select','textarea'].includes(el.tagName.toLowerCase())){
      issues.push({type:'interaction',severity:'low',selector:_s(el),description:'Element with onclick has no keyboard accessibility'});
    }
  });
  return issues;
})()
"""

_DOM_STRUCTURE_SCRIPT = """
(() => {
  const issues = [];
  const ids = {};
  document.querySelectorAll('[id]').forEach(el => {
    ids[el.id] = (ids[el.id]||0)+1;
    if(ids[el.id]===2){
      issues.push({type:'dom_structure',severity:'high',selector:'#'+el.id,description:'Duplicate ID: '+el.id});
    }
  });
  document.querySelectorAll('*').forEach(el => {
    let depth=0, p=el;
    while(p.parentElement){depth++;p=p.parentElement;}
    if(depth>20){
      issues.push({type:'dom_structure',severity:'medium',selector:_s(el),description:'Deeply nested element ('+depth+' levels)'});
    }
  });
  document.querySelectorAll('br+br').forEach(el => {
    issues.push({type:'dom_structure',severity:'low',selector:_s(el),description:'Consecutive <br> tags — use CSS margin instead'});
  });
  return issues;
})()
"""

_HELPER_FN = """
function _s(el){
  if(el.id) return '#'+el.id;
  if(el.className&&typeof el.className==='string'){
    const cls=el.className.trim().split(/\\s+/)[0];
    if(cls) return el.tagName.toLowerCase()+'.'+cls;
  }
  return el.tagName.toLowerCase();
}
"""


class DesignReviewTool(CapabilityAdapterBase):
    """设计审查工具，检测页面的元素缺陷、交互Bug、无障碍问题。"""

    _adapter_name = "design_review"

    _CHECK_SCRIPTS: dict[str, str] = {
        "layout": _LAYOUT_SCRIPT,
        "color": _COLOR_SCRIPT,
        "typography": _TYPOGRAPHY_SCRIPT,
        "responsive": _RESPONSIVE_SCRIPT,
        "spacing": _SPACING_SCRIPT,
        "accessibility": _ACCESSIBILITY_SCRIPT,
        "interaction": _INTERACTION_SCRIPT,
        "dom_structure": _DOM_STRUCTURE_SCRIPT,
    }

    @staticmethod
    def get_tool_definition() -> Tool:
        return Tool(
            name="design_review",
            description=(
                "审查页面的元素缺陷、交互Bug和无障碍问题。"
                "通过无障碍快照、JS注入检测和控制台日志分析，"
                "从布局、颜色、字体、间距、响应式、无障碍、交互、DOM结构等维度进行检查。"
                "自动回退到可用的后端。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "要审查的页面 URL",
                    },
                    "check_types": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "enum": [
                                "layout",
                                "color",
                                "typography",
                                "responsive",
                                "spacing",
                                "accessibility",
                                "interaction",
                                "dom_structure",
                            ],
                        },
                        "default": ["layout", "color", "accessibility", "interaction"],
                        "description": "要检查的维度",
                    },
                    "include_screenshot": {
                        "type": "boolean",
                        "default": False,
                        "description": "是否同时截图（用于视觉参考）",
                    },
                },
                "required": ["url"],
            },
            when_to_use=[
                "需要检测前端页面的元素缺陷和布局问题",
                "需要检查无障碍合规性（缺失alt、label等）",
                "需要发现交互Bug（空链接、禁用按钮等）",
                "需要审查DOM结构（重复ID、深层嵌套等）",
            ],
            when_not_to_use=[
                "需要功能逻辑测试（应使用 playwright_test）",
                "需要代码质量检查（应使用 code_reviewer agent）",
            ],
            source=ToolSource.BUILTIN,
            category=ToolCategory.ANALYSIS,
            level=ToolLevel.USER,
            tags=["design", "review", "accessibility", "interaction", "mcp"],
        )

    def _build_inspect_script(self, check_types: list[str]) -> str:
        """根据 check_types 组合 JS 检测脚本。"""
        parts = [_HELPER_FN]
        parts.append("const __all_issues = [];")
        for ct in check_types:
            script = self._CHECK_SCRIPTS.get(ct)
            if script:
                parts.append(f"__all_issues.push(...{script});")
        parts.append("return JSON.stringify({issues: __all_issues});")
        body = "\n".join(parts)
        return f"(() => {{\n{body}\n}})()"

    def _build_steps(
        self,
        backend: Any,
        url: str,
        check_types: list[str],
        include_screenshot: bool,
    ) -> list[tuple[str, dict[str, Any]]]:
        """构建审查步骤：导航 → 快照 → JS检测 → 控制台 → 截图。"""
        tm = backend.tool_mapping
        steps: list[tuple[str, dict[str, Any]]] = []

        nav_tool = tm.get("navigate", "browser_navigate")
        steps.append((nav_tool, {"url": url}))

        snapshot_tool = tm.get("snapshot", "browser_snapshot")
        steps.append((snapshot_tool, {}))

        evaluate_tool = tm.get("evaluate", "browser_evaluate")
        inspect_script = self._build_inspect_script(check_types)
        steps.append((evaluate_tool, {"function": inspect_script}))

        console_tool = tm.get("console", "browser_console_messages")
        steps.append((console_tool, {}))

        if include_screenshot:
            screenshot_tool = tm.get("screenshot", "browser_take_screenshot")
            steps.append((screenshot_tool, {}))

        return steps

    def _parse_snapshot_issues(self, snapshot_text: str) -> list[dict[str, Any]]:
        """从无障碍快照中提取结构问题。"""
        issues: list[dict[str, Any]] = []
        if not snapshot_text or not isinstance(snapshot_text, str):
            return issues
        lines = snapshot_text.split("\n")
        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue
            if "StaticText" in stripped and len(stripped) < 3:
                issues.append(
                    {
                        "type": "dom_structure",
                        "severity": "low",
                        "selector": "text-node",
                        "description": "Very short text node detected in accessibility tree",
                    }
                )
        return issues[:20]

    def _parse_console_issues(self, console_data: Any) -> list[dict[str, Any]]:
        """从控制台日志中提取 JS 错误和警告。"""
        issues: list[dict[str, Any]] = []
        if not console_data:
            return issues
        if isinstance(console_data, str):
            try:
                console_data = json.loads(console_data)
            except (json.JSONDecodeError, TypeError):
                return issues
        if isinstance(console_data, list):
            for entry in console_data:
                if not isinstance(entry, dict):
                    continue
                msg_type = entry.get("type", "")
                text = entry.get("text", "")
                if msg_type in ("error", "warning") and text:
                    issues.append(
                        {
                            "type": "interaction",
                            "severity": "high" if msg_type == "error" else "medium",
                            "selector": "console",
                            "description": f"Console {msg_type}: {text[:200]}",
                        }
                    )
        elif isinstance(console_data, dict):
            for entry in console_data.get("messages", []):
                if isinstance(entry, dict):
                    msg_type = entry.get("type", "")
                    text = entry.get("text", "")
                    if msg_type in ("error", "warning") and text:
                        issues.append(
                            {
                                "type": "interaction",
                                "severity": "high" if msg_type == "error" else "medium",
                                "selector": "console",
                                "description": f"Console {msg_type}: {text[:200]}",
                            }
                        )
        return issues

    async def execute(self, inputs: dict[str, Any]) -> ToolResult:
        """执行设计审查。"""
        url = inputs.get("url", "").strip()
        if not url:
            return create_failure_result(
                error="url 不能为空",
                error_code="EMPTY_INPUT",
            )

        check_types = inputs.get("check_types", ["layout", "color", "accessibility", "interaction"])
        include_screenshot = inputs.get("include_screenshot", False)

        backends = self._get_backends()
        if not backends:
            return self._fail_no_backends()

        last_error: Exception | None = None
        attempted = False
        for backend in backends:
            if not backend.available:
                continue
            attempted = True
            try:
                steps = self._build_steps(backend, url, check_types, include_screenshot)
                raw_results = await self._call_backend_multi_step(backend, steps)
                parsed_results = [self._extract_mcp_content(r) for r in raw_results]
                return self._transform_results(parsed_results, backend.name, check_types)
            except Exception as e:
                logger.warning(
                    "[DesignReview] 后端 '%s' 失败: %s",
                    backend.name,
                    e,
                )
                last_error = e

        if not attempted:
            return self._fail_no_backends()

        return create_failure_result(
            error=f"所有后端均失败: {last_error}",
            error_code="ALL_BACKENDS_FAILED",
        )

    def _transform_results(  # noqa: PLR0912
        self,
        parsed_results: list[Any],
        backend_name: str,
        check_types: list[str],
    ) -> ToolResult:
        """将审查结果规范化为统一格式。"""
        all_issues: list[dict[str, Any]] = []
        snapshot_text = ""
        console_data = None
        screenshot_data = None
        js_result = None

        for idx, result in enumerate(parsed_results):
            if isinstance(result, dict) and result.get("error"):
                all_issues.append(
                    {
                        "type": "system",
                        "severity": "high",
                        "selector": "mcp",
                        "description": f"MCP step {idx} returned error: {result.get('message', '')[:200]}",
                    }
                )
                continue

            if idx == 1:
                snapshot_text = str(result) if result else ""
                snapshot_issues = self._parse_snapshot_issues(snapshot_text)
                all_issues.extend(snapshot_issues)
            elif idx == 2:
                raw = result
                if isinstance(raw, str):
                    try:
                        parsed = json.loads(raw)
                        js_result = parsed
                    except (json.JSONDecodeError, TypeError):
                        js_result = raw
                else:
                    js_result = raw
                if isinstance(js_result, dict) and "issues" in js_result:
                    all_issues.extend(js_result["issues"])
                elif isinstance(js_result, list):
                    all_issues.extend(js_result)
            elif idx == 3:
                console_data = result
                console_issues = self._parse_console_issues(console_data)
                all_issues.extend(console_issues)
            elif idx == 4:
                screenshot_data = result

        by_severity: dict[str, int] = {"high": 0, "medium": 0, "low": 0}
        by_type: dict[str, int] = {}
        for issue in all_issues:
            if isinstance(issue, dict):
                sev = issue.get("severity", "low")
                typ = issue.get("type", "unknown")
                by_severity[sev] = by_severity.get(sev, 0) + 1
                by_type[typ] = by_type.get(typ, 0) + 1

        total = len(all_issues)
        score = max(
            0, 100 - by_severity.get("high", 0) * 10 - by_severity.get("medium", 0) * 3 - by_severity.get("low", 0)
        )

        summary = (
            f"发现 {total} 个问题: "
            f"{by_severity.get('high', 0)} 高 / "
            f"{by_severity.get('medium', 0)} 中 / "
            f"{by_severity.get('low', 0)} 低 | "
            f"评分: {score}/100"
        )

        data: dict[str, Any] = {
            "issues": all_issues,
            "summary": summary,
            "score": score,
            "check_types": check_types,
            "total_issues": total,
            "by_severity": by_severity,
            "by_type": by_type,
            "backend_used": backend_name,
        }
        if snapshot_text:
            data["snapshot_length"] = len(snapshot_text)
        if screenshot_data:
            data["screenshot"] = screenshot_data

        return create_success_result(
            data=data,
            metadata={
                "adapter": "design_review",
                "backend": backend_name,
            },
        )
