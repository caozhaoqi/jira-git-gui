"""核心逻辑层：纯 Python，无任何 GUI 依赖。

包含：
- constants : 路径、代理、超时等运行时常量
- models    : 数据模型（dataclass）
- client    : JiraGitClient，封装所有对 Jira Git 插件的网络/解析/克隆/下载操作
"""
