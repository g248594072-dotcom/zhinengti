# -*- coding: utf-8 -*-
"""SaleSmartly 拉取取消控制（网页断连、手动取消）。"""

from __future__ import annotations

from typing import Callable, Optional

CancelCheck = Callable[[], bool]


class FetchCancelledError(Exception):
    """拉取被用户关闭页面或主动取消。"""


def raise_if_cancelled(cancel_check: Optional[CancelCheck]) -> None:
    if cancel_check and cancel_check():
        raise FetchCancelledError("拉取已取消（页面已关闭或用户取消）")
