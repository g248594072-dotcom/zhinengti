# -*- coding: utf-8 -*-
"""拉取进度与取消控制"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Callable, Optional


class FetchCancelledError(Exception):
    """用户关闭进度窗口主动取消拉取。"""


@dataclass
class ProgressSnapshot:
    stage: str = ""
    current: int = 0
    total: int = 0
    detail: str = ""


ProgressUIHook = Optional[Callable[[ProgressSnapshot], None]]
CancelCheck = Callable[[], bool]


class FetchProgressReporter:
    """线程安全的拉取进度上报；支持取消检测。"""

    def __init__(
        self,
        ui_hook: ProgressUIHook = None,
        cancel_check: Optional[CancelCheck] = None,
    ):
        self._ui_hook = ui_hook
        self._cancel_check = cancel_check
        self._cancel_event = threading.Event()
        self._lock = threading.Lock()
        self._snapshot = ProgressSnapshot()

    def bind_cancel_check(self, cancel_check: CancelCheck) -> None:
        self._cancel_check = cancel_check

    def request_cancel(self) -> None:
        self._cancel_event.set()

    def is_cancelled(self) -> bool:
        if self._cancel_event.is_set():
            return True
        if self._cancel_check and self._cancel_check():
            self._cancel_event.set()
            return True
        return False

    def check_cancelled(self) -> None:
        if self.is_cancelled():
            raise FetchCancelledError("拉取已取消")

    def set_stage(self, stage: str, total: int, detail: str = "") -> None:
        with self._lock:
            self._snapshot = ProgressSnapshot(
                stage=stage,
                current=0,
                total=max(int(total), 1),
                detail=detail or stage,
            )
            snap = ProgressSnapshot(**self._snapshot.__dict__)
        self._emit(snap)

    def set_total(self, total: int) -> None:
        with self._lock:
            self._snapshot.total = max(int(total), 1)
            snap = ProgressSnapshot(**self._snapshot.__dict__)
        self._emit(snap)

    def advance(self, step: int = 1, detail: str | None = None) -> None:
        with self._lock:
            self._snapshot.current = min(
                self._snapshot.current + step,
                self._snapshot.total,
            )
            if detail is not None:
                self._snapshot.detail = detail
            snap = ProgressSnapshot(**self._snapshot.__dict__)
        self._emit(snap)

    def set_detail(self, detail: str) -> None:
        with self._lock:
            self._snapshot.detail = detail
            snap = ProgressSnapshot(**self._snapshot.__dict__)
        self._emit(snap)

    def _emit(self, snap: ProgressSnapshot) -> None:
        if self._ui_hook:
            self._ui_hook(snap)
