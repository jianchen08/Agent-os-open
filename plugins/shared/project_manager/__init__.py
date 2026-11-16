"""project_manager 插件包标记（项目域主，T6a：project_create + project_state 合并）。

pytest 收集面与邻插件同名测试件共存：无本标记时裸名模块冲突（import file
mismatch），标记后按包限定名收集互不串扰。sidecar/合宿加载走路径 exec，
不受本标记影响。私有实现模块（project_registry / 两工具）按包限定名导入，
与共享根迁移期过渡件 plugins/shared/project_registry.py 裸名空间隔离。
"""
