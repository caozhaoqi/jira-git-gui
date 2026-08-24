#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HCM Token Bridge —— 从本机浏览器（Chrome/Safari）提取 rcbhlj HCM 登录 token，
供云函数日志查询 / hcm 对象查询等本地自动化调用使用。

用法:
  python hcm_token_bridge.py token [--raw] [--browser chrome|safari|auto]
      提取 token。默认只输出掩码（前8后4）；--raw 输出完整值（慎用，勿进日志/仓库）。
  python hcm_token_bridge.py check [--browser chrome|safari|auto]
      提取 token 并带 cookie 请求后端根路径验证有效性（HTTP 200 即链路通）。
  python hcm_token_bridge.py watch [--interval 1800] [--browser auto]
      定时刷新模式：循环从浏览器重抓 token，变化则更新 ~/.hcm_token_cache（权限 600）。
  python hcm_token_bridge.py cached
      输出当前缓存中的 token（无缓存时提示）。

安全约定:
  - token 是登录凭证，默认掩码输出，--raw 仅在受控环境使用
  - 缓存文件权限 600；本文件与缓存不进 git
  - Chrome 解密走 Keychain（首次运行会弹「允许 python 访问 Chrome Safe Storage」，需点允许）
  - Safari 读容器内 cookie 库可能需要「完全磁盘访问」(TCC) 授权
"""

import argparse
import os
import sys
import time
import hashlib
import datetime

try:
    import browser_cookie3
except ImportError:
    sys.exit("缺少依赖：请先安装  browser-cookie3 pycryptodome")

# HCM 后端（cookie domain 即 73.2.3.27，本机可直连）
BASE_URL = "http://73.2.3.27"
TARGET_DOMAIN = "73.2.3.27"
TOKEN_NAME = "token"

CACHE_FILE = os.path.expanduser("~/.hcm_token_cache")
CACHE_MODE = 0o600


def mask(token: str) -> str:
    """掩码显示：前 8 后 4，中间打码。"""
    if not token:
        return "<EMPTY>"
    if len(token) <= 16:
        return token[:4] + "****"
    return token[:8] + "..." + token[-4:]


def fingerprint(token: str) -> str:
    """token 指纹（用于检测变化，不泄露明文）。"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


def get_token(browser: str = "auto") -> str | None:
    """从指定浏览器提取 token；auto = 先 Chrome 后 Safari。"""
    candidates = []
    if browser in ("chrome", "auto"):
        candidates.append(("chrome", browser_cookie3.chrome))
    if browser in ("safari", "auto"):
        candidates.append(("safari", browser_cookie3.safari))

    for name, loader in candidates:
        try:
            cj = loader(domain_name=TARGET_DOMAIN)
            for c in cj:
                if c.name == TOKEN_NAME and c.value:
                    print(f"[ok] 从 {name} 提取到 token（指纹 {fingerprint(c.value)}）", file=sys.stderr)
                    return c.value
            print(f"[warn] {name}: 未找到 {TARGET_DOMAIN} 的 {TOKEN_NAME} cookie", file=sys.stderr)
        except PermissionError as e:
            print(f"[error] {name}: 无权限读取（TCC/Keychain 授权）: {e}", file=sys.stderr)
        except Exception as e:
            print(f"[error] {name}: {e}", file=sys.stderr)
    return None


def read_cache() -> str | None:
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            return f.read().strip() or None
    except FileNotFoundError:
        return None


def write_cache(token: str) -> None:
    fd = os.open(CACHE_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, CACHE_MODE)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(token)
    os.chmod(CACHE_FILE, CACHE_MODE)


def check_valid(token: str) -> bool:
    """带 token 请求后端根路径，验证链路通。"""
    import urllib.request
    req = urllib.request.Request(BASE_URL + "/")
    req.add_header("Cookie", f"{TOKEN_NAME}={token}")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            code = resp.status
            print(f"[check] {BASE_URL}/ -> HTTP {code}", file=sys.stderr)
            return code == 200
    except Exception as e:
        print(f"[check] 请求失败: {e}", file=sys.stderr)
        return False


def cmd_token(args) -> int:
    tok = get_token(args.browser)
    if not tok:
        return 1
    write_cache(tok)
    if args.raw:
        print(tok)
    else:
        print(f"{TOKEN_NAME}|{mask(tok)}")
    return 0


def cmd_check(args) -> int:
    tok = get_token(args.browser)
    if not tok:
        print("提取 token 失败，无法验证", file=sys.stderr)
        return 1
    write_cache(tok)
    ok = check_valid(tok)
    return 0 if ok else 2


def cmd_cached(_args) -> int:
    tok = read_cache()
    if not tok:
        print("无缓存 token（先运行  token 或 watch）")
        return 1
    print(f"{TOKEN_NAME}|{mask(tok)}|fp={fingerprint(tok)}")
    return 0


def cmd_watch(args) -> int:
    interval = max(60, args.interval)
    print(f"[watch] 每 {interval}s 从 {args.browser} 刷新 token（变化时更新缓存），Ctrl-C 退出", file=sys.stderr)
    last_fp = None
    while True:
        tok = get_token(args.browser)
        if tok:
            fp = fingerprint(tok)
            write_cache(tok)
            if fp != last_fp:
                last_fp = fp
                ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                print(f"[watch] {ts} token 更新（fp={fp}，缓存已刷新）", file=sys.stderr)
        else:
            print(f"[watch] {datetime.datetime.now():%H:%M:%S} 提取失败，沿用旧缓存", file=sys.stderr)
        time.sleep(interval)
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="HCM token bridge（从本机浏览器提取 HCM 登录 token）")
    sub = p.add_subparsers(dest="cmd", required=True)

    pt = sub.add_parser("token", help="提取 token（默认掩码）")
    pt.add_argument("--raw", action="store_true", help="输出完整 token（慎用）")
    pt.add_argument("--browser", default="auto", choices=["chrome", "safari", "auto"])
    pt.set_defaults(fn=cmd_token)

    pc = sub.add_parser("check", help="提取并验证后端链路")
    pc.add_argument("--browser", default="auto", choices=["chrome", "safari", "auto"])
    pc.set_defaults(fn=cmd_check)

    pw = sub.add_parser("watch", help="定时刷新缓存")
    pw.add_argument("--interval", type=int, default=1800, help="刷新间隔秒数（默认 1800）")
    pw.add_argument("--browser", default="auto", choices=["chrome", "safari", "auto"])
    pw.set_defaults(fn=cmd_watch)

    pc2 = sub.add_parser("cached", help="查看缓存 token")
    pc2.set_defaults(fn=cmd_cached)

    args = p.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
