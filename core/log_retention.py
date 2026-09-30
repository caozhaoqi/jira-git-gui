# -*- coding: utf-8 -*-
"""日志/导出目录的保留策略（防止磁盘无限增长）。

背景：`logs/` 下的三类产物此前**只增不减**，实测积累到 271MB / 760 个文件：

  - ``logs/discover_raw_*.txt``：每次「发现仓库」写一份接口原始响应（537 个 / 113MB），
    纯调试产物，正常使用完全不需要保留。
  - ``logs/cf_logs/*.json``：云函数日志导出（134 个 / 122MB，单个最大 9.2MB），
    用户主动导出，可能需要留几天。
  - ``logs/cf_clipboard/*``：剪贴板转存文件，同上。

注意 ``jira_git_gui.log`` 本身已有 RotatingFileHandler 轮转（5MB × 3），不在此列。

策略：**按数量 + 按龄双重保留**，超出的按修改时间从旧到新删除；删除动作写入日志，
便于事后追溯。默认值偏保守（宁多留一些），可用环境变量覆盖。
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Iterable, Optional

from core.logger import get_logger

logger = get_logger("jira-git-gui")


def _env_int(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("[保留策略] 环境变量 %s=%r 不是整数，使用默认值 %d", name, raw, default)
        return default


def prune_dir(
    directory: Path,
    *,
    keep: int,
    max_age_days: int | None = None,
    patterns: Iterable[str] = ("*",),
    label: str = "",
    dry_run: bool = False,
) -> tuple[int, int]:
    """清理目录中过多的文件：保留最新 ``keep`` 个，并删除超过 ``max_age_days`` 的。

    返回 ``(删除文件数, 释放字节数)``。任何异常都被吞掉——保留策略失败绝不能
    影响主流程（例如发现仓库/日志导出本身）。
    """
    removed = 0
    freed = 0
    try:
        if not directory.is_dir():
            return 0, 0
        files: list[Path] = []
        for pat in patterns:
            files.extend(p for p in directory.glob(pat) if p.is_file())
        if not files:
            return 0, 0

        # 统一按 mtime 从新到旧排序
        def mtime(p: Path) -> float:
            try:
                return p.stat().st_mtime
            except OSError:
                return 0.0

        files.sort(key=mtime, reverse=True)
        now = time.time()
        age_cutoff = now - max_age_days * 86400 if max_age_days else None

        victims: list[Path] = []
        for idx, p in enumerate(files):
            too_many = idx >= keep
            too_old = age_cutoff is not None and mtime(p) < age_cutoff
            if too_many or too_old:
                victims.append(p)

        for p in victims:
            try:
                size = p.stat().st_size
            except OSError:
                size = 0
            if dry_run:
                removed += 1
                freed += size
                continue
            try:
                p.unlink()
                removed += 1
                freed += size
            except OSError as e:
                logger.warning("[保留策略] 删除失败 %s：%s", p, e)
        if removed and not dry_run:
            logger.info(
                "[保留策略] %s 清理 %d 个文件，释放 %.1f MB（保留最新 %d 个%s）",
                label or directory.name, removed, freed / 1048576, keep,
                f"，过期阈值 {max_age_days} 天" if max_age_days else "",
            )
    except Exception as e:  # noqa: BLE001 —— 保留策略失败不影响主流程
        logger.warning("[保留策略] %s 清理异常：%s", label or directory, e)
    return removed, freed


def prune_discover_raw(directory: Path, *, dry_run: bool = False) -> tuple[int, int]:
    """发现仓库的接口原始 dump：纯调试产物，只保留最近几份。"""
    return prune_dir(
        directory,
        keep=_env_int("JGG_KEEP_DISCOVER_RAW", 3),
        max_age_days=_env_int("JGG_KEEP_DISCOVER_RAW_DAYS", 7),
        patterns=("discover_raw_*.txt",),
        label="发现仓库原始响应",
        dry_run=dry_run,
    )


def prune_cf_exports(directory: Path, *, dry_run: bool = False) -> tuple[int, int]:
    """云函数日志导出 / 剪贴板转存：用户主动导出，保留得宽松一些。"""
    return prune_dir(
        directory,
        keep=_env_int("JGG_KEEP_CF_EXPORTS", 30),
        max_age_days=_env_int("JGG_KEEP_CF_EXPORTS_DAYS", 14),
        patterns=("*.json", "*.txt", "*.log", "*.md", "*.csv"),
        label="云函数日志导出",
        dry_run=dry_run,
    )
