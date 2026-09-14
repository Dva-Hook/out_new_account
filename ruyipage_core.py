#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Small self-contained direct RuyiPage/Firefox adapter for Actions."""

from __future__ import annotations

import contextlib
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any


PROFILE_MARKER = ".microsoft_email_actions_profile"
NAVIGATION_STATE_JS = r"""return (() => {
  const href = String(document.documentURI || location.href || '');
  const errorText = document.querySelector('#errorShortDescText, .title-text')
    ?.textContent?.trim() || '';
  return {
    href,
    ready_state: String(document.readyState || ''),
    network_error: href.startsWith('about:neterror')
      || !!document.querySelector('#errorPageContainer, .neterror'),
    error_code: errorText
  };
})();"""


def _atomic_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(value)
            handle.flush()
            with contextlib.suppress(OSError):
                os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            Path(name).unlink()


def sanitize_cached_profile(cache_dir: Path) -> list[str]:
    """Remove Firefox identity state while retaining HTTP cache directories."""

    cache_dir = Path(cache_dir).expanduser().resolve()
    cache_dir.mkdir(parents=True, exist_ok=True)
    marker = cache_dir / PROFILE_MARKER
    if not marker.exists():
        _atomic_text(marker, "managed static-cache profile\n")
    identity = (
        "cookies.sqlite", "cookies.sqlite-shm", "cookies.sqlite-wal",
        "webappsstore.sqlite", "webappsstore.sqlite-shm", "webappsstore.sqlite-wal",
        "storage.sqlite", "storage.sqlite-shm", "storage.sqlite-wal",
        "formhistory.sqlite", "logins.json", "logins.db", "logins.db-shm", "logins.db-wal",
        "sessionstore.jsonlz4", "sessionstore-backups", "sessionstore-logs",
        "storage/default", "storage/temporary", "storage/ls-archive.sqlite",
    )
    removed: list[str] = []
    for relative in identity:
        target = cache_dir / relative
        if not (target.exists() or target.is_symlink()):
            continue
        try:
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
            else:
                target.unlink()
            removed.append(relative)
        except FileNotFoundError:
            pass
    return removed


def build_ruyi_launch_options(
    *,
    headless: bool,
    snapshot_dir: Path,
    cache_dir: Path,
) -> dict[str, Any]:
    """Build direct-network launch options without a route override."""

    return {
        "headless": bool(headless),
        "private": False,
        "user_dir": str(Path(cache_dir).resolve()),
        "window_size": (1920, 1080),
        "timeout_page_load": 60,
        "timeout_script": 60,
        "close_on_exit": True,
        "failure_snapshot": True,
        "snapshot_dir": str(Path(snapshot_dir).resolve()),
    }


def launch_ruyi_browser(
    _unused_route: str = "",
    *,
    headless: bool,
    snapshot_dir: Path,
    cache_dir: Path,
) -> Any:
    try:
        import ruyipage
    except ImportError as exc:  # pragma: no cover - runner dependency
        raise RuntimeError("缺少 RuyiPage，请先安装 requirements.txt") from exc
    Path(snapshot_dir).mkdir(parents=True, exist_ok=True)
    sanitize_cached_profile(Path(cache_dir))
    page = ruyipage.launch(
        **build_ruyi_launch_options(
            headless=headless,
            snapshot_dir=Path(snapshot_dir),
            cache_dir=Path(cache_dir),
        )
    )
    with contextlib.suppress(Exception):
        page.close_other_tabs()
    with contextlib.suppress(Exception):
        page.set_bypass_csp(True)
    return page


def close_ruyi_browser(page: Any, cache_dir: Path | None = None) -> None:
    try:
        page.quit(timeout=10, force=True)
    finally:
        if cache_dir is not None:
            sanitize_cached_profile(Path(cache_dir))


def _contexts(page: Any) -> list[Any]:
    contexts = [page]
    with contextlib.suppress(Exception):
        contexts.extend(page.get_all_frames() or [])
    return contexts


def navigate_with_retry(
    page: Any,
    url: str,
    description: str,
    *,
    attempts: int = 4,
    timeout: float = 60.0,
    backoff_seconds: float = 2.0,
) -> dict[str, Any]:
    last_detail = "导航失败"
    total_attempts = max(1, int(attempts))
    for attempt in range(1, total_attempts + 1):
        navigation_error: Exception | None = None
        try:
            page.get(url, wait="interactive", timeout=timeout)
        except Exception as exc:
            navigation_error = exc
        try:
            state = page.run_js(NAVIGATION_STATE_JS, timeout=10)
        except Exception as exc:
            state = None
            navigation_error = navigation_error or exc
        if isinstance(state, dict):
            href = str(state.get("href") or "")
            if not state.get("network_error") and href.startswith(("http://", "https://")):
                return state
            last_detail = str(state.get("error_code") or href or last_detail)
        elif navigation_error is not None:
            last_detail = type(navigation_error).__name__
        if attempt < total_attempts and backoff_seconds > 0:
            time.sleep(float(backoff_seconds) * attempt)
    raise RuntimeError(f"{description}连续 {total_attempts} 次导航失败: {last_detail}")


def wait_ele(page: Any, selector: str, description: str, timeout: float = 30.0) -> Any:
    deadline = time.monotonic() + max(0.1, float(timeout))
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        for context in _contexts(page):
            try:
                element = context.ele(selector, timeout=0.25)
                if element:
                    return element
            except Exception as exc:
                last_error = exc
        time.sleep(0.25)
    raise TimeoutError(f"等待元素超时: {description}, selector={selector}, last={last_error}")


def click_ele(page: Any, selector: str, description: str, timeout: float = 30.0) -> None:
    element = wait_ele(page, selector, description, timeout)
    try:
        element.click()
    except Exception:
        element.click(by_js=True)


def save_screenshot(page: Any, path: Path) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(Exception):
        page.screenshot(path=str(path), full_page=True)


__all__ = [
    "build_ruyi_launch_options",
    "click_ele",
    "close_ruyi_browser",
    "launch_ruyi_browser",
    "navigate_with_retry",
    "sanitize_cached_profile",
    "save_screenshot",
    "wait_ele",
]
