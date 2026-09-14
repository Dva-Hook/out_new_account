#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Log in to Microsoft email and add two aliases with a reusable RuyiPage profile."""

from __future__ import annotations

import argparse
import datetime as dt
import html
import importlib.util
import logging
import os
import random
import re
import shutil
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
# The Actions checkout is self-contained; sibling folders from the desktop
# development workspace must never be required at runtime.
PROJECT_ROOT = SCRIPT_DIR
CORE_DIR = SCRIPT_DIR
if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))

import ruyipage_core as core


DEFAULT_URL = "https://login.live.com/"
DEFAULT_CACHE_DIR = SCRIPT_DIR / "browser_cache_profile"
DEFAULT_SNAPSHOT_DIR = SCRIPT_DIR / "failure_snapshots"
DEFAULT_LOG_FILE = SCRIPT_DIR / "ruyipage_open.log"
DEFAULT_AUXILIARY_CREDENTIALS_FILE = SCRIPT_DIR / "辅助邮箱.txt"
O2_MAIL_BASE_URL = os.environ.get("BATTLE_NET_O2_MAIL_BASE_URL", "https://app.wyx66.com").strip().rstrip("/")
O2_REQUEST_TIMEOUT = 20.0
ACCOUNT_HOME_URL = "https://account.microsoft.com/?lang=en-US&refd=account.live.com&refp=landing&mkt=EN-US"
ADD_ALIAS_URL = "https://account.live.com/AddAssocId"
ACCOUNT_NAMES_PREFIX = "https://account.live.com/names"
CREDENTIAL_ACTION_PREFIX = "https://account.live.com/interrupt/credentialaction"
ALIAS_REDIRECT_GRACE_SECONDS = 0.75
MAX_CREDENTIAL_ACTION_ATTEMPTS = 2
SECURITY_CODE_INPUT_INTERVAL = 0.5
ID_ZERO_SELECTOR = "#id__0"
YES_BUTTON_SELECTOR = 'button[type="submit"][data-testid="primaryButton"]'
YES_BUTTON_TEXT = "Yes"
# The watcher intentionally polls once per second.  Keep the historical
# constant name so callers that configure the monitor continue to work.
ID_ZERO_POLL_INTERVAL = 1.0
ID_ZERO_JS_TIMEOUT = 1.0
ID_ZERO_MONITOR_JOIN_TIMEOUT = 2.0
# RuyiPage serializes transport commands, but browser focus and DOM event
# ordering still need an application-level guard when the watcher and the
# foreground workflow touch the same tab.
BROWSER_INTERACTION_LOCK = threading.RLock()
USERNAME_SELECTOR = "#usernameEntry"
PASSWORD_SELECTOR = "#passwordEntry"
PRIMARY_BUTTON_SELECTOR = 'button[type="submit"][data-testid="primaryButton"]'
ASSOCIATED_ID_SELECTOR = "#AssociatedIdLive"
SUBMIT_YES_SELECTOR = "#SubmitYes"
EMAIL_ALIAS_SELECTORS = ("#idAliasEmail0", "#idAliasEmail1")
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
LOG = logging.getLogger("microsoft_email_ruyipage_open")
CLOSE_PROMPT = "请检查浏览器中的结果，按 Enter 关闭浏览器..."


@dataclass(frozen=True, slots=True)
class OpenOptions:
    url: str = DEFAULT_URL
    cache_dir: Path = DEFAULT_CACHE_DIR
    snapshot_dir: Path = DEFAULT_SNAPSHOT_DIR
    timeout: float = 60.0
    mail_timeout: float = 180.0
    headless: bool = False
    keep_open: bool = True
    clear_cache: bool = False
    auxiliary_credentials_file: Path = DEFAULT_AUXILIARY_CREDENTIALS_FILE


@dataclass(frozen=True, slots=True, repr=False)
class AccountCredentials:
    email: str
    password: str
    client_id: str
    token: str

    def __repr__(self) -> str:
        return f"AccountCredentials(email={self.email!r})"

    def alias_credential_line(self, alias: str) -> str:
        alias_value = str(alias or "").strip()
        if not alias_value:
            raise ValueError("别名不能为空")
        if "@" not in alias_value:
            domain = self.email.rsplit("@", 1)[1]
            alias_value = f"{alias_value}@{domain}"
        return "----".join((alias_value, self.password, self.client_id, self.token))


@dataclass(frozen=True, slots=True)
class AccountRun:
    """The non-secret outcome consumed by the Actions result writer."""

    aliases: tuple[str, ...]
    auxiliary_email: str


def email_prefix(email: str) -> str:
    normalized = str(email or "").strip()
    if not EMAIL_RE.fullmatch(normalized):
        raise ValueError(f"邮箱格式无效: {email!r}")
    return normalized.rsplit("@", 1)[0]


def parse_credential_line(raw: str) -> AccountCredentials:
    parts = [part.strip() for part in str(raw or "").strip().split("----", 3)]
    if len(parts) != 4 or not all(parts):
        raise ValueError("凭据格式错误，需要四段：邮箱----密码----client_id----令牌")
    email, password, client_id, token = parts
    email_prefix(email)
    return AccountCredentials(email=email, password=password, client_id=client_id, token=token)


def read_credentials(
    *,
    input_fn: Any = input,
) -> AccountCredentials:
    raw = str(input_fn("请输入邮箱----密码----client_id----令牌: ") or "").strip()
    return parse_credential_line(raw)


def load_auxiliary_credentials(
    path: Path = DEFAULT_AUXILIARY_CREDENTIALS_FILE,
    *,
    chooser: Any = random.choice,
) -> AccountCredentials:
    if not path.is_file():
        raise FileNotFoundError(f"辅助邮箱文件不存在: {path}")
    raw_rows = [line.strip() for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
    valid_rows: list[tuple[str, AccountCredentials]] = []
    for row in raw_rows:
        try:
            valid_rows.append((row, parse_credential_line(row)))
        except ValueError:
            LOG.warning("跳过格式无效的辅助邮箱记录")
    if not valid_rows:
        raise ValueError(f"辅助邮箱文件为空: {path}")
    selected_row = chooser([row for row, _ in valid_rows])
    for row, credentials in valid_rows:
        if row == selected_row:
            return credentials
    return parse_credential_line(str(selected_row))


def extract_security_code(content: str) -> str | None:
    text = html.unescape(str(content or ""))
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("\u200b", "").replace("\ufeff", "")
    match = re.search(
        r"security\s+code\s*[:：]?\s*(?:\*{1,2}|_{1,2}|`{1,3})?\s*(\d{6})",
        text,
        re.IGNORECASE,
    )
    return match.group(1) if match else None


def _load_outlook_fetcher() -> Any:
    path = PROJECT_ROOT / "outlook_battlenet_ticket_http.py"
    if not path.is_file():
        raise FileNotFoundError(f"Outlook 邮件读取模块不存在: {path}")
    spec = importlib.util.spec_from_file_location("outlook_mail_fetcher_for_microsoft_email", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Outlook 邮件读取模块加载失败: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _message_sender_is_microsoft_account_team(message: dict[str, Any]) -> bool:
    sender = message.get("from") or {}
    address = sender.get("emailAddress") if isinstance(sender, dict) else {}
    name = str(address.get("name") or "") if isinstance(address, dict) else ""
    email = str(address.get("address") or "") if isinstance(address, dict) else ""
    normalized = re.sub(
        r"\s+",
        " ",
        html.unescape(f"{name} {email}").replace("\u200b", "").replace("\ufeff", ""),
    ).casefold()
    return (
        "microsoft account team" in normalized
        or "accountprotection.microsoft.com" in email.casefold()
    )


def _message_content(message: dict[str, Any]) -> str:
    body = message.get("body") or {}
    if isinstance(body, dict):
        content = body.get("content") or body.get("text") or ""
    else:
        content = body
    return f"{content}\n{message.get('bodyPreview') or ''}"


def _received_datetime(message: dict[str, Any]) -> dt.datetime | None:
    raw = str(message.get("receivedDateTime") or message.get("received_time") or "").strip()
    if not raw:
        return None
    try:
        value = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)


def _target_email_is_mentioned(content: str, target_email: str | None) -> bool:
    if not target_email:
        return True
    normalized = (
        html.unescape(str(content or ""))
        .replace("\u200b", "")
        .replace("\ufeff", "")
        .casefold()
    )
    target = target_email.strip().casefold()
    if target in normalized:
        return True
    local, _, domain = target.partition("@")
    if len(local) >= 3 and domain:
        masked = re.compile(
            rf"{re.escape(local[:2])}\*{{2,}}{re.escape(local[-1])}@{re.escape(domain)}",
            re.IGNORECASE,
        )
        return bool(masked.search(normalized))
    return True


def _find_security_code_in_messages(
    messages: Any,
    *,
    not_before: dt.datetime | None,
    target_email: str | None,
) -> str | None:
    if not isinstance(messages, list):
        return None
    if not_before:
        normalized_cutoff = (
            not_before.replace(tzinfo=dt.timezone.utc)
            if not_before.tzinfo is None
            else not_before
        )
        cutoff = normalized_cutoff.astimezone(dt.timezone.utc)
    else:
        cutoff = None
    for message in messages:
        if not isinstance(message, dict) or not _message_sender_is_microsoft_account_team(message):
            continue
        received = _received_datetime(message)
        if cutoff and received and received < cutoff:
            continue
        content = _message_content(message)
        if not _target_email_is_mentioned(content, target_email):
            continue
        code = extract_security_code(content)
        if code:
            return code
    return None


def _fetch_security_code_graph(
    credentials: AccountCredentials,
    *,
    timeout: float,
    poll_interval: float,
    not_before: dt.datetime | None,
    target_email: str | None,
    fetcher_module: Any | None,
    sleeper: Any,
    monotonic: Any,
) -> str:
    fetcher = fetcher_module or _load_outlook_fetcher()
    deadline = monotonic() + max(1.0, float(timeout))
    with fetcher.requests.Session() as session:
        session.trust_env = False
        access_token, refresh_token = fetcher.get_access_token(
            session, credentials.client_id, credentials.token
        )
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "Prefer": 'outlook.body-content-type="html"',
        }
        while monotonic() < deadline:
            next_url: str | None = fetcher.MESSAGES_URL
            first_page = True
            while next_url and monotonic() < deadline:
                response = session.get(
                    next_url,
                    headers=headers,
                    params={
                        "$top": "50",
                        "$select": "id,subject,from,receivedDateTime,bodyPreview,body",
                        "$orderby": "receivedDateTime desc",
                    }
                    if first_page
                    else None,
                    timeout=30,
                )
                if response.status_code == 401:
                    access_token, refresh_token = fetcher.get_access_token(
                        session, credentials.client_id, refresh_token
                    )
                    headers["Authorization"] = f"Bearer {access_token}"
                    break
                response.raise_for_status()
                payload = response.json()
                code = _find_security_code_in_messages(
                    payload.get("value", []) if isinstance(payload, dict) else [],
                    not_before=not_before,
                    target_email=target_email,
                )
                if code:
                    LOG.info("已从辅助邮箱读取 Microsoft 安全验证码")
                    return code
                next_url = payload.get("@odata.nextLink") if isinstance(payload, dict) else None
                first_page = False
            else:
                next_url = None
            remaining = deadline - monotonic()
            if remaining > 0:
                sleeper(min(max(0.1, poll_interval), remaining))
    raise TimeoutError("在辅助邮箱中等待 Microsoft Security code 超时")


def _o2_post_json(session: Any, path: str, payload: dict[str, Any]) -> dict[str, Any]:
    response = session.post(
        f"{O2_MAIL_BASE_URL}/{path.lstrip('/')}",
        json=payload,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Origin": O2_MAIL_BASE_URL,
            "Referer": f"{O2_MAIL_BASE_URL}/",
            "User-Agent": "MicrosoftEmailRuyiPage-O2/1.0",
        },
        timeout=O2_REQUEST_TIMEOUT,
    )
    status_code = int(getattr(response, "status_code", 0))
    if not 200 <= status_code < 300:
        raise RuntimeError(f"O2请求失败: HTTP {status_code}")
    payload_value = response.json()
    if not isinstance(payload_value, dict):
        raise RuntimeError("O2响应格式错误")
    return payload_value


def _normalize_o2_message(row: dict[str, Any]) -> dict[str, Any]:
    sender = row.get("from")
    sender_name = str(row.get("from_name") or row.get("fromName") or row.get("sender_name") or "").strip()
    sender_address = str(row.get("from_address") or row.get("fromAddress") or row.get("sender_address") or "").strip()
    if isinstance(sender, dict):
        address_node = sender.get("emailAddress")
        if isinstance(address_node, dict):
            sender_name = sender_name or str(address_node.get("name") or "").strip()
            sender_address = sender_address or str(address_node.get("address") or "").strip()
        else:
            sender_name = sender_name or str(sender.get("name") or "").strip()
            sender_address = sender_address or str(sender.get("address") or "").strip()
    body = row.get("body")
    if isinstance(body, dict):
        body_content = body.get("content") or body.get("text") or body.get("value") or ""
    else:
        body_content = body or ""
    return {
        "from": {"emailAddress": {"name": sender_name, "address": sender_address}},
        "receivedDateTime": row.get("receivedDateTime") or row.get("received_time") or row.get("received_at") or "",
        "body": {"content": str(body_content)},
        "bodyPreview": row.get("bodyPreview") or row.get("body_preview") or row.get("preview") or "",
    }


def fetch_security_code_o2(
    credentials: AccountCredentials,
    *,
    timeout: float = 180.0,
    poll_interval: float = 3.0,
    not_before: dt.datetime | None = None,
    target_email: str | None = None,
    requests_module: Any | None = None,
    sleeper: Any = time.sleep,
    monotonic: Any = time.monotonic,
) -> str:
    if requests_module is None:
        try:
            import requests as requests_module
        except ImportError as exc:
            raise RuntimeError("读取辅助邮箱需要 requests 依赖") from exc
    deadline = monotonic() + max(1.0, float(timeout))
    with requests_module.Session() as session:
        try:
            session.trust_env = False
        except Exception:
            pass
        permission = _o2_post_json(
            session,
            "/detect-permission",
            {"client_id": credentials.client_id, "refresh_token": credentials.token},
        )
        if permission.get("success") is not True:
            raise RuntimeError("O2凭证检测失败")
        token_type = str(permission.get("token_type") or "").strip().casefold()
        if token_type not in {"o2", "graph"}:
            raise RuntimeError(f"O2返回了不支持的 token_type: {token_type or 'empty'}")
        while monotonic() < deadline:
            result = _o2_post_json(
                session,
                "/api/emails/refresh",
                {
                    "email_address": credentials.email,
                    "client_id": credentials.client_id,
                    "refresh_token": credentials.token,
                    "folder": "inbox",
                    "token_type": token_type,
                },
            )
            if result.get("success") is False:
                raise RuntimeError("O2读取邮件失败")
            rows = result.get("data")
            if not isinstance(rows, list):
                raise RuntimeError("O2返回的邮件列表格式错误")
            messages = [_normalize_o2_message(row) for row in rows if isinstance(row, dict)]
            code = _find_security_code_in_messages(
                messages,
                not_before=not_before,
                target_email=target_email,
            )
            if code:
                LOG.info("已通过 O2 从辅助邮箱读取 Microsoft 安全验证码")
                return code
            remaining = deadline - monotonic()
            if remaining > 0:
                sleeper(min(max(0.1, poll_interval), remaining))
    raise TimeoutError("通过 O2 等待 Microsoft Security code 超时")


def fetch_security_code(
    credentials: AccountCredentials,
    *,
    timeout: float = 180.0,
    poll_interval: float = 3.0,
    not_before: dt.datetime | None = None,
    target_email: str | None = None,
    fetcher_module: Any | None = None,
    o2_fetcher: Any = fetch_security_code_o2,
    sleeper: Any = time.sleep,
    monotonic: Any = time.monotonic,
) -> str:
    try:
        return _fetch_security_code_graph(
            credentials,
            timeout=timeout,
            poll_interval=poll_interval,
            not_before=not_before,
            target_email=target_email,
            fetcher_module=fetcher_module,
            sleeper=sleeper,
            monotonic=monotonic,
        )
    except Exception as graph_error:
        LOG.warning("Graph 读取 Microsoft 验证码失败，切换 O2: %s", type(graph_error).__name__)
        return o2_fetcher(
            credentials,
            timeout=timeout,
            poll_interval=poll_interval,
            not_before=not_before,
            target_email=target_email,
            sleeper=sleeper,
            monotonic=monotonic,
        )


ID_ZERO_PROBE_CLICK_JS = r"""
return (() => {
  const state = window.__ruyiButtonMonitorState
    || (window.__ruyiButtonMonitorState = {lastOk: null, lastYes: null});
  const normalize = value => String(value || '').replace(/\s+/g, ' ').trim().toLowerCase();
  const okElement = document.querySelector('#id__0');
  const yesElement = Array.from(document.querySelectorAll(
    'button[type="submit"][data-testid="primaryButton"]'
  )).find(button => normalize(button.textContent) === 'yes');
  const candidates = [
    {key: 'lastOk', element: okElement},
    {key: 'lastYes', element: yesElement},
  ];
  let present = false;
  for (const candidate of candidates) {
    const element = candidate.element;
    if (!element) {
      state[candidate.key] = null;
      continue;
    }
    present = true;
    if (state[candidate.key] && !state[candidate.key].isConnected) {
      state[candidate.key] = null;
    }
    if (state[candidate.key] === element) {
      continue;
    }
    // ``#id__0`` is the label span inside Microsoft's OK button.  Resolve
    // the actual interactive ancestor before checking disabled state so a
    // disabled parent cannot be triggered through a child span.
    const clickTarget = element.closest('button, [role="button"], a') || element;
    if (
      element.disabled
      || element.getAttribute('aria-disabled') === 'true'
      || clickTarget.disabled
      || clickTarget.getAttribute('aria-disabled') === 'true'
    ) {
      state[candidate.key] = null;
      continue;
    }
    try {
      clickTarget.click();
      state[candidate.key] = element;
      return {present: true, clicked: true, target: candidate.key};
    } catch (error) {
      state[candidate.key] = null;
      return {present: true, clicked: false, error: String(error)};
    }
  }
  return {present, clicked: false};
})();
"""


def probe_and_click_id_zero(
    page: Any,
    *,
    timeout: float = ID_ZERO_JS_TIMEOUT,
) -> bool:
    """Atomically detect and click either the ``OK`` or ``Yes`` button.

    Separate per-document markers prevent persistent elements from being
    clicked on every poll while still allowing newly rendered elements to
    trigger.
    """
    result = page.run_js(ID_ZERO_PROBE_CLICK_JS, timeout=timeout)
    if isinstance(result, dict):
        return bool(result.get("clicked"))
    return bool(result)


def _probe_id_zero_presence(page: Any, *, timeout: float) -> bool:
    result = page.run_js(
        "return Boolean(document.querySelector('#id__0'))",
        timeout=timeout,
    )
    return bool(result)


def _click_id_zero(page: Any, *, timeout: float) -> None:
    core.click_ele(page, ID_ZERO_SELECTOR, "id__0 自动按钮", timeout)


def _id_zero_target_key(target: Any) -> tuple[str, object]:
    """Return a stable key for a page/tab wrapper across refreshes."""
    # RuyiPage exposes the browsing-context identifier publicly as ``tab_id``;
    # injected wrappers in downstream callers may expose only that property.
    # Keep private/context spellings first, then tolerate common naming
    # variants without relying on the wrapper object's identity.
    for attribute in (
        "_context_id",
        "context_id",
        "tab_id",
        "_tab_id",
        "tabId",
        "_tabId",
    ):
        try:
            context_id = getattr(target, attribute, None)
        except Exception:
            context_id = None
        if context_id:
            return ("context", str(context_id))
    return ("object", id(target))


class IdZeroMonitor:
    """Background watcher that clicks the configured OK/Yes buttons.

    With no injected callbacks, each poll uses :func:`probe_and_click_id_zero`
    so detection and the DOM click happen in one browser command. Tests and
    callers that need a native RuyiPage click can inject ``probe`` and
    ``clicker``; those callbacks use appearance-edge de-duplication.
    """

    def __init__(
        self,
        page: Any,
        *,
        probe: Any | None = None,
        clicker: Any | None = None,
        poll_interval: float = ID_ZERO_POLL_INTERVAL,
        action_timeout: float = ID_ZERO_JS_TIMEOUT,
        page_lock: Any | None = None,
        monitor_tabs: bool = True,
    ) -> None:
        self.page = page
        self.probe = probe
        self.clicker = clicker
        self.poll_interval = max(0.01, float(poll_interval))
        self.action_timeout = max(0.1, float(action_timeout))
        self.page_lock = page_lock
        self.monitor_tabs = bool(monitor_tabs)
        self.stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._state_lock = threading.Lock()
        self._started = False
        self._was_present: dict[tuple[str, object], bool] = {}
        self._last_error: Exception | None = None
        self._error_count = 0
        self._click_count = 0
        self._last_target_count = 1

    @property
    def thread(self) -> threading.Thread | None:
        return self._thread

    @property
    def is_alive(self) -> bool:
        thread = self._thread
        return bool(thread and thread.is_alive())

    @property
    def last_error(self) -> Exception | None:
        return self._last_error

    @property
    def error_count(self) -> int:
        return self._error_count

    @property
    def click_count(self) -> int:
        return self._click_count

    def start(self) -> "IdZeroMonitor":
        with self._state_lock:
            if self._thread is not None and self._thread.is_alive():
                return self
            if self._started or self.stop_event.is_set():
                # A stopped monitor can be started again with a fresh event.
                self.stop_event = threading.Event()
                self._was_present.clear()
                self._last_target_count = 1
            self._started = True
            self._thread = threading.Thread(
                target=self._run,
                name="ruyi-id-zero-monitor",
                daemon=True,
            )
            self._thread.start()
        return self

    def stop(self, timeout: float = ID_ZERO_MONITOR_JOIN_TIMEOUT) -> "IdZeroMonitor":
        self.stop_event.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            requested_timeout = max(0.0, float(timeout))
            # A poll can be waiting on one bounded JS command per target.
            # Give multi-tab scans enough time to unwind before the browser is
            # closed, while retaining the caller-provided minimum timeout.
            unwind_timeout = self.action_timeout * max(1, self._last_target_count) + 0.25
            thread.join(max(requested_timeout, unwind_timeout))
        if thread is not None and thread.is_alive():
            LOG.warning("id__0 监控线程未在 %.1f 秒内结束", float(timeout))
        return self

    def _with_page_lock(self, callback: Any) -> Any:
        if self.page_lock is None:
            return callback()
        with self.page_lock:
            return callback()

    def _targets(self) -> list[Any]:
        targets = [self.page]
        if not self.monitor_tabs:
            return targets
        getter = getattr(self.page, "get_tabs", None)
        if callable(getter):
            try:
                targets.extend(
                    self._with_page_lock(lambda: list(getter() or []))
                )
            except Exception:
                pass
        else:
            # Small compatibility fallback for injected page doubles.
            try:
                tabs = self._with_page_lock(lambda: getattr(self.page, "tabs", None))
            except Exception:
                tabs = None
            if isinstance(tabs, (list, tuple)):
                targets.extend(tabs)
        unique: list[Any] = []
        seen: set[tuple[str, object]] = set()
        for target in targets:
            marker = _id_zero_target_key(target)
            if marker not in seen:
                seen.add(marker)
                unique.append(target)
        return unique

    def _poll_target(self, target: Any) -> None:
        if self.probe is None and self.clicker is None:
            clicked = self._with_page_lock(
                lambda: probe_and_click_id_zero(
                    target,
                    timeout=self.action_timeout,
                )
            )
            if clicked:
                self._click_count += 1
            return

        probe = self.probe or (
            lambda current: _probe_id_zero_presence(
                current,
                timeout=self.action_timeout,
            )
        )
        clicker = self.clicker or (
            lambda current: _click_id_zero(
                current,
                timeout=self.action_timeout,
            )
        )
        key = _id_zero_target_key(target)
        present = bool(self._with_page_lock(lambda: probe(target)))
        previous = self._was_present.get(key, False)
        if present and not previous:
            self._with_page_lock(lambda: clicker(target))
            self._click_count += 1
        self._was_present[key] = present

    def _run(self) -> None:
        while not self.stop_event.is_set():
            try:
                targets = self._targets()
                self._last_target_count = max(1, len(targets))
                for target in targets:
                    if self.stop_event.is_set():
                        break
                    try:
                        self._poll_target(target)
                    except Exception as exc:
                        self._last_error = exc
                        self._error_count += 1
                        if self._error_count == 1 or self._error_count % 20 == 0:
                            LOG.debug(
                                "按钮监控轮询失败（第 %d 次）: %s",
                                self._error_count,
                                type(exc).__name__,
                            )
            except Exception as exc:
                self._last_error = exc
                self._error_count += 1
            if self.stop_event.wait(self.poll_interval):
                break


def start_id_zero_monitor(page: Any, **kwargs: Any) -> IdZeroMonitor:
    kwargs.setdefault("page_lock", BROWSER_INTERACTION_LOCK)
    monitor = IdZeroMonitor(page, **kwargs)
    monitor.start()
    LOG.info(
        "按钮监控线程已启动: name=%s interval=%.1fs targets=#id__0(OK), %s(Yes)",
        monitor.thread.name if monitor.thread else "unknown",
        monitor.poll_interval,
        YES_BUTTON_SELECTOR,
    )
    return monitor


def stop_id_zero_monitor(
    monitor: IdZeroMonitor | None,
    *,
    timeout: float = ID_ZERO_MONITOR_JOIN_TIMEOUT,
) -> None:
    if monitor is not None:
        monitor.stop(timeout)
        LOG.info("按钮监控线程已停止")


def fill_security_code(
    page: Any,
    code: str,
    *,
    timeout: float,
    waiter: Any = core.wait_ele,
    clicker: Any = core.click_ele,
    sleeper: Any = time.sleep,
    interaction_lock: Any = BROWSER_INTERACTION_LOCK,
) -> None:
    normalized_code = str(code or "").strip()
    if not re.fullmatch(r"\d{6}", normalized_code):
        raise ValueError("Microsoft 安全验证码必须是 6 位数字")
    def fill_and_submit() -> None:
        for index, digit in enumerate(normalized_code):
            if index:
                sleeper(SECURITY_CODE_INPUT_INTERVAL)
            # Each Microsoft OTP box is initially empty and auto-focuses the
            # next box after a key event.  ``clear=True`` performs an extra
            # click plus Ctrl+A/Delete; that delayed focus action can clear
            # the preceding box.  Keep the native key event, but never clear
            # an individual box.
            waiter(
                page,
                f"#codeEntry-{index}",
                f"验证码第 {index + 1} 位",
                timeout,
            ).input(digit, clear=False)
        clicker(page, "#iMarkLost", "验证码丢失选项", timeout)
        clicker(page, "#CommitMiniBtn", "验证码提交按钮", timeout)

    # Hold the guard across the complete OTP sequence, including the
    # requested 0.5 s pauses, so the background id__0 watcher cannot steal
    # focus between two boxes.
    if interaction_lock is None:
        fill_and_submit()
    else:
        with interaction_lock:
            fill_and_submit()


def is_credential_action_redirect(href: str) -> bool:
    return str(href or "").strip().casefold().startswith(CREDENTIAL_ACTION_PREFIX.casefold())


def read_navigation_state(page: Any) -> dict[str, Any]:
    state = page.run_js(
        "return {href:String(location.href||''), ready_state:String(document.readyState||''), associated_id_present:Boolean(document.querySelector('#AssociatedIdLive')), code_entry_present:Boolean(document.querySelector('#codeEntry-0'))}",
        timeout=5,
    )
    return state if isinstance(state, dict) else {}


def wait_for_url(
    page: Any,
    matcher: str,
    *,
    timeout: float,
    state_reader: Any = read_navigation_state,
    sleeper: Any = time.sleep,
    monotonic: Any = time.monotonic,
) -> dict[str, Any]:
    deadline = monotonic() + max(0.1, float(timeout))
    while monotonic() < deadline:
        try:
            state = state_reader(page)
        except Exception:
            state = {}
        href = str(state.get("href") or "")
        ready_state = str(state.get("ready_state") or "").casefold()
        if matcher.casefold() in href.casefold() and ready_state == "complete":
            return state
        remaining = deadline - monotonic()
        if remaining > 0:
            sleeper(min(0.25, remaining))
    raise TimeoutError(f"等待页面加载完成超时: {matcher}")


def wait_for_alias_entry(
    page: Any,
    *,
    timeout: float,
    state_reader: Any = read_navigation_state,
    sleeper: Any = time.sleep,
    monotonic: Any = time.monotonic,
) -> dict[str, Any]:
    """Wait until AddAssocId is ready or Microsoft redirects to credentialaction."""
    deadline = monotonic() + max(0.1, float(timeout))
    stable_alias_deadline: float | None = None
    while monotonic() < deadline:
        try:
            state = state_reader(page)
        except Exception:
            state = {}
        if not isinstance(state, dict):
            state = {}
        href = str(state.get("href") or "")
        ready_state = str(state.get("ready_state") or "").casefold()
        if is_credential_action_redirect(href) and ready_state in {"complete", "interactive"}:
            return state
        associated_present = state.get("associated_id_present")
        # A missing field keeps compatibility with injected state readers that
        # only expose URL/readiness; the real reader always returns a boolean.
        if href.casefold().startswith(ADD_ALIAS_URL.casefold()) and ready_state == "complete":
            if associated_present is False:
                stable_alias_deadline = None
            elif stable_alias_deadline is None:
                # A credentialaction redirect can follow the initial form by
                # a short client-side navigation. Keep observing the URL for
                # a brief grace window before accepting the alias form. A
                # reader that omits DOM presence also gets this protection.
                stable_alias_deadline = min(
                    deadline,
                    monotonic() + ALIAS_REDIRECT_GRACE_SECONDS,
                )
            elif monotonic() >= stable_alias_deadline:
                return state
        else:
            stable_alias_deadline = None
        remaining = deadline - monotonic()
        if remaining > 0:
            sleeper(min(0.25, remaining))
    raise TimeoutError("等待 AddAssocId 页面或 credentialaction 重定向超时")


def wait_for_credential_submission(
    page: Any,
    *,
    timeout: float,
    state_reader: Any = read_navigation_state,
    sleeper: Any = time.sleep,
    monotonic: Any = time.monotonic,
) -> dict[str, Any]:
    """Wait until the six-digit credential-action form has submitted."""
    deadline = monotonic() + max(0.1, float(timeout))
    while monotonic() < deadline:
        try:
            state = state_reader(page)
        except Exception:
            state = {}
        if not isinstance(state, dict):
            state = {}
        href = str(state.get("href") or "").strip()
        usable_href = bool(href) and href.casefold() != "about:blank"
        if usable_href and not is_credential_action_redirect(href):
            # The route change is the authoritative completion signal. Some
            # Microsoft pages keep hidden code inputs in the DOM after submit.
            return state
        if usable_href and state.get("code_entry_present") is False:
            return state
        remaining = deadline - monotonic()
        if remaining > 0:
            sleeper(min(0.25, remaining))
    raise TimeoutError("等待辅助邮箱验证码提交完成超时")


def login_and_add_aliases(
    page: Any,
    *,
    email: str,
    password: str,
    timeout: float,
    mail_timeout: float = 180.0,
    waiter: Any = core.wait_ele,
    clicker: Any = core.click_ele,
    navigator: Any = core.navigate_with_retry,
    state_reader: Any = read_navigation_state,
    sleeper: Any = time.sleep,
    monotonic: Any = time.monotonic,
    utcnow: Any = lambda: dt.datetime.now(dt.timezone.utc),
    auxiliary_credentials_file: Path = DEFAULT_AUXILIARY_CREDENTIALS_FILE,
    auxiliary_credentials: AccountCredentials | None = None,
    auxiliary_loader: Any = load_auxiliary_credentials,
    security_code_fetcher: Any = fetch_security_code,
    interaction_lock: Any = BROWSER_INTERACTION_LOCK,
    auxiliary_result: list[Any] | None = None,
) -> list[str]:
    prefix = email_prefix(email)
    if not password:
        raise ValueError("密码不能为空")

    def run_locked(callback: Any) -> Any:
        if interaction_lock is None:
            return callback()
        with interaction_lock:
            return callback()

    def input_value(target: Any, selector: str, description: str, value: str, *, clear: bool) -> None:
        run_locked(
            lambda: waiter(target, selector, description, timeout).input(value, clear=clear)
        )

    def click_value(target: Any, selector: str, description: str) -> None:
        run_locked(lambda: clicker(target, selector, description, timeout))

    def navigate_value(target: Any, url: str, description: str) -> Any:
        return run_locked(lambda: navigator(target, url, description, timeout=timeout))

    def read_state(target: Any) -> dict[str, Any]:
        result = run_locked(lambda: state_reader(target))
        return result if isinstance(result, dict) else {}

    input_value(
        page,
        USERNAME_SELECTOR,
        "微软邮箱账号输入框",
        email,
        clear=True,
    )
    click_value(page, PRIMARY_BUTTON_SELECTOR, "微软邮箱账号 Next 按钮")

    input_value(page, PASSWORD_SELECTOR, "微软邮箱密码输入框", password, clear=True)
    click_value(page, PRIMARY_BUTTON_SELECTOR, "微软邮箱密码 Next 按钮")
    wait_for_url(
        page,
        "account.microsoft.com",
        timeout=timeout,
        state_reader=read_state,
        sleeper=sleeper,
        monotonic=monotonic,
    )

    aliases: list[str] = []
    for index, selector in enumerate(EMAIL_ALIAS_SELECTORS, start=1):
        alias = f"{prefix}{index:02d}"
        alias_tab = run_locked(lambda: page.new_tab("about:blank", background=False))
        navigate_value(alias_tab, ADD_ALIAS_URL, "微软邮箱别名添加页")
        current_state = wait_for_alias_entry(
            alias_tab,
            timeout=timeout,
            state_reader=read_state,
            sleeper=sleeper,
            monotonic=monotonic,
        )
        current_href = str(current_state.get("href") or "")
        challenge_attempts = 0
        while is_credential_action_redirect(current_href):
            if challenge_attempts >= MAX_CREDENTIAL_ACTION_ATTEMPTS:
                raise RuntimeError("credentialaction 重定向次数超过上限")
            challenge_attempts += 1
            LOG.info("检测到 credentialaction 重定向，先绑定辅助邮箱")
            click_value(alias_tab, PRIMARY_BUTTON_SELECTOR, "添加辅助邮箱按钮")
            auxiliary = auxiliary_credentials or auxiliary_loader(auxiliary_credentials_file)
            if auxiliary_result is not None:
                auxiliary_result.clear()
                auxiliary_result.append(auxiliary)
            input_value(
                alias_tab,
                "#floatingLabelInput10",
                "辅助邮箱输入框",
                auxiliary.email,
                clear=True,
            )
            submitted_at = utcnow()
            click_value(alias_tab, PRIMARY_BUTTON_SELECTOR, "辅助邮箱 Add email 按钮")
            run_locked(
                lambda: waiter(alias_tab, "#codeEntry-0", "安全验证码输入框", timeout)
            )
            code = security_code_fetcher(
                auxiliary,
                timeout=mail_timeout,
                not_before=submitted_at,
                target_email=email,
            )
            fill_security_code(
                alias_tab,
                code,
                timeout=timeout,
                waiter=waiter,
                clicker=clicker,
                sleeper=sleeper,
                interaction_lock=interaction_lock,
            )
            wait_for_credential_submission(
                alias_tab,
                timeout=timeout,
                state_reader=read_state,
                sleeper=sleeper,
                monotonic=monotonic,
            )
            navigate_value(alias_tab, ADD_ALIAS_URL, "重新打开微软邮箱别名添加页")
            current_state = wait_for_alias_entry(
                alias_tab,
                timeout=timeout,
                state_reader=read_state,
                sleeper=sleeper,
                monotonic=monotonic,
            )
            current_href = str(current_state.get("href") or "")
        input_value(
            alias_tab,
            ASSOCIATED_ID_SELECTOR,
            "关联邮箱输入框",
            alias,
            clear=True,
        )
        click_value(alias_tab, SUBMIT_YES_SELECTOR, "添加别名确认按钮")
        wait_for_url(
            alias_tab,
            ACCOUNT_NAMES_PREFIX,
            timeout=timeout,
            state_reader=read_state,
            sleeper=sleeper,
            monotonic=monotonic,
        )
        run_locked(lambda: waiter(alias_tab, selector, f"第 {index} 个别名确认元素", timeout))
        aliases.append(alias)
        LOG.info("第 %d 个别名添加成功: %s", index, alias)
    return aliases


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="使用 RuyiPage 登录微软邮箱并添加两个别名，同时复用静态资源缓存"
    )
    parser.add_argument("--url", default=DEFAULT_URL, help="要打开的页面 URL")
    parser.add_argument(
        "--cache-dir",
        default=str(DEFAULT_CACHE_DIR),
        help="持久化浏览器 profile 目录（保留 HTTP 静态资源缓存）",
    )
    parser.add_argument(
        "--snapshot-dir",
        default=str(DEFAULT_SNAPSHOT_DIR),
        help="导航失败截图目录",
    )
    parser.add_argument(
        "--auxiliary-file",
        default=str(DEFAULT_AUXILIARY_CREDENTIALS_FILE),
        help="辅助邮箱凭据文件；每行格式为 邮箱----密码----client_id----令牌",
    )
    parser.add_argument("--timeout", type=float, default=60.0, help="页面导航超时（秒）")
    parser.add_argument("--mail-timeout", type=float, default=180.0, help="验证码邮件等待超时（秒）")
    parser.add_argument("--headless", action="store_true", help="无界面运行")
    parser.add_argument(
        "--no-keep-open",
        dest="keep_open",
        action="store_false",
        help="兼容旧参数；浏览器关闭前仍会等待回车",
    )
    parser.add_argument(
        "--clear-cache",
        action="store_true",
        help="启动前删除本项目 profile，首次运行后重新建立缓存",
    )
    parser.add_argument("--log-file", default=str(DEFAULT_LOG_FILE), help="日志文件路径")
    return parser.parse_args(argv)


def options_from_args(args: argparse.Namespace) -> OpenOptions:
    timeout = float(args.timeout)
    if timeout <= 0:
        raise ValueError("--timeout 必须大于 0")
    mail_timeout = float(getattr(args, "mail_timeout", 180.0))
    if mail_timeout <= 0:
        raise ValueError("--mail-timeout 必须大于 0")
    url = str(args.url or "").strip()
    if not url.startswith(("http://", "https://")):
        raise ValueError("--url 必须是 http:// 或 https:// 地址")
    return OpenOptions(
        url=url,
        cache_dir=Path(args.cache_dir).expanduser().resolve(),
        snapshot_dir=Path(args.snapshot_dir).expanduser().resolve(),
        timeout=timeout,
        mail_timeout=mail_timeout,
        headless=bool(args.headless),
        keep_open=bool(args.keep_open),
        clear_cache=bool(args.clear_cache),
        auxiliary_credentials_file=Path(
            getattr(args, "auxiliary_file", DEFAULT_AUXILIARY_CREDENTIALS_FILE)
        ).expanduser().resolve(),
    )


def clear_profile(cache_dir: Path) -> None:
    """Remove only the configured profile directory before a fresh run."""
    cache_dir = cache_dir.resolve()
    project_root = PROJECT_ROOT.resolve()
    script_dir = SCRIPT_DIR.resolve()
    if (
        cache_dir == Path(cache_dir.anchor).resolve()
        or cache_dir in {project_root, script_dir}
        or cache_dir in project_root.parents
    ):
        raise ValueError(f"拒绝清理过宽的缓存目录: {cache_dir}")
    if cache_dir.exists():
        shutil.rmtree(cache_dir)
        LOG.info("已清理旧缓存 profile: %s", cache_dir)


def open_email_page(
    options: OpenOptions,
    *,
    core_module: Any = core,
    credentials: AccountCredentials | None = None,
    input_fn: Any = input,
    workflow: Any = login_and_add_aliases,
    monitor_factory: Any | None = None,
    monitor_stopper: Any | None = None,
    emit_alias_lines: bool = True,
    auxiliary_credentials: AccountCredentials | None = None,
) -> int:
    """Launch RuyiPage, optionally log in, and cleanly close the browser."""
    options.cache_dir.parent.mkdir(parents=True, exist_ok=True)
    options.snapshot_dir.mkdir(parents=True, exist_ok=True)
    if options.clear_cache:
        clear_profile(options.cache_dir)

    page: Any | None = None
    id_zero_monitor: Any | None = None
    use_default_monitor = monitor_factory is None
    monitor_factory = monitor_factory or start_id_zero_monitor
    monitor_stopper = monitor_stopper or stop_id_zero_monitor
    try:
        page = core_module.launch_ruyi_browser(
            "",
            headless=options.headless,
            snapshot_dir=options.snapshot_dir,
            cache_dir=options.cache_dir,
        )
        # Lightweight page doubles used by callers may not expose a browser
        # execution API. Skip only the default monitor in that case; an
        # explicitly injected factory is always honored.
        has_page_api = callable(getattr(page, "run_js", None))
        if not use_default_monitor or has_page_api:
            try:
                id_zero_monitor = monitor_factory(page)
            except Exception as exc:
                LOG.warning("启动按钮监控线程失败，继续执行主流程: %s", type(exc).__name__)
        LOG.info("打开微软邮箱页面: %s", options.url)
        with BROWSER_INTERACTION_LOCK:
            core_module.navigate_with_retry(
                page,
                options.url,
                "微软邮箱页面",
                timeout=options.timeout,
            )
        LOG.info("页面已打开；静态资源缓存 profile: %s", options.cache_dir)
        if credentials is not None:
            aliases = workflow(
                page,
                email=credentials.email,
                password=credentials.password,
                timeout=options.timeout,
                mail_timeout=options.mail_timeout,
                auxiliary_credentials_file=options.auxiliary_credentials_file,
                auxiliary_credentials=auxiliary_credentials,
            )
            if emit_alias_lines:
                print("\n".join(credentials.alias_credential_line(alias) for alias in aliases))
        elif options.keep_open:
            # All post-launch paths use the same explicit Enter confirmation.
            # Keep the option for CLI compatibility, but do not create a
            # second Ctrl+C-only lifetime mode.
            LOG.info("浏览器保持打开，完成检查后按 Enter 关闭")
        return 0
    except KeyboardInterrupt:
        LOG.info("收到中断，准备关闭浏览器")
        return 130
    except Exception:
        LOG.exception("打开微软邮箱页面失败")
        return 1
    finally:
        if page is not None:
            try:
                input_fn(CLOSE_PROMPT)
            except EOFError:
                LOG.warning("未收到关闭确认输入，继续关闭浏览器")
            except OSError as exc:
                LOG.warning("关闭确认输入不可用，继续关闭浏览器: %s", exc)
            except KeyboardInterrupt:
                LOG.info("关闭确认阶段收到中断，继续关闭浏览器")
            except Exception as exc:
                LOG.warning("关闭确认输入失败，继续关闭浏览器: %s", type(exc).__name__)
            if id_zero_monitor is not None:
                try:
                    monitor_stopper(
                        id_zero_monitor,
                        timeout=ID_ZERO_MONITOR_JOIN_TIMEOUT,
                    )
                except Exception:
                    LOG.exception("停止按钮监控线程失败")
            try:
                core_module.close_ruyi_browser(page, cache_dir=options.cache_dir)
            except Exception:
                LOG.exception("关闭 RuyiPage 失败")


def run_account(
    credentials: AccountCredentials,
    *,
    auxiliary_credentials: AccountCredentials | None = None,
    cache_dir: Path,
    snapshot_dir: Path,
    timeout: float = 60.0,
    mail_timeout: float = 180.0,
    headless: bool = True,
    core_module: Any = core,
    workflow: Any = login_and_add_aliases,
    monitor_factory: Any | None = None,
    monitor_stopper: Any | None = None,
) -> AccountRun:
    """Run one account without interactive stdin and return only safe metadata."""
    selected_auxiliary: AccountCredentials | None = auxiliary_credentials

    def auxiliary_loader(path: Path) -> AccountCredentials:
        nonlocal selected_auxiliary
        selected_auxiliary = load_auxiliary_credentials(path)
        return selected_auxiliary

    def workflow_adapter(page: Any, **kwargs: Any) -> list[str]:
        result = workflow(
            page,
            email=credentials.email,
            password=credentials.password,
            timeout=timeout,
            mail_timeout=mail_timeout,
            auxiliary_credentials=auxiliary_credentials,
            auxiliary_credentials_file=Path(kwargs.get("auxiliary_credentials_file", DEFAULT_AUXILIARY_CREDENTIALS_FILE)),
            auxiliary_loader=auxiliary_loader,
        )
        return list(result or [])

    options = OpenOptions(
        cache_dir=Path(cache_dir),
        snapshot_dir=Path(snapshot_dir),
        timeout=float(timeout),
        mail_timeout=float(mail_timeout),
        headless=bool(headless),
        keep_open=False,
        auxiliary_credentials_file=DEFAULT_AUXILIARY_CREDENTIALS_FILE,
    )
    captured: list[str] = []

    def capturing_workflow(page: Any, **kwargs: Any) -> list[str]:
        values = workflow_adapter(page, **kwargs)
        captured.extend(values)
        return values

    status = open_email_page(
        options,
        core_module=core_module,
        credentials=credentials,
        input_fn=lambda _prompt: "",
        workflow=capturing_workflow,
        monitor_factory=monitor_factory,
        monitor_stopper=monitor_stopper,
        emit_alias_lines=False,
        auxiliary_credentials=auxiliary_credentials,
    )
    if status != 0:
        raise RuntimeError("微软邮箱流程未完成")
    if len(captured) != 2:
        raise RuntimeError("未确认两个微软邮箱别名")
    return AccountRun(
        aliases=tuple(captured),
        auxiliary_email=selected_auxiliary.email if selected_auxiliary else "",
    )


def configure_logging(log_file: Path) -> None:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
        handlers=[logging.StreamHandler(), logging.FileHandler(log_file, encoding="utf-8")],
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    configure_logging(Path(args.log_file).expanduser().resolve())
    try:
        options = options_from_args(args)
        credentials = read_credentials()
    except ValueError as exc:
        LOG.error("参数错误: %s", exc)
        return 2
    return open_email_page(options, credentials=credentials)


if __name__ == "__main__":
    raise SystemExit(main())
