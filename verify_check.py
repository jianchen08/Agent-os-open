#!/usr/bin/env python3
"""resume.html 静态结构验证脚本"""
import re
import json

with open('resume.html', 'r', encoding='utf-8') as f:
    content = f.read()

results = {}

def check(name, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    results[name] = {"status": status, "detail": detail}
    icon = "✅" if condition else "❌"
    print(f"  {icon} {name}: {detail}")

print("=" * 60)
print("【1】HTML文件基本结构")
print("=" * 60)
check("DOCTYPE", content.strip().startswith('<!DOCTYPE html>'), "HTML5文档类型声明")
check("UTF-8编码", 'charset="UTF-8"' in content, "UTF-8编码声明")
check("中文语言", 'lang="zh-CN"' in content, "zh-CN语言设置")
title_m = re.search(r'<title>(.*?)</title>', content)
check("页面标题", bool(title_m), title_m.group(1) if title_m else "缺失")
check("内嵌CSS", '<style>' in content and '</style>' in content, "CSS内嵌在HTML中")
check("内嵌JS", '<script>' in content and '</script>' in content, "JS内嵌在HTML中")
ext_css = [l for l in re.findall(r'<link[^>]+href=["\']([^"\']+)', content) if l.endswith('.css')]
check("无外部CSS", len(ext_css) == 0, "纯内嵌CSS，无外部CSS文件")

print()
print("=" * 60)
print("【2】照片验证")
print("=" * 60)
img_m = re.search(r'<img\s+class="photo"[^>]*src="([^"]+)"', content)
if img_m:
    src = img_m.group(1)
    check("照片标签", True, "class=photo 的 img 标签存在")
    check("Base64内嵌", src.startswith('data:image/'), f"data:image/jpeg base64内嵌, 数据长度{len(src)}字符")
else:
    check("照片标签", False, "未找到photo img标签")

photo_css = re.search(r'\.photo\s*\{([^}]+)\}', content)
if photo_css:
    css = photo_css.group(1)
    check("圆形裁剪", 'border-radius: 50%' in css, "border-radius: 50% 实现圆形裁剪")
    w_m = re.search(r'width:\s*(\d+)', css)
    h_m = re.search(r'height:\s*(\d+)', css)
    w_val = w_m.group(1) if w_m else "?"
    h_val = h_m.group(1) if h_m else "?"
    check("照片尺寸", True, f"{w_val} x {h_val}")
else:
    check("照片CSS", False, "未找到.photo样式")

print()
print("=" * 60)
print("【3】contenteditable 可编辑验证")
print("=" * 60)
editable_count = len(re.findall(r'contenteditable="true"', content))
check("可编辑元素", editable_count > 0, f"共 {editable_count} 个可编辑元素")

editable_checks = [
    ("姓名可编辑", r'class="name"[^>]*contenteditable="true"'),
    ("职位可编辑", r'class="title"[^>]*contenteditable="true"'),
    ("联系方式可编辑", r'contact.*?contenteditable="true"'),
    ("项目经历可编辑", r'项目经历.*?contenteditable="true"'),
    ("工作经历可编辑", r'工作经历.*?contenteditable="true"'),
    ("教育背景可编辑", r'教育背景.*?contenteditable="true"'),
    ("技能可编辑", r'技能.*?contenteditable="true"'),
    ("自我评价可编辑", r'自我评价.*?contenteditable="true"'),
]
for name, pattern in editable_checks:
    found = bool(re.search(pattern, content, re.DOTALL))
    check(name, found, "已标记contenteditable" if found else "未标记")

# 编辑hover/focus样式
check("编辑hover样式", ':hover' in content and 'outline-color' in content, "hover时显示虚线边框")
check("编辑focus样式", ':focus' in content and 'outline-color' in content, "focus时高亮显示")

print()
print("=" * 60)
print("【4】导出PDF按钮验证")
print("=" * 60)
btn_m = re.search(r'<button[^>]*class="btn-export"[^>]*>(.*?)</button>', content)
check("导出按钮存在", bool(btn_m), f"按钮文本: {btn_m.group(1) if btn_m else 'N/A'}")
check("exportPDF函数", 'function exportPDF()' in content, "PDF导出函数已定义")
check("html2pdf库", 'html2pdf' in content, "引入html2pdf.js库")
cdn_m = re.search(r'html2pdf\.js/([\d.]+)/html2pdf\.bundle\.min\.js', content)
check("html2pdf CDN", bool(cdn_m), f"版本: {cdn_m.group(1) if cdn_m else 'N/A'}")
check("A4格式", "format: 'a4'" in content or 'format: "a4"' in content, "jsPDF format设为a4")
check("纵向布局", "orientation: 'portrait'" in content, "portrait纵向布局")

print()
print("=" * 60)
print("【5】导出时隐藏编辑状态")
print("=" * 60)
check("printing CSS类", '.printing' in content, ".printing类用于PDF导出状态")
check("隐藏工具栏", '.printing .toolbar' in content and 'display: none' in content, "导出时隐藏toolbar")
check("去除编辑样式", '.printing [contenteditable' in content, "导出时去除outline和background")
check("导出前添加类", "classList.add('printing')" in content, "导出前添加printing类")
check("导出后移除类", "classList.remove('printing')" in content, "导出完成后移除printing类")

print()
print("=" * 60)
print("【6】简历内容完整性")
print("=" * 60)
sections = ['项目经历', '工作经历', '教育背景', '技能', '自我评价']
for s in sections:
    check(f"区块: {s}", s in content, f"在页面中出现{content.count(s)}次")

print("\n  --- 具体内容 ---")
details = [
    ("姓名", "陈健"),
    ("职位", "AI Agent"),
    ("手机号", "15116047185"),
    ("邮箱", "chenjian1306792950@foxmail.com"),
    ("项目: 灵汐Agent系统", "灵汐"),
    ("项目: 太阳能吸波器", "太阳能"),
    ("项目: 光学陀螺仪", "陀螺仪"),
    ("公司: 振华集团", "振华"),
    ("公司: 托克逊", "托克逊"),
    ("学历: 国防科技大学", "国防科技大学"),
    ("学历: 湖南工业大学", "湖南工业大学"),
    ("技能: Python", "Python"),
    ("技能: React", "React"),
    ("技能: TypeScript", "TypeScript"),
    ("技能: FastAPI", "FastAPI"),
    ("技能: Docker", "Docker"),
    ("技能: Multi-Agent", "Multi-Agent"),
]
for name, kw in details:
    check(name, kw in content, "内容存在" if kw in content else "缺失")

print()
print("=" * 60)
print("【7】A4页面排版控制")
print("=" * 60)
check("A4宽度(210mm)", 'width: 210mm' in content, "resume-page宽度210mm")
check("A4高度(297mm)", 'min-height: 297mm' in content, "resume-page最小高度297mm")
check("@page设置", '@page' in content and 'size: A4' in content, "@page size:A4 margin:0")
check("打印色彩保留", 'print-color-adjust: exact' in content, "print-color-adjust: exact")

print()
print("=" * 60)
print("【8】设计风格")
print("=" * 60)
check("深蓝主色(#1a2a4a)", '#1a2a4a' in content, "用于标题、边框等")
check("藏青渐变(#2c3e6b)", '#2c3e6b' in content, "用于toolbar背景")
check("蓝色强调(#2980d4)", '#2980d4' in content, "用于section边框、链接等")
check("渐变效果", 'linear-gradient' in content, "toolbar和按钮使用渐变")
check("阴影效果", 'box-shadow' in content, "卡片阴影增加层次感")
check("圆角设计", 'border-radius' in content, "按钮、照片圆角")
check("过渡动画", 'transition' in content, "交互过渡效果")
check("微软雅黑字体", 'Microsoft YaHei' in content, "中文优先字体")

print()
print("=" * 60)
print("【9】自动保存功能 (localStorage)")
print("=" * 60)
check("localStorage", 'localStorage' in content, "使用localStorage持久化编辑数据")
check("自动保存", 'setItem' in content, "编辑时自动保存")
check("数据恢复", 'getItem' in content, "刷新后恢复编辑内容")
check("防抖处理(300ms)", 'setTimeout' in content and 'clearTimeout' in content and '300' in content, "300ms防抖")

print()
print("=" * 60)
print("【10】工具栏与用户提示")
print("=" * 60)
check("工具栏", 'class="toolbar"' in content, "顶部固定工具栏")
check("工具栏标题", 'toolbar-title' in content, "简历编辑器标题")
hint_m = re.search(r'class="toolbar-hint"[^>]*>([^<]+)', content)
check("编辑提示", bool(hint_m), hint_m.group(1) if hint_m else "N/A")
check("工具栏fixed定位", 'position: fixed' in content, "工具栏固定在顶部")

print()
print("=" * 60)
print("统计汇总")
print("=" * 60)
total = len(results)
passed = sum(1 for v in results.values() if v["status"] == "PASS")
failed = sum(1 for v in results.values() if v["status"] == "FAIL")
print(f"  总检查项: {total}")
print(f"  通过: {passed}")
print(f"  失败: {failed}")
print(f"  通过率: {passed/total*100:.1f}%")

# 保存结果为JSON
with open('verify_results.json', 'w', encoding='utf-8') as f:
    json.dump({"total": total, "passed": passed, "failed": failed, "details": results}, f, ensure_ascii=False, indent=2)
print("\n结果已保存到 verify_results.json")
