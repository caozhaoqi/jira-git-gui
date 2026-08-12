# Jira Git 通用拉取工具（PyQt 桌面版）

针对 Jira Git Integration 插件（Xiplink / BigBrassBand）的通用桌面客户端。纯 Python +
PyQt6，无需浏览器，所有网络请求在后台线程执行，界面不卡顿。

## 两种模式

| 模式 | 认证 | 能力 | 局限 |
| --- | --- | --- | --- |
| **PAT 模式** | Personal Access Token | `git clone` 全量拉取（含嵌套文件），本地浏览/预览 | 需要该账号名下有效 PAT 与仓库名 |
| **Cookie 模式** | `JSESSIONID` 会话 | 浏览文件树、预览根目录文件、批量下载根目录文件 | 插件子目录列表 / 正文走前端 AJAX，无服务端接口，嵌套文件无法获取 |

> Cookie 模式下子目录点开可能为空，属已知限制；要拿全量请改用 PAT 模式克隆。

## 项目结构

```
jira-git-gui/
├── main.py                 # 入口：创建 QApplication + MainWindow
├── core/                   # 核心逻辑层（无 GUI 依赖，可独立测试）
│   ├── constants.py        # 目录 / 代理 / 超时
│   ├── models.py           # ConnectConfig / RepoInfo / TreeEntry
│   └── client.py           # JiraGitClient：connect / discover / list_level / get_file / clone / download
├── gui/                    # 界面层（PyQt6 组件）
│   ├── main_window.py      # 布局 + 信号绑定 + 异步任务编排
│   ├── connect_dialog.py   # 连接设置（地址/账号/模式/PAT/Cookie/仓库）
│   ├── repo_panel.py       # 发现仓库 / 手动指定仓库
│   ├── tree_panel.py       # 懒加载文件树（QTreeWidget）
│   ├── preview_panel.py    # 代码预览
│   └── log_panel.py        # 日志
├── workers/                # 异步任务层
│   └── tasks.py            # 通用 QThread Worker（自动注入 on_log 回调）
├── store/                  # 运行期产物（git 克隆 / 下载，已 gitignore）
└── requirements.txt
```

依赖方向：`gui → workers → core`，`core` 不反向依赖 GUI，便于单独复用与测试。

## 运行

```bash
# 1. 创建并激活虚拟环境
python3 -m venv venv
source venv/bin/activate

# 2. 安装依赖
pip install -r requirements.txt

# 3. 启动
python main.py
```

## 使用流程

1. 工具栏「连接设置」：填写 Jira 地址、用户名，选择 PAT 或 Cookie 模式，填入对应凭据，
   以及仓库 ID / 分支 / 仓库名。点「测试连接」可就地校验。
2. 仓库面板：若已配置 Cookie，可点「发现仓库」列出可读仓库并双击加载；或手动填写
   仓库 ID 后点「加载文件树」。
3. 文件树：展开目录懒加载子项；勾选文件「选择」列后可点「下载选中(Cookie)」。
4. 点击文件节点在右侧预览正文。
5. PAT 模式点「克隆仓库(PAT)」全量 clone 到 `store/repos/<repoId>/`，随后以本地模式浏览。

## 已知约束

- 当前提供的 PAT 若与登录账号不匹配（base64 前缀解出的账号 ≠ 登录账号），克隆会被 Jira 拒绝，
  需在该账号名下重新生成 PAT。
- Cookie 模式仅能获取根目录文件；子目录文件无服务端接口，需用 PAT 克隆。
- 运行时凭据仅存于内存，不写入磁盘；`store/` 已 gitignore。
