"""depin_experiments：GPU 计量可行性实验（issue #13 / spec §7.3）。

约束：
- 本包导入不依赖 torch（torch 只在实验函数内延迟导入）；
- 纯逻辑（解析、汇总、校验）与副作用（跑负载、调 ncu）分离，
  前者可在无 GPU 机器上单测。
"""

__version__ = "0.1.0"
