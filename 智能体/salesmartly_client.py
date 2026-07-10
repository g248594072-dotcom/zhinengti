# -*- coding: utf-8 -*-
"""SaleSmartly API 客户端（签名、分页、配置加载）"""

from __future__ import annotations

import hashlib
import json
import os
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

DEFAULT_PAGE_SIZE = 200
DEFAULT_MAX_WORKERS = 10
DEFAULT_GET_QPS = 10

# 官方 QPS 限制：https://salesmartly-api.apifox.cn/接口qps限制文档-6974984m0
ENDPOINT_QPS_LIMITS: Dict[str, float] = {
    "/api/v2/get-contact-list": 10,
    "/api/v2/get-all-message-list": 10,
    "/api/v2/get-message-list": 10,
    "/api/v2/get-member-list": 10,
    "/api/v2/get-link-list": 10,
    "/api/v2/get-link-record-list": 10,
    "/api/v2/get-individual-whatsapp-list": 20,
    "/api/v2/start-individual-whatsapp-app": 30,
}

_ENDPOINT_LIMITERS: Dict[str, "RateLimiter"] = {}
_ENDPOINT_LIMITERS_LOCK = threading.Lock()


class RateLimiter:
    """线程安全的固定间隔限速器，保证单接口不超过文档 QPS。"""

    def __init__(self, qps: float):
        self._interval = 1.0 / max(float(qps), 0.1)
        self._lock = threading.Lock()
        self._next_allowed = 0.0

    def acquire(self) -> None:
        with self._lock:
            now = time.monotonic()
            if now < self._next_allowed:
                time.sleep(self._next_allowed - now)
                now = time.monotonic()
            self._next_allowed = max(now, self._next_allowed) + self._interval


def _rate_limiter_for(endpoint: str) -> RateLimiter:
    qps = ENDPOINT_QPS_LIMITS.get(endpoint, DEFAULT_GET_QPS)
    with _ENDPOINT_LIMITERS_LOCK:
        limiter = _ENDPOINT_LIMITERS.get(endpoint)
        if limiter is None:
            limiter = RateLimiter(qps)
            _ENDPOINT_LIMITERS[endpoint] = limiter
        return limiter



class ConfigError(Exception):
    pass


class APIError(Exception):
    def __init__(self, code: int, message: str):
        self.code = code
        super().__init__(f"API 错误 {code}: {message}")


class NetworkError(Exception):
    pass


@dataclass
class Config:
    api_key: str
    project_id: str
    base_url: str = "https://developer.salesmartly.com"


def app_dir() -> str:
    import sys
    if getattr(sys, "frozen", False) and hasattr(sys, "executable"):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def config_path() -> str:
    config_dir = (os.environ.get("QC_CONFIG_DIR") or "").strip() or app_dir()
    return os.path.join(config_dir, "api-key.json")


def load_config(path: Optional[str] = None) -> Config:
    path = path or config_path()
    api_key = os.environ.get("SALESMARTLY_API_KEY")
    project_id = os.environ.get("SALESMARTLY_PROJECT_ID")
    base_url = os.environ.get("SALESMARTLY_BASE_URL", "https://developer.salesmartly.com")

    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        nested = raw.get("salesmartly", raw)
        api_key = api_key or nested.get("apiKey") or nested.get("api_key")
        project_id = project_id or nested.get("projectId") or nested.get("project_id")
        base_url = nested.get("baseUrl") or nested.get("base_url") or base_url

    if not api_key or not project_id:
        raise ConfigError(
            "未找到 API 配置。请在程序目录创建 api-key.json，"
            "或设置环境变量 SALESMARTLY_API_KEY / SALESMARTLY_PROJECT_ID。"
        )
    return Config(api_key=api_key, project_id=project_id, base_url=base_url)


def save_config(api_key: str, project_id: str, path: Optional[str] = None) -> None:
    path = path or config_path()
    data = {"apiKey": api_key.strip(), "projectId": project_id.strip()}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


class SaleSmartlyClient:
    USER_AGENT = "QC-Agent-DealFetch/1.0"

    def __init__(self, config: Config, timeout: int = 60):
        self.config = config
        self.timeout = timeout
        self._ssl_ctx = ssl.create_default_context()

    @staticmethod
    def generate_sign(api_key: str, params: dict) -> str:
        parts = [api_key]
        for k, v in sorted(params.items(), key=lambda x: x[0]):
            if v is not None:
                parts.append(f"{k}={v}")
        return hashlib.md5("&".join(parts).encode()).hexdigest()

    def get(self, endpoint: str, params: Optional[Dict[str, Any]] = None) -> dict:
        _rate_limiter_for(endpoint).acquire()
        params = dict(params or {})
        params.setdefault("project_id", self.config.project_id)
        sign = self.generate_sign(self.config.api_key, params)

        query_parts = []
        for k, v in params.items():
            sv = str(v)
            if sv.startswith("{") or sv.startswith("["):
                query_parts.append(f"{k}={urllib.parse.quote(sv)}")
            else:
                query_parts.append(f"{k}={urllib.parse.quote(sv, safe='')}")
        url = f"{self.config.base_url}{endpoint}?{'&'.join(query_parts)}"
        headers = {
            "Content-Type": "application/json",
            "User-Agent": self.USER_AGENT,
            "External-Sign": sign,
        }
        req = urllib.request.Request(url, headers=headers)
        return self._execute(req)

    def get_all_pages(
        self,
        endpoint: str,
        params: Optional[Dict[str, Any]] = None,
        page_size: int = DEFAULT_PAGE_SIZE,
        max_pages: int = 500,
        list_key: str = "list",
        cancel_check: Optional[Callable[[], bool]] = None,
        on_page: Optional[Callable[[int, int], None]] = None,
    ) -> Tuple[List[dict], int]:
        params = dict(params or {})
        all_items: List[dict] = []
        total = 0
        for page in range(1, max_pages + 1):
            if cancel_check and cancel_check():
                break
            params["page"] = str(page)
            params["page_size"] = str(page_size)
            data = self.get(endpoint, params)
            total = data.get("total") or total
            items = data.get(list_key) or []
            if not items:
                break
            all_items.extend(items)
            if on_page:
                on_page(len(all_items), total or len(all_items))
            if total and len(all_items) >= int(total):
                break
            if len(items) < page_size:
                break
        return all_items, total

    def get_all_pages_concurrent(
        self,
        endpoint: str,
        params: Optional[Dict[str, Any]] = None,
        page_size: int = DEFAULT_PAGE_SIZE,
        max_pages: int = 500,
        list_key: str = "list",
        max_workers: int = DEFAULT_MAX_WORKERS,
        cancel_check: Optional[Callable[[], bool]] = None,
        on_page: Optional[Callable[[int, int], None]] = None,
    ) -> Tuple[List[dict], int]:
        """先拉第 1 页拿 total，再并发拉剩余页。"""
        params = dict(params or {})
        first = self.get(
            endpoint,
            {**params, "page": "1", "page_size": str(page_size)},
        )
        total = int(first.get("total") or 0)
        items: List[dict] = list(first.get(list_key) or [])
        if on_page and items:
            on_page(len(items), total or len(items))

        if cancel_check and cancel_check():
            return items, total

        if not items:
            return items, total

        total_pages = (total + page_size - 1) // page_size if total else 1
        total_pages = min(total_pages, max_pages)
        if total_pages <= 1:
            return items, total

        def _fetch_page(page_no: int) -> List[dict]:
            if cancel_check and cancel_check():
                return []
            data = self.get(
                endpoint,
                {**params, "page": str(page_no), "page_size": str(page_size)},
            )
            return list(data.get(list_key) or [])

        workers = min(max_workers, max(1, total_pages - 1))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(_fetch_page, page): page
                for page in range(2, total_pages + 1)
            }
            for fut in as_completed(futures):
                if cancel_check and cancel_check():
                    for pending in futures:
                        pending.cancel()
                    break
                page_items = fut.result()
                if page_items:
                    items.extend(page_items)
                    if on_page:
                        on_page(len(items), total or len(items))

        return items, total

    def _execute(self, req: urllib.request.Request) -> dict:
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self._ssl_ctx) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8")
            except Exception:
                pass
            raise NetworkError(f"HTTP {e.code}: {body}") from e
        except urllib.error.URLError as e:
            raise NetworkError(f"网络错误：{e.reason}") from e
        except Exception as e:
            raise NetworkError(f"请求失败：{e}") from e

        code = payload.get("code", -1)
        if code != 0:
            raise APIError(code, payload.get("msg", "Unknown error"))
        return payload.get("data", {})
