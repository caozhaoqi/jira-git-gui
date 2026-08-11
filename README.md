# Jira Git 插件通用拉取工具（GUI）

一个本地运行的图形化工具，用于从 **Jira Git Integration 插件**（`GIJBrowseGit.jspa` 等）托管的仓库里
**浏览文件树、查看/下载源码、克隆整个仓库**。

适用于：公司内网 Jira + BigBrassBand「Git Integration for Jira」插件场景，普通 Jira 账号密码 / 会话
无法直接 `git clone`（插件要求 PAT），但你有浏览器登录态或 Personal Access Token 的情况。

---

## 架构

单进程 **FastAPI** 后端同时托管前端网页，浏览器打开 `http://localhost:8787` 即用，零前端构建、跨平台。
后端代理所有 Jira 请求，规避浏览器 CORS 与鉴权难题，并对代理下的偶发 SSL 抖动做了重试。

```
jira-git-gui/
├── server.py          # FastAPI 后端 + 静态托管 + 全部 /api 接口
├── index.html         # 单页前端（原生 JS + 内联 CSS，浅色卡片式）
├── requirements.txt   # fastapi / uvicorn / httpx
├── store/             # 本地数据（已被 .gitignore 忽略）
│   ├── repos/<repoId>/     # PAT 模式 git clone 落盘处
│   └── downloads/<repoId>/ # Cookie 模式下载落盘处
└── venv/              # 虚拟环境（忽略）
```

---

## 两种模式

| 能力 | PAT 模式 (`git clone`) | Cookie 模式 (Web 抓取) |
|---|---|---|
| 浏览文件树（含子目录，懒加载） | ✅ 本地读，秒开 | ⚠️ 仅根目录层（插件子目录内容纯前端 AJAX，无服务端列表接口） |
| 读取/预览文件 | ✅ 全量 | ⚠️ 仅根目录文件（`.json` 走 JSP 提取） |
| 下载文件到本地 | ✅ 全量 | ⚠️ 仅根目录文件 |
| 完整 git 历史 | ✅ | ❌ |

> **结论**：PAT 模式是「全量」主路径；Cookie 模式适合「只有浏览器登录态、没有 PAT」时快速看一眼根目录配置
> （README、package.json、Dockerfile、webpack 配置等）。嵌套源码请用 PAT 模式克隆。

---

## 使用步骤

1. **安装依赖**
   ```bash
   python3 -m venv venv
   ./venv/bin/pip install -r requirements.txt
   ```

2. **启动**
   ```bash
   ./venv/bin/python server.py
   # 打开 http://localhost:8787
   ```

3. **填连接信息**
   - Jira 基址，如 `https://jira.hcmcloud.cn`
   - 用户名
   - 鉴权模式：
     - **PAT**：填入 Personal Access Token（在 Jira 个人设置 → Personal Access Tokens 创建，**用你的登录账号创建**）
     - **Cookie**：填入浏览器里的 `JSESSIONID=...; atlassian.xsrf.token=...`（F12 → Application → Cookies 复制）
   - 点「测试连接」

4. **选仓库**
   - 填 `repoId`（插件仓库数字 ID，如 `1032`）、分支（如 `cherry-pick-36e0626c`）
   - PAT 模式还需仓库名（`cloneUrl` 里的那一段，如 `hcm_cloud`，Cookie 模式可点「自动探测」）

5. **浏览 / 拉取**
   - 「加载文件树」→ 树形展开目录、点文件预览
   - PAT 模式点「PAT 克隆」全量拉到 `store/repos/<repoId>/`
   - 勾选文件 → 「下载所选」存到 `store/downloads/<repoId>/`

---

## 接口一览

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/connect` | 存凭据 + 测连通，返回 `{cookieOk, patProvided, repoDefaults}` |
| GET  | `/api/status` | 当前会话状态 |
| GET  | `/api/repos`   | 尝试发现仓库列表（多数部署无此 REST，回退手动 repoId） |
| GET  | `/api/tree?path=` | 单层目录列表（懒加载，前端展开时按需请求子层） |
| GET  | `/api/file?path=` | 文件正文（PAT 读本地 / Cookie 抓 Web） |
| POST | `/api/clone`  | PAT 克隆到 `store/repos/<repoId>/` |
| POST | `/api/download` | Cookie 模式批量下载所选文件 |

---

## 已知限制 / 排错

- **必须用 PAT 才能 `git clone`**：Jira 登录密码 / 会话 cookie 对 git smart-HTTP 端点无效
  （返回 401/403，多次失败还会触发验证码锁）。PAT 要在**当前登录账号**下创建。
- **PAT 账号不匹配**：若 PAT 前缀 base64 解码出的账号 ID 与你填的用户名不同，克隆会失败，
  后端会尝试用 PAT 内嵌账号 ID 作为 git 用户名再试一次；仍失败请确认 PAT 归属与有效性。
- **Cookie 模式子目录为空**：插件对子目录文件列表只走前端 AJAX，无服务端接口，故 Cookie 模式
  只能浏览根层、读取根文件。需要嵌套源码请用 PAT 克隆。
- **代理环境**：后端会读取 `HTTPS_PROXY/HTTP_PROXY` 环境变量走代理；每次请求新建客户端并自带重试，
  以对抗代理偶发的 `SSL UNEXPECTED_EOF`。
- **会话过期**：`JSESSIONID` 有时效，过期后 Cookie 模式失效，重新从浏览器复制新的即可。

---

## 技术备注（踩坑记录）

- 插件 git 端点 `https://<host>/git/<repoId>/<repoName>.git` 仅认 PAT（Basic）；`/` 在令牌里需编码为 `%2F`。
- 文件查看接口：`/secure/GIJBrowseGit.jspa`（树）、`/secure/GIJViewGitFileContent.jspa?revision=&repoId=&path=`（内容）、
  REST 裸文件 `/rest/gitplugin/1.0/files/<repoId>/<revision>/<path>`（仅根文本文件，不支持多级路径）。
- `ns.repoInfo.lastCommit.name` 含分支 HEAD commit；`ns.data.files` 为当前目录条目（仅根目录服务端渲染）。
