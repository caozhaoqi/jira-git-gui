# -*- coding: utf-8 -*-
"""Pydantic 请求模型（由 api/server.py 拆分而来）。"""

from typing import Optional

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
    keyword: str = ""  # 客户端过滤关键字（导出时带入，便于追溯这是过滤后的日志）
    filtered: bool = False  # 标记 rows 是否已是客户端过滤后的结果


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


class CfAutoLoginReq(BaseModel):
    proxy: str = ""  # 可选代理，如 http://127.0.0.1:7890


# CF 验证码会话缓存：captcha_id -> {"jar": cookie jar, "index": image_code_index}
_CF_CAPTCHA_CACHE: dict[str, object] = {}
_CF_CAPTCHA_TTL: dict[str, float] = {}


class ClipboardSaveReq(BaseModel):
    text: str = ""
    filename: str = ""  # 可选文件名，留空则自动生成


class CfDiagnoseReq(BaseModel):
    """云函数错误诊断请求：粘贴错误文本，返回聚合后的诊断上下文。"""
    text: str = ""              # 错误原文（必填）
    server_url: str = ""        # 网关地址（用于取缓存 token 判断健康度）
    token: str = ""             # 直接传 token（优先于 server_url 缓存）
    model: str = ""             # 可选，覆盖解析结果
    object_id: str = ""         # 可选，覆盖解析结果
    field: str = ""             # 可选，覆盖解析结果
    max_docs: int = 3           # 最多返回几篇 Wiki 片段
    max_chars: int = 1500       # 每篇片段最大字符数
    case_limit: int = 5         # 最多返回几条历史相似案例
    current_value: str = ""     # 前端已查询到的当前字段值（已脱敏）
    current_present: Optional[bool] = None  # 当前字段是否有值


class CfCaseSaveReq(BaseModel):
    """保存诊断案例到 logs/cf_cases/。"""
    content: str = ""           # 案例正文（Markdown）
    errcode: str = ""           # 用于文件名，如 17003
    log_type: str = ""          # 用于文件名，如 daily_overtime
    source: str = "manual"      # manual / panel / ai


class CfCaseFeedbackReq(BaseModel):
    """记录 AI 诊断是否正确，驱动案例库和准确率迭代。"""
    case_file: str = ""         # logs/cf_cases/ 下的文件名或相对路径
    result: str = ""             # correct / partially_correct / wrong / unknown
    actual_root_cause: str = ""  # 人工确认的根因分类
    fix_applied: Optional[bool] = None
    notes: str = ""
    source: str = "manual"      # manual / panel / ai


class CfFeedbackLearnReq(BaseModel):
    """诊断→规范闭环请求：根据反馈反哺词典与路由索引。"""
    apply: bool = False         # false=仅预览提案；true=备份后回写 errdict.json + 路由索引
    max_proposals: int = 100    # 单次最多处理的反馈样本数


class UnifiedDiagnoseReq(BaseModel):
    """统一诊断请求：CF 云函数错误 + K8s 基础设施诊断。

    粘贴错误文本 + 选择 K8s 环境，一次调用拿到应用层和基础设施层的联合诊断上下文。
    """
    # --- CF 诊断参数（同 CfDiagnoseReq） ---
    text: str = ""              # 错误原文（必填）
    server_url: str = ""        # HCM 网关地址
    token: str = ""             # 直接传 token
    model: str = ""             # 可选，覆盖解析结果
    object_id: str = ""         # 可选，覆盖解析结果
    field: str = ""             # 可选，覆盖解析结果
    max_docs: int = 3           # 最多返回几篇 Wiki 片段
    max_chars: int = 1500       # 每篇片段最大字符数
    case_limit: int = 5         # 最多返回几条历史相似案例
    current_value: str = ""     # 前端已查询到的当前字段值（已脱敏）
    current_present: Optional[bool] = None  # 当前字段是否有值

    # --- K8s 诊断参数 ---
    k8s_env: str = ""           # K8s 环境名（留空则跳过 K8s 诊断）
    k8s_namespace: str = ""     # 命名空间（留空用环境默认）
    k8s_pod_filter: str = ""    # Pod 名称过滤（模糊匹配）
    k8s_tail: int = 100         # 日志行数限制


class FullDiagnoseReq(UnifiedDiagnoseReq):
    """一键诊断请求：CF + K8s + 远程 dynamic_log + JSON 元数据 + AI 编码规范。"""
    # --- dynamic_log 采集参数 ---
    dynamic_log_enabled: bool = True
    dynamic_log_type: str = ""       # 留空时优先使用错误文本解析出的 log_type
    dynamic_log_page_index: int = 1
    dynamic_log_page_size: int = 200
    dynamic_log_keyword: str = ""     # 客户端关联前的可选关键词
    proxy: str = ""                   # 查询 HCM dynamic_log 的代理

    # --- 元数据输入 ---
    metadata: dict = {}               # 请求内联 JSON 元数据（模型/字段/关系/schema）
    metadata_files: list = []         # 项目目录或 reference 目录下的 JSON 文件路径
    include_coding_rules: bool = True
    coding_rules_max_chars: int = 12000

    # --- 返回控制 ---
    dynamic_log_match_limit: int = 20
    k8s_time_window_minutes: int = 15 # 预留给后续按时间窗增强 K8s 采集


