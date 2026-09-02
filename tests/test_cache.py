# -*- coding: utf-8 -*-
"""core.cache 并发安全 / TTL 淘汰 / 锁池有界 回归测试。

修复前的根因（见优化计划 B 组第 5 条）：
- set() 用 open('w') 先截断再写，并发/崩溃读到半截 JSON 被静默当 miss；
- get() TTL 过期只忽略、不删文件，缓存只增不减；
- _locks 每 key 一个 Lock，10 万文件仓库 = 10 万常驻 Lock 永不释放。
"""
import json
import threading
import time

from core import cache as _cache


def test_set_is_atomic_under_concurrent_writes(tmp_path, monkeypatch):
    """并发写同一 key，读到的必须是完整 JSON（不能半截）。"""
    monkeypatch.setattr(_cache, "CACHE_DIR", tmp_path)
    payload = {"big": "x" * 5000, "n": 0}

    errors = []

    def writer(i):
        for _ in range(20):
            payload["n"] = i
            _cache.set("ns", "k", dict(payload))
            # 立刻读回，必须能解析且是完整 dict
            got = _cache.get("ns", "k", ttl=0)
            if got is None:
                errors.append("None")
                return
            try:
                json.dumps(got)
            except (TypeError, ValueError) as e:
                errors.append(str(e))

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, "并发写入后出现损坏读取：%r" % errors[:3]


def test_get_evicts_expired_file(tmp_path, monkeypatch):
    """get() 命中过期条目时应删除文件，避免只增不减。"""
    monkeypatch.setattr(_cache, "CACHE_DIR", tmp_path)
    _cache.set("ns", "k", {"v": 1})
    f = _cache._cache_path("ns", "k")
    assert f.exists()

    # 把缓存时间改到 1 小时前，使其过期
    entry = json.loads(f.read_text())
    entry["_cached_at"] = time.time() - 7200
    f.write_text(json.dumps(entry))

    assert _cache.get("ns", "k", ttl=3600) is None
    assert not f.exists(), "过期文件应被删除"


def test_locks_pool_is_bounded(tmp_path, monkeypatch):
    """_locks 必须是固定大小池（不为每个 key 无限增长）。"""
    # 直接断言池大小有界；与路径无关，无需 monkeypatch
    assert len(_cache._locks) == _cache._LOCK_POOL_SIZE
    assert _cache._LOCK_POOL_SIZE <= 256, "锁池不应过大"
    # 不同 key 映射到池内某个锁，且同一 key 始终映射到同一把锁
    l1 = _cache._get_lock("ns", "a")
    l2 = _cache._get_lock("ns", "a")
    l3 = _cache._get_lock("ns", "b")
    assert l1 is l2
    assert isinstance(l1, threading.Lock)


def test_evict_expired_removes_stale_and_tmp(tmp_path, monkeypatch):
    """evict_expired 清掉过期 .json 与残留 .tmp。"""
    monkeypatch.setattr(_cache, "CACHE_DIR", tmp_path)
    _cache.set("ns", "fresh", {"v": 1})
    stale = _cache._cache_path("ns", "stale")
    stale.parent.mkdir(parents=True, exist_ok=True)
    entry = {"_cached_at": time.time() - 7200, "data": 1}
    stale.write_text(json.dumps(entry))
    # 真实场景里旧缓存文件的 mtime 也是老的；写入时间设到 2 小时前
    old = time.time() - 7200
    import os as _os
    _os.utime(stale, (old, old))
    tmp = stale.with_suffix(".json.tmp")
    tmp.write_text("{broken")

    removed = _cache.evict_expired(ttl=3600)
    assert removed >= 2
    assert not stale.exists()
    assert not tmp.exists()
    assert _cache._cache_path("ns", "fresh").exists()
