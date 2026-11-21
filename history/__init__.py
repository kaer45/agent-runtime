"""turn 级历史：存储、压缩、摘要与共享指标。

本包故意不 re-export 子模块：``history.compactor`` 依赖 ``context.models``，
而 ``context.models`` 又依赖 ``history.models``；如果在包的 ``__init__`` 里
把 ``compactor`` 导出来，``import context`` 和 ``import history.compactor``
会形成包级循环。调用方直接 ``from history.<module> import ...``。
"""
