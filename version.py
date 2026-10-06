# -*- coding: utf-8 -*-
"""工具版本与适配的游戏版本 —— 零依赖, GUI / 命令行 / 打包共用同一真源。

单独成文件的原因: patch_dll.py 顶部 import dnfile (第三方库), 而 GUI 靠
惰性导入保持"没装 dnfile 也能启动"。版本号若放进 patch_dll 会破坏这一点。
"""

__version__ = "1.0.1"
ADAPTED = ("2026-09", "2026-10")   # 本补丁支持的 IL 代次 (见 patch_dll 的 G1/G2)
