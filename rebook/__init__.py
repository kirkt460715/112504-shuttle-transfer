"""山区接驳停运后的联程改签后端服务。

模块划分：

- ``domain``  纯领域规则（无 IO）：空间互斥、需求匹配、换乘判定、全程可兑现性
- ``store``   SQLite 台账与恢复
- ``carrier`` 承运方端口：HTTP 对端客户端与测试用内存实现
- ``app``     应用服务：编排领域规则、台账与承运方调用
- ``web``     面向旅客、售票台、承运方的 HTTP 接口
- ``loader``  从演练台账（registers.json）引导数据
"""

__all__ = ["domain", "store", "carrier", "app", "web", "loader"]
