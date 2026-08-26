# -*- coding: utf-8 -*-
"""合并远程仓库最新代码到本地目录（缓存优先 + 同步历史 + 性能优化版）。

用法：
    python run_merge.py                # 合并 .env 中配置的全部仓库
    python run_merge.py --repo <别名>  # 仅合并指定仓库
    python run_merge.py --no-cache     # 禁用缓存，强制全量拉取
    python run_merge.py --history      # 查看同步历史（类 git log）
    python run_merge.py --clear-cache  # 清空缓存

特性：
- 仓库映射从 .env 加载，代码中无硬编码业务仓库名
- 远程文件树 / 文件内容优先走 JSON 缓存，避免重复拉取
- 每次同步记录到 sync_history/，可追溯（类 git log）
- 并行扫描 + 重试，合并阶段缓存命中提速
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.client import JiraGitClient
from core.config import load_config, load_merge_config
from core.diff import (
    scan_local_cached,
    scan_remote_cached,
    compute_diff,
    merge_entries,
    clear_dir_cache,
    DiffStatus,
)
from core import sync_history
from core import cache


def p(msg):
    """带时间戳的 flush print。"""
    t = time.strftime("%H:%M:%S")
    print(f"[{t}] {msg}", flush=True)


def sync_one_repo(client, repo, local_dir, scan_workers, tree_ttl, file_ttl,
                   use_cache, merge_workers=4, remote_only=False):
    """同步单个仓库：扫描 → 差异 → 合并，并记录历史。

    Args:
        client: 已选中仓库的客户端
        repo: 仓库信息对象
        local_dir: 本地目录
        scan_workers: 远程扫描并发数
        tree_ttl: 文件树缓存 TTL
        file_ttl: 文件内容缓存 TTL
        use_cache: 是否启用缓存
        remote_only: 为 True 时仅合并「仅远程」的云端差异项

    Returns:
        (summary, merged_count, failed_count, duration)
    """
    repo_alias = repo.display_name or repo.repo_id
    namespace = str(repo.repo_id)

    # 扫描本地（缓存优先）
    t0 = time.time()
    p(f"   扫描本地文件…")
    local_files = scan_local_cached(local_dir, tree_ttl=300, use_cache=use_cache)
    p(f"   ✓ 本地: {len(local_files)} 个文件 ({time.time()-t0:.1f}s)")

    # 扫描远程（缓存优先 + 并行）
    t1 = time.time()
    p(f"   扫描远程文件（{scan_workers}线程+重试+缓存）…")

    def _on_progress(scanned, pending):
        p(f"     远程扫描进度: {scanned} 文件, {pending} 目录待扫")

    remote_files = scan_remote_cached(
        client, namespace,
        max_workers=scan_workers,
        tree_ttl=tree_ttl,
        on_progress=_on_progress,
        use_cache=use_cache,
    )
    p(f"   ✓ 远程: {len(remote_files)} 个文件 ({time.time()-t1:.1f}s)")

    # 差异
    t2 = time.time()
    diff = compute_diff(local_files, remote_files)
    p(f"   ✓ 差异: {time.time()-t2:.1f}s")
    p(f"   总计={diff.total}  相同={diff.same}  修改={diff.modified}  "
      f"仅本地={diff.local_only}  仅远程={diff.remote_only}")

    summary = diff.summary()

    if diff.modified == 0 and diff.remote_only == 0:
        p(f"   → 无需合并")
        sync_history.record(
            repo_alias=repo_alias, local_dir=local_dir, summary=summary,
            merged=[], failed=[], duration=time.time() - t0, status="success",
            extra={"note": "无需合并", "remote_only": remote_only},
        )
        return summary, 0, 0, time.time() - t0

    # 合并（并行 fetch + write，受全局令牌桶限流，不会打崩服务器）
    if remote_only:
        to_merge = [e for e in diff.entries if e.status == DiffStatus.REMOTE_ONLY]
        p(f"\n   【仅合并云端差异项】开始合并 {len(to_merge)} 个文件（{merge_workers} 并发抓+写）…")
    else:
        to_merge = [e for e in diff.entries
                    if e.status in (DiffStatus.MODIFIED, DiffStatus.REMOTE_ONLY)]
        p(f"\n   开始合并 {len(to_merge)} 个文件（{merge_workers} 并发抓+写）…")

    clear_dir_cache()  # 每个仓库重置父目录缓存
    t3 = time.time()

    def _on_progress(done, ok, fail, total, path, success, err):
        if done % 50 == 0 or done == total:
            elapsed = time.time() - t3
            rate = done / elapsed if elapsed > 0 else 0
            eta = (total - done) / rate if rate > 0 else 0
            p(f"     {done}/{total}  成功={ok}  失败={fail}  "
              f"速率={rate:.1f}/s  预计剩余={eta:.0f}s")
        elif not success and fail <= 3:
            p(f"     ✗ {path} → {err}")

    ok, fail, merged_list, failed_list = merge_entries(
        local_dir, to_merge, client, namespace,
        file_ttl=file_ttl, use_cache=use_cache,
        max_workers=merge_workers, on_progress=_on_progress,
    )

    duration = time.time() - t3
    p(f"\n   ✓ {repo_alias} 完成: 成功={ok}  失败={fail}  耗时={duration:.1f}s")

    # 记录同步历史（类 git commit）
    status = "success" if fail == 0 else ("partial" if ok > 0 else "failed")
    commit_id = sync_history.record(
        repo_alias=repo_alias,
        local_dir=local_dir,
        summary=summary,
        merged=merged_list,
        failed=failed_list,
        duration=time.time() - t0,
        status=status,
        extra={
            "repo_id": repo.repo_id,
            "branch": repo.default_branch,
            "scan_workers": scan_workers,
            "remote_only": remote_only,
        },
    )
    p(f"   已记录同步历史: {commit_id}")

    return summary, ok, fail, duration


def main():
    parser = argparse.ArgumentParser(description="合并远程仓库到本地（缓存+历史+优化）")
    parser.add_argument("--repo", help="仅合并指定仓库别名（远程仓库名）")
    parser.add_argument("--no-cache", action="store_true", help="禁用缓存，强制全量拉取")
    parser.add_argument("--history", action="store_true", help="查看同步历史（类 git log）")
    parser.add_argument("--clear-cache", action="store_true", help="清空全部缓存")
    parser.add_argument("--clear-history", action="store_true", help="清空同步历史")
    parser.add_argument("--merge-workers", type=int, default=0,
                        help="合并并发数（默认取 .env 的 MERGE_WORKERS，未配置则 4）")
    parser.add_argument("--remote-only", action="store_true",
                        help="仅合并「仅远程」的云端差异项，跳过本地修改")
    args = parser.parse_args()

    # 历史查看
    if args.history:
        print(sync_history.format_log(limit=30))
        return

    # 清空缓存
    if args.clear_cache:
        n = cache.clear_all()
        print(f"已清空缓存：{n} 条")
        return

    # 清空历史
    if args.clear_history:
        n = sync_history.clear()
        print(f"已清空同步历史：{n} 个文件")
        return

    use_cache = not args.no_cache

    p("=" * 60)
    p("1. 加载 .env 配置并连接…")
    cfg, loaded, env_path = load_config()
    if not loaded:
        p(f"  ✗ 无法加载 .env")
        sys.exit(1)
    p(f"  ✓ 配置: {cfg.jira_url}  user={cfg.username}  mode={cfg.mode}")

    # 加载合并仓库映射（从 .env）
    merge_cfg = load_merge_config()
    repo_map = merge_cfg["repo_map"]
    if not repo_map:
        p("  ✗ .env 中未配置任何 MERGE_REPO_* 映射")
        p("  请在 .env 中添加，格式：MERGE_REPO_<别名>=<远程仓库名>|<本地绝对路径>")
        sys.exit(1)
    p(f"  ✓ 仓库映射: {len(repo_map)} 个")
    scan_workers = merge_cfg["scan_workers"]
    merge_workers = merge_cfg["merge_workers"]
    tree_ttl = merge_cfg["tree_ttl"]
    file_ttl = merge_cfg["file_ttl"]
    # CLI --merge-workers 优先覆盖 .env 配置
    if args.merge_workers and args.merge_workers > 0:
        merge_workers = args.merge_workers

    client = JiraGitClient()
    client.set_config(cfg)

    p("  连接中…")
    result = client.connect() or {}
    p(f"  Cookie: {'✓' if result.get('cookieOk') else '✗'}")

    # 发现仓库
    p("\n2. 发现仓库…")
    repos = client.discover_repos() or []
    p(f"  共 {len(repos)} 个仓库")

    # 构建仓库名 -> repo 对象的索引（取第一个精确匹配）
    repo_dict = {}
    for r in repos:
        name = (r.display_name or "").strip()
        if name in repo_map and name not in repo_dict:
            repo_dict[name] = r
            p(f"  ✓ 匹配: {name} → id={r.repo_id} branch={r.default_branch}")

    if not repo_dict:
        p("  ✗ 未匹配到任何目标仓库！")
        p(f"  前20个仓库名: {[r.display_name for r in repos[:20]]}")
        sys.exit(1)

    # 逐仓库处理
    for repo_name, local_dir in repo_map.items():
        if args.repo and repo_name != args.repo:
            continue
        if repo_name not in repo_dict:
            p(f"\n  ✗ 跳过 {repo_name}（未在远程找到精确匹配）")
            continue
        repo = repo_dict[repo_name]

        p(f"\n{'=' * 60}")
        p(f"3. 处理: {repo_name}")
        p(f"   id={repo.repo_id}  branch={repo.default_branch}")
        p(f"   本地: {local_dir}")

        if not os.path.isdir(local_dir):
            p(f"   ✗ 本地目录不存在，跳过")
            continue

        client.set_repo(repo.repo_id, repo.display_name, repo.default_branch)
        p(f"   ✓ 仓库已选择")

        sync_one_repo(
            client, repo, local_dir,
            scan_workers=scan_workers,
            tree_ttl=tree_ttl,
            file_ttl=file_ttl,
            use_cache=use_cache,
            merge_workers=merge_workers,
            remote_only=args.remote_only,
        )

    p(f"\n{'=' * 60}")
    p("全部完成！")
    p(f"\n同步统计：")
    stats = sync_history.stats()
    p(f"  历史同步次数: {stats['total_syncs']}")
    p(f"  累计合并文件: {stats['total_merged_files']}")
    p(f"  最近同步时间: {stats['last_sync_time'] or '(无)'}")


if __name__ == "__main__":
    main()
