"""pipeline 兼容包：使 0.1 式 ``from pipeline.plugin/types import ...`` 在 0.2 可解析。

0.2 架构下各 pipeline 插件的 server.py 把 plugins/shared/ 加入 sys.path，
再从本地 plugin.py 平铺 import。本 __init__.py + plugin.py/types.py re-export
shim 让遗留的 ``from pipeline.X import`` 也能解析（_base/plugin.py 内部用绝对
``from pipeline.types import``，故必须把 pipeline 做成真包）。
"""
