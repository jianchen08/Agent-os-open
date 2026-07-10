"""generate_task_chain_html.py 的单元测试。

设计原则：
- 纯标准库（unittest），与被测脚本「无第三方依赖」风格一致。
- 覆盖两类：① 稳定行为（重构前后都应通过的回归保护）；
  ② 修复点（XSS/AC 语义/环报错/异常警告，修复前红、修复后绿）。
"""

from __future__ import annotations

import contextlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import generate_task_chain_html as g  # noqa: E402


class TestParseFrontmatter(unittest.TestCase):
    def test_no_frontmatter(self):
        fm, body = g.parse_frontmatter("hello world")
        self.assertEqual(fm, {})
        self.assertEqual(body, "hello world")

    def test_scalar_fields(self):
        text = "---\ntask_id: T1\ntask_name: Foo\nstatus: done\n---\nbody here"
        fm, body = g.parse_frontmatter(text)
        self.assertEqual(fm["task_id"], "T1")
        self.assertEqual(fm["status"], "done")
        self.assertEqual(body.strip(), "body here")

    def test_list_field(self):
        fm, _ = g.parse_frontmatter("---\ndepends_on:\n- T1\n- T2\n---\n")
        self.assertEqual(fm["depends_on"], ["T1", "T2"])

    def test_comments_ignored(self):
        fm, _ = g.parse_frontmatter("---\n# comment\ntask_id: T1\n---\n")
        self.assertEqual(fm.get("task_id"), "T1")


class TestLoadTasks(unittest.TestCase):
    def _write(self, d: Path, name: str, content: str) -> Path:
        p = d / name
        p.write_text(content, encoding="utf-8")
        return p

    def test_sort_by_filename(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            self._write(d, "task_02_b.md", "---\ntask_id: T2\n---\n# B\n")
            self._write(d, "task_01_a.md", "---\ntask_id: T1\n---\n# A\n")
            tasks = g.load_tasks(d)
            self.assertEqual([t.task_id for t in tasks], ["T1", "T2"])

    def test_depends_on_string_form(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            self._write(d, "task_01.md", '---\ntask_id: T1\ndepends_on: "[T0, T00]"\n---\n')
            self.assertEqual(g.load_tasks(d)[0].depends_on, ["T0", "T00"])

    def test_ac_stats_normalized(self):  # S1
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            body = "AC-1 内容；traces_to: AC-1；traces_to：AC-1（中文冒号）；参考 AC-2"
            self._write(d, "task_01.md", f"---\ntask_id: T1\n---\n{body}\n")
            t = g.load_tasks(d)[0]
            # 中英文冒号不重复计数；与 ac_ids 同为纯编号，二者可比
            self.assertEqual(t.ac_traces, ["AC-1"])
            self.assertEqual(t.ac_ids, ["AC-1", "AC-2"])

    def test_read_failure_warns_and_skips(self):  # S3
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "task_01.md").write_bytes(b"\xff\xfe\x00bad")  # 非 UTF-8
            with contextlib.redirect_stderr(io.StringIO()) as buf:
                tasks = g.load_tasks(d)
            self.assertEqual(tasks, [])
            self.assertIn("task_01.md", buf.getvalue())


class TestComputeLayers(unittest.TestCase):
    def _t(self, tid: str, deps: list[str] | None = None) -> g.Task:
        t = g.Task(task_id=tid)
        t.depends_on = deps or []
        return t

    def test_linear(self):
        layers = g.compute_layers([self._t("A"), self._t("B", ["A"]), self._t("C", ["B"])])
        self.assertEqual([layers["A"], layers["B"], layers["C"]], [0, 1, 2])

    def test_diamond(self):
        layers = g.compute_layers(
            [self._t("A"), self._t("B", ["A"]), self._t("C", ["A"]), self._t("D", ["B", "C"])]
        )
        self.assertEqual(layers["D"], 2)

    def test_cycle_no_crash_and_reports(self):  # S5
        with contextlib.redirect_stderr(io.StringIO()) as buf:
            layers = g.compute_layers([self._t("A", ["B"]), self._t("B", ["A"])])
        self.assertIn("A", layers)
        self.assertIn("B", layers)
        self.assertIn("环", buf.getvalue())


class TestMarkdown(unittest.TestCase):
    def test_codeblock_escapes_script_tag(self):
        out = g.markdown_to_html("```\n</script><script>alert(1)\n```")
        self.assertIn("&lt;/script&gt;", out)

    def test_headings_lists(self):
        out = g.markdown_to_html("# T\n- a\n- b\n1. c\n")
        self.assertIn("<h1>", out)
        self.assertIn("<ul>", out)
        self.assertIn("<ol>", out)

    def test_inline(self):
        out = g.markdown_to_html("`x` and **b**")
        self.assertIn("<code>x</code>", out)
        self.assertIn("<strong>b</strong>", out)

    def test_gfm_table(self):
        md = "| 名称 | 值 |\n| --- | ---: |\n| a | 1 |\n| b | 2 |\n"
        out = g.markdown_to_html(md)
        self.assertIn("<table>", out)
        self.assertIn("<thead>", out)
        self.assertIn("<th", out)
        # 对齐标记行（| --- | ---: |）不应原样出现在输出中
        self.assertNotIn("---", out)
        # 单元格内容被渲染
        self.assertIn("名称", out)
        self.assertIn(">1<", out)

    def test_table_not_triggered_by_loose_pipe(self):
        # 单行管道符（无分隔行、无多行）不应误判为表格
        out = g.markdown_to_html("这是一句话 | 中间有个竖线")
        self.assertNotIn("<table>", out)


class TestRenderHtml(unittest.TestCase):
    def _write_task(self, d: Path, name: str, task_id: str, body: str = "正文内容") -> None:
        (d / name).write_text(
            f"---\ntask_id: {task_id}\ntask_name: 名字\n---\n{body}\n", encoding="utf-8"
        )

    def test_xss_in_task_id_neutralized(self):  # M1
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            self._write_task(d, "task_01.md", "x');alert(1);//")
            out = Path(td) / "o.html"
            g.render_html("P", g.load_tasks(d), [], out)
            html_text = out.read_text(encoding="utf-8")
            # 不再有任何内联 onclick="showTask(...)" —— 改用 data 属性 + 事件委托
            self.assertNotIn('onclick="showTask', html_text)
            self.assertIn("data-task-id=", html_text)
            # 危险字符（单引号）在属性值里必须被转义，无法脱离属性
            self.assertIn("&#x27;", html_text)

    def test_script_injection_in_body_neutralized(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            body = "```\n</script><script>alert('pwn')</script>\n```"
            self._write_task(d, "task_01.md", "T1", body=body)
            out = Path(td) / "o.html"
            g.render_html("P", g.load_tasks(d), [], out)
            html_text = out.read_text(encoding="utf-8")
            self.assertNotIn("</script><script>", html_text)

    def test_end_to_end_valid_html(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            self._write_task(d, "task_01.md", "T1", body="# 标题\n- 项\n")
            (d / "doc.md").write_text("# 项目说明\n正文\n", encoding="utf-8")
            out = Path(td) / "o.html"
            g.render_html("我的项目", g.load_tasks(d), g.load_project_docs(d), out)
            html_text = out.read_text(encoding="utf-8")
            self.assertTrue(html_text.lstrip().startswith("<!DOCTYPE html>"))
            self.assertIn("我的项目", html_text)
            self.assertIn("名字", html_text)
            self.assertIn("data-task-id=", html_text)
            self.assertIn("项目说明", html_text)


class TestCli(unittest.TestCase):
    def test_main_generates_file(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "task_01.md").write_text("---\ntask_id: T1\n---\n# A\n", encoding="utf-8")
            out = Path(td) / "o.html"
            old_argv = sys.argv
            sys.argv = [
                "generate_task_chain_html.py",
                "--title", "演示",
                "--tasks-dir", str(d),
                "--project-dir", str(Path(td) / "empty"),
                "--output", str(out),
            ]
            try:
                rc = g.main()
            finally:
                sys.argv = old_argv
            self.assertEqual(rc, 0)
            self.assertTrue(out.exists())

    def test_title_sanitized_in_default_output(self):  # N2
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "task_01.md").write_text("---\ntask_id: T1\n---\n# A\n", encoding="utf-8")
            old_argv, old_cwd = sys.argv, os.getcwd()
            os.chdir(td)
            sys.argv = [
                "generate_task_chain_html.py",
                "--title", "../../evil",
                "--tasks-dir", str(d),
                "--project-dir", str(Path(td) / "empty"),
            ]
            try:
                g.main()
            finally:
                sys.argv = old_argv
                os.chdir(old_cwd)
            # 默认输出：路径分隔符被替换为 _，不再逃逸目录
            produced = list(Path(td).rglob("*_task_chain.html"))
            self.assertTrue(produced, "应生成 HTML")
            self.assertTrue(all("../../" not in str(p) for p in produced))


if __name__ == "__main__":
    unittest.main()
