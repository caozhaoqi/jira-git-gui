# -*- coding: utf-8 -*-
"""
云函数调试启动器（api/cfdebug/launcher.py）
================================================================
由后端以 `python -m debugpy --listen 127.0.0.1:<dap_port> --wait-for-client
<本文件> --cf-file ... --cf-kwargs ...` 方式启动。

本文件本身【不】调用 debugpy——debugpy 在外层包裹它（server/attach 模式），
负责在断点处暂停并把变量/调用栈经 DAP 暴露给前端。这里只负责：
  - 解析入参
  - 按 env 装配 CustomerUtil（Mock / 真实 HCM）
  - 装配 DB（可选，带 DDL 拦截与落库开关）
  - 用 cfdebug.loader 的 run_model_a / run_model_b 真正执行云函数 execute()
  - 把执行结果 / 异常打印到 stdout（后端读取后经 SSE 推给前端日志区）

co_filename 使用真实磁盘路径，因此 IDE/debugpy 断点能精确命中。
"""
import argparse
import json
import logging
import os
import sys
import traceback
from pathlib import Path

# 把项目根加入 sys.path，使本脚本（以脚本形式运行）也能 import api.cfdebug.*
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from api.cfdebug import loader                       # noqa: E402
from api.cfdebug.mock_data import MockCustomerUtil  # noqa: E402
from api.cfdebug.real_client import RealCustomerUtil  # noqa: E402


def _setup_logging():
    # 统一输出到 stdout，便于后端按行采集（running / error / debug 日志同源）
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        stream=sys.stdout,
        force=True,
    )


def main():
    _setup_logging()
    ap = argparse.ArgumentParser(description="HCM cloud function debug launcher")
    ap.add_argument("--cf-file", required=True, help="云函数 .py 绝对路径")
    ap.add_argument("--cf-kwargs", default="{}", help="execute() 入参 JSON 字符串")
    ap.add_argument("--cf-env", default="mock", choices=["mock", "test", "custom"],
                    help="调试环境：mock / test / custom")
    ap.add_argument("--cf-server", default="", help="test/custom 环境 HCM 网关 base url")
    ap.add_argument("--cf-token", default="", help="test/custom 环境 token（cookie 形式）")
    ap.add_argument("--cf-db-url", default="", help="直连 DB 的 SQLAlchemy URL（可选）")
    ap.add_argument("--cf-company-id", default="1", help="company_id（context.company.id）")
    ap.add_argument("--cf-debug-id", default="", help="Mock 模式收窄到单条记录的 id")
    ap.add_argument("--cf-allow-ddl", default="0", help="1=允许 DDL（仅测试库）")
    ap.add_argument("--cf-db-save", default="0", help="1=真正提交 DB 事务")
    ap.add_argument("--cf-write-real", default="0",
                    help="1=真实环境也真正执行写接口（默认 dry_run 只登记）")
    ap.add_argument("--cf-entry", default="", help="入口类名（默认取首个顶层类）")
    ap.add_argument("--cf-dap-port", default="", help="debugpy DAP 监听端口（由后端分配）")
    args = ap.parse_args()

    cf_file = args.cf_file
    logging.info("[launcher] 云函数文件: %s", cf_file)
    logging.info("[launcher] 调试环境: %s", args.cf_env)

    try:
        kwargs = json.loads(args.cf_kwargs) if args.cf_kwargs else {}
    except Exception as e:
        raise SystemExit(f"--cf-kwargs 不是合法 JSON: {e}")
    if not isinstance(kwargs, dict):
        raise SystemExit("--cf-kwargs 必须是 JSON 对象")

    # 设置 DEBUG_ID（影响 Mock 收窄）
    if args.cf_debug_id:
        os.environ["DEBUG_ID"] = args.cf_debug_id

    # 装配 CustomerUtil
    if args.cf_env == "mock":
        logging.info("[launcher] 使用离线 Mock 数据（不连任何环境）")
        cu = MockCustomerUtil(debug_id=args.cf_debug_id or None, company_id=int(args.cf_company_id or 1))
    else:
        server = args.cf_server
        if not server:
            raise SystemExit(f"环境 {args.cf_env} 需要提供 --cf-server(base url)")
        dry_run = args.cf_write_real != "1"
        cu = RealCustomerUtil(
            base=server,
            token=args.cf_token or None,
            dry_run=dry_run,
            company_id=int(args.cf_company_id or 1),
        )
        logging.info("[launcher] 连接真实 HCM: %s (dry_run=%s)", server, dry_run)

    # 装配 DB（可选）
    db = None
    if args.cf_db_url:
        try:
            db = loader.make_db(
                args.cf_db_url,
                allow_ddl=(args.cf_allow_ddl == "1"),
                save=(args.cf_db_save == "1"),
            )
            logging.info("[launcher] DB 已连接: %s (allow_ddl=%s save=%s)",
                         args.cf_db_url, args.cf_allow_ddl == "1", args.cf_db_save == "1")
        except Exception as e:
            logging.warning("[launcher] DB 连接失败，将以 _DbStub 运行: %s", e)

    loader.configure(company_id=int(args.cf_company_id or 1), db=db, customer_util=cu)

    # 进入调试模式：在本进程内启动 debugpy 适配器并等待 DAP 客户端附加。
    # 断点命中、变量/调用栈均由 debugpy 经 DAP 暴露给前端（仿 PyCharm）。
    if args.cf_dap_port:
        import debugpy
        debugpy.listen(("127.0.0.1", int(args.cf_dap_port)))
        logging.info("[launcher] debugpy 监听 127.0.0.1:%s，等待 DAP 客户端附加…", args.cf_dap_port)
        debugpy.wait_for_client()
        logging.info("[launcher] DAP 客户端已附加，开始执行云函数")

    # 执行
    model = loader.detect_model(cf_file)
    logging.info("[launcher] 检测到执行上下文: 模型 %s", model)
    entry = args.cf_entry or None
    run = loader.run_model_b if model == "B" else loader.run_model_a
    result = run(cf_file, entry=entry, kwargs=kwargs)

    logging.info("[launcher] execute() 返回: %r", result)
    print("\n===== CLOUD_FUNCTION_RESULT =====")
    try:
        print(json.dumps(result, ensure_ascii=False, default=str, indent=2))
    except Exception:
        print(repr(result))
    print("===== END =====")
    return 0


if __name__ == "__main__":
    try:
        rc = main()
        sys.exit(rc if isinstance(rc, int) else 0)
    except Exception:
        # 异常完整 traceback 打到 stdout，前端日志区 + debugpy DAP 都能看到
        traceback.print_exc()
        sys.exit(1)
