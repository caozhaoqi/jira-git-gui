# -*- coding: utf-8 -*-
"""Pydantic 请求模型（由 api/server.py 拆分而来）。"""

from pydantic import BaseModel

from core.client import DEFAULT_DOWNLOAD_WORKERS
from core.constants import DEFAULT_REQUEST_QPS


class ConnectReq(BaseModel):
    jira_url: str = ""
    username: str = ""
    mode: str = "pat"
    pat: str = ""
    cookie: str = ""
    repo_id: str = ""
    repo_name: str = ""
    branch: str = ""


class RepoSelectReq(BaseModel):
    repo_id: str
    repo_name: str = ""
    branch: str = ""


class CloneReq(BaseModel):
    repo_id: str = ""
    repo_name: str = ""
    branch: str = ""


class DownloadReq(BaseModel):
    paths: list[str]
    max_workers: int = DEFAULT_DOWNLOAD_WORKERS


class DownloadRepoReq(BaseModel):
    repo_id: str = ""
    branch: str = ""
    max_workers: int = DEFAULT_DOWNLOAD_WORKERS


class RateLimitReq(BaseModel):
    qps: int = DEFAULT_REQUEST_QPS


class CommitsReq(BaseModel):
    issue_key: str = ""
    local_mode: bool = False


class K8sSnapshotReq(BaseModel):
    namespace: str = ""
    selector: str = ""
    pod_filter: str = ""
    tail: int = 200
    restart_threshold: int = 5
    all_logs: bool = False
    include_previous: bool = False
    out_dir: str = ""
    kubeconfig: str = ""
    infile: str = ""
    env: str = ""   # 指定环境（开发/测试/正式）；优先于 kubeconfig/namespace
    log_level: str = "INFO"  # 日志级别: DEBUG/INFO/WARNING/ERROR


class K8sEnvReq(BaseModel):
    name: str
    label: str = ""
    kubeconfig: str = ""
    context: str = ""
    namespace: str = "default"
    intranet_hosts: list = []


class K8sYamlReq(BaseModel):
    env: str = ""
    kind: str = "pod"
    name: str = ""
    namespace: str = ""
    content: str = ""        # apply 时用
    action: str = "get"      # get | apply
    clean: bool = True       # get 时是否剔除 status/服务端字段


class K8sNetworkReq(BaseModel):
    env: str = ""
    extra_hosts: list = []


class K8sExecReq(BaseModel):
    env: str = ""
    pod: str = ""
    container: str = ""
    namespace: str = ""
    command: str = ""
    cwd: str = ""


class K8sFileListReq(BaseModel):
    env: str = ""
    pod: str = ""
    container: str = ""
    namespace: str = ""
    path: str = ""


class K8sFileReadReq(BaseModel):
    env: str = ""
    pod: str = ""
    container: str = ""
    namespace: str = ""
    path: str = ""
    max_bytes: int = 200000


class K8sFileSearchReq(BaseModel):
    env: str = ""
    pod: str = ""
    container: str = ""
    namespace: str = ""
    q: str = ""
    path: str = ""


class K8sFileWriteReq(BaseModel):
    env: str = ""
    pod: str = ""
    container: str = ""
    namespace: str = ""
    path: str = ""
    content: str = ""
    encoding: str = ""      # 'base64' 表示 content 已 base64 编码（二进制）


class K8sFileUploadReq(BaseModel):
    env: str = ""
    pod: str = ""
    container: str = ""
    namespace: str = ""
    path: str = ""
    data: str = ""          # base64 编码的二进制内容


class K8sFileDeleteReq(BaseModel):
    env: str = ""
    pod: str = ""
    container: str = ""
    namespace: str = ""
    path: str = ""
    is_dir: bool = False


class K8sFileMkdirReq(BaseModel):
    env: str = ""
    pod: str = ""
    container: str = ""
    namespace: str = ""
    path: str = ""


class CfLogReq(BaseModel):
    server_url: str = ""
    token: str = ""
    log_type: str = ""
    page_index: int = 1
    page_size: int = 200
    proxy: str = ""  # 代理地址，如 http://127.0.0.1:7890 或 socks5://127.0.0.1:7891


class CfLogExportReq(BaseModel):
    server_url: str = ""
    log_type: str = ""
    auth_method: str = ""  # 实际生效的认证方式
    page_index: int = 1
    page_size: int = 200
    total: int = 0
    rows: list = []  # 日志记录数组
    raw: object = None  # 原始响应（可选）


class CfLoginReq(BaseModel):
    server_url: str = ""
    mobile: str = ""
    password: str = ""
    proxy: str = ""
    image_code: str = ""  # 图片验证码（用户输入）
    image_code_index: str = ""  # 验证码索引，与拉取验证码图片时一致
    captcha_id: str = ""  # 后端返回的验证码会话ID（关联 httpx cookie jar）


class CfCaptchaReq(BaseModel):
    server_url: str = ""
    proxy: str = ""


# CF 验证码会话缓存：captcha_id -> {"jar": cookie jar, "index": image_code_index}
_CF_CAPTCHA_CACHE: dict[str, object] = {}
_CF_CAPTCHA_TTL: dict[str, float] = {}


class ClipboardSaveReq(BaseModel):
    text: str = ""
    filename: str = ""  # 可选文件名，留空则自动生成


