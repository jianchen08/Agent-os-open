"""skill_manager 插件包标记。

pytest 收集面与邻插件同名测试件共存：无本标记时裸名模块冲突（import file
mismatch），标记后按包限定名收集互不串扰。sidecar/合宿加载走路径 exec，
不受本标记影响。
"""
