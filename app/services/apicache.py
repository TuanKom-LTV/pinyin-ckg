"""Bộ nhớ đệm ngắn cho các API trang web hỏi liên tục (biểu đồ, phân tích, trạng thái).

Nhiều người cùng mở trang → mỗi người hỏi /ckg/api/data, /chart... mỗi 3 giây. Không có đệm thì máy chủ tính lại
cho từng người và giữ khóa MT5 (bot phải xếp hàng chờ). Có đệm: cùng tham số thì trong `ttl` giây chỉ tính 1 lần,
người hỏi cùng lúc chờ kết quả của lần tính đó (không tính trùng). Kết quả lỗi ({"ok": False}) không giữ lại.
"""

import functools
import threading
import time

STATS = {"hit": 0, "miss": 0}


def ttl(seconds):
    def deco(fn):
        cache, locks, glock = {}, {}, threading.Lock()

        @functools.wraps(fn)
        def wrapper(*a, **k):
            key = (a, tuple(sorted(k.items())))
            hit = cache.get(key)
            if hit and time.time() - hit[0] < seconds:
                STATS["hit"] += 1
                return hit[1]
            with glock:
                lk = locks.setdefault(key, threading.Lock())
            with lk:
                hit = cache.get(key)
                if hit and time.time() - hit[0] < seconds:
                    STATS["hit"] += 1
                    return hit[1]
                STATS["miss"] += 1
                v = fn(*a, **k)
                if not (isinstance(v, dict) and v.get("ok") is False):
                    cache[key] = (time.time(), v)
                if len(cache) > 400:                      # dọn khóa cũ
                    now = time.time()
                    for kk in [kk for kk, vv in cache.items() if now - vv[0] > max(seconds, 30)]:
                        cache.pop(kk, None)
                        locks.pop(kk, None)
                return v
        return wrapper
    return deco
