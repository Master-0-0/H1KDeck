# -*- coding: utf-8 -*-
"""Web 控制台不支持 Qt 图形扩展（havocui）。

图形化扩展依赖 Qt 客户端进程内的 Python UI 绑定，无法在网关子进程中运行。
导入该模块会立即给出明确错误，而不是让脚本在后续调用中神秘失败。
"""
raise ImportError(
    "Havoc Web 控制台不支持 havocui 图形扩展；请在 Qt 客户端使用该扩展。"
)
