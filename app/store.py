"""基于本地 JSON 文件的持久化（全量状态 + 原子替换写盘）。

资金流水采用 append-only 语义（由 service 层保证），本模块只负责
把 :class:`~app.models.State` 整体序列化到单个 JSON 文件：

- 进程内以一把可重入锁串行化所有“读-改-写”，保证同进程并发安全；
- 写盘采用临时文件 + ``os.replace`` 原子替换，进程崩溃不会留下半写文件；
- 服务重启后从同一文件恢复，幂等记录与资金流水均可复现。
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path

from app.models import State


class JsonStore:
    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._state: State | None = None

    def load(self) -> State:
        """读取（必要时从磁盘加载）状态；文件不存在则返回空状态。"""
        with self._lock:
            if self._state is not None:
                return self._state
            if not self.path.exists():
                self._state = State()
                return self._state
            with self.path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            self._state = State.from_dict(data)
            return self._state

    def save(self, state: State) -> None:
        """将状态原子写入磁盘。"""
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(
                dir=str(self.path.parent), suffix=".tmp", prefix=self.path.name
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(state.to_dict(), f, ensure_ascii=False, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_name, self.path)
            except BaseException:
                # 写盘失败时清理临时文件，避免污染数据目录
                try:
                    os.unlink(tmp_name)
                except FileNotFoundError:
                    pass
                raise
            self._state = state

    def lock(self) -> threading.RLock:
        """返回串行化锁。业务方应在同一把锁内完成读-改-写。"""
        return self._lock

    def reload(self) -> State:
        """丢弃内存缓存重新从磁盘加载（用于验证重启语义）。"""
        with self._lock:
            self._state = None
            return self.load()
