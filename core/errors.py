"""可预期的用户操作类异常。

与「未捕获的 Bug」区分：``UserError`` 表示「用户配置 / 输入 / 会话」层面的
可预期问题（缺配置、会话过期、输入不合法、功能不支持等）。Worker 会把这类异常
以 WARNING 级别记录（不带完整 traceback），并只把消息文本上抛给 UI 提示；
其余 ``Exception`` 仍按 ERROR + 完整 traceback 处理，便于追溯真正的代码缺陷。
"""


class UserError(Exception):
    """用户可预期的操作提示（配置缺失 / 会话过期 / 输入不合法等）。"""
    pass
