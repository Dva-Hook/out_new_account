#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Run one Microsoft-alias matrix task.

The matrix workflow assigns one mailbox to each process.  This module keeps
the browser/mail implementation behind an injectable attempt runner so the
task can be tested without starting a browser or contacting a mailbox.  The
only persisted contract is a deliberately small, non-secret ``result.json``.
"""

from __future__ import annotations

import argparse
import inspect
import json
import logging
import math
import os
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


LOG = logging.getLogger("microsoft_email_v6.register")
SCHEMA_VERSION = 1
MAX_ATTEMPTS = 3
FIELD_SEPARATOR = "----"
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


class SecretRedactionFilter(logging.Filter):
    """Replace credential values in every log record before formatting."""

    def __init__(self, secrets: Sequence[str]) -> None:
        super().__init__()
        self._secrets = tuple(
            sorted({value for value in secrets if value}, key=len, reverse=True)
        )

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            rendered = record.getMessage()
        except Exception:
            rendered = str(record.msg)
        for secret in self._secrets:
            rendered = rendered.replace(secret, "<已隐藏>")
        record.msg = rendered
        record.args = ()
        # Tracebacks can contain the original exception text; the concise
        # message above is sufficient for an Actions artifact.
        record.exc_info = None
        record.exc_text = None
        return True


def install_secret_redaction(secrets: Sequence[str]) -> SecretRedactionFilter:
    redactor = SecretRedactionFilter(secrets)
    root = logging.getLogger()
    for handler in root.handlers:
        handler.addFilter(redactor)
    LOG.addFilter(redactor)
    return redactor


@dataclass(frozen=True, slots=True, repr=False)
class JobAccount:
    """Credential-shaped input used by tests and by the CLI.

    Secret fields are retained only in memory while an attempt is running.
    ``repr`` intentionally exposes the address and source index only.
    """

    email: str
    password: str
    client_id: str
    token: str
    raw_line: str = ""
    source_index: int = 1

    def __post_init__(self) -> None:
        values = (self.email, self.password, self.client_id, self.token)
        if not all(isinstance(value, str) and value.strip() for value in values):
            raise ValueError("邮箱凭据四个字段都不能为空")
        email = self.email.strip()
        if not EMAIL_RE.fullmatch(email):
            raise ValueError("邮箱格式无效")
        object.__setattr__(self, "email", email)
        object.__setattr__(self, "password", self.password.strip())
        object.__setattr__(self, "client_id", self.client_id.strip())
        object.__setattr__(self, "token", self.token.strip())
        if not isinstance(self.source_index, int) or isinstance(self.source_index, bool):
            raise ValueError("source_index 必须是整数")
        if not self.raw_line:
            object.__setattr__(self, "raw_line", self.normalized_line)

    @property
    def mailbox_password(self) -> str:
        return self.password

    @property
    def refresh_token(self) -> str:
        return self.token

    @property
    def normalized_line(self) -> str:
        return FIELD_SEPARATOR.join((self.email, self.password, self.client_id, self.token))

    @property
    def api_line(self) -> str:
        return self.normalized_line

    def __repr__(self) -> str:
        return f"JobAccount(email={self.email!r}, source_index={self.source_index!r})"


def _field(value: object, *names: str, default: object = "") -> object:
    if isinstance(value, Mapping):
        for name in names:
            if name in value:
                return value[name]
    for name in names:
        try:
            return getattr(value, name)
        except AttributeError:
            continue
    return default


def coerce_account(account: object) -> JobAccount:
    """Return a :class:`JobAccount` from the local pool class or a mapping.

    ``email_pool.EmailCredential`` has used both ``password/token`` and
    ``mailbox_password/refresh_token`` names across project revisions; both
    spellings are accepted here.
    """

    if isinstance(account, JobAccount):
        return account
    if isinstance(account, str):
        try:
            from email_pool import parse_credential_line

            parsed = parse_credential_line(account, source_index=1)
            return JobAccount(
                parsed.email,
                str(_field(parsed, "password", "mailbox_password")),
                str(_field(parsed, "client_id")),
                str(_field(parsed, "token", "refresh_token")),
                str(_field(parsed, "raw_line", default=account)),
                int(_field(parsed, "source_index", default=1)),
            )
        except Exception as exc:
            raise ValueError("邮箱凭据格式无效") from exc

    email = str(_field(account, "email", "assigned_email", default="") or "").strip()
    password = str(
        _field(account, "password", "mailbox_password", default="") or ""
    ).strip()
    client_id = str(_field(account, "client_id", default="") or "").strip()
    token = str(_field(account, "token", "refresh_token", default="") or "").strip()
    raw_line = str(_field(account, "raw_line", "normalized_line", default="") or "")
    source_index_raw = _field(account, "source_index", default=1)
    try:
        source_index = int(source_index_raw)
    except (TypeError, ValueError):
        source_index = 1
    return JobAccount(email, password, client_id, token, raw_line, source_index)


def _invoke(callable_object: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Call a hook with only the keyword arguments it declares.

    This keeps test hooks intentionally small while still allowing production
    hooks to receive the complete context.  Functions accepting ``**kwargs``
    receive every value.
    """

    try:
        signature = inspect.signature(callable_object)
    except (TypeError, ValueError):
        return callable_object(*args, **kwargs)
    parameters = signature.parameters.values()
    if any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters):
        return callable_object(*args, **kwargs)
    accepted = {
        name: value
        for name, value in kwargs.items()
        if name in signature.parameters
        and signature.parameters[name].kind
        in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
    }
    return callable_object(*args, **accepted)


def _default_browser_launcher(
    *, profile_dir: Path, snapshot_dir: Path, headless: bool
) -> Any:
    """Launch the existing RuyiPage core without a proxy."""

    try:
        import ruyipage_core
    except ImportError as exc:  # pragma: no cover - exercised only in a runner
        raise RuntimeError("无法加载浏览器运行模块") from exc
    return ruyipage_core.launch_ruyi_browser(
        "",
        headless=bool(headless),
        snapshot_dir=snapshot_dir,
        cache_dir=profile_dir,
    )


def _default_browser_closer(page: Any, *, profile_dir: Path | None = None) -> None:
    try:
        import ruyipage_core
    except ImportError:
        # A custom test page may not require the optional browser package.
        close = getattr(page, "quit", None)
        if callable(close):
            _invoke(close)
        return
    ruyipage_core.close_ruyi_browser(page, cache_dir=profile_dir)


def _to_open_credentials(account: JobAccount) -> Any:
    try:
        import open_microsoft_email

        return open_microsoft_email.AccountCredentials(
            email=account.email,
            password=account.password,
            client_id=account.client_id,
            token=account.token,
        )
    except (ImportError, AttributeError):  # pragma: no cover - test-only fallback
        return account


def _default_performer(
    page: Any,
    account: JobAccount,
    *,
    profile_dir: Path,
    snapshot_dir: Path,
    form_timeout: float,
    mail_timeout: float,
    success_timeout: float,
    headless: bool,
    auxiliary_credentials: object | None = None,
    auxiliary_credentials_file: Path | None = None,
    static_cache_dir: Path | str | None = None,
) -> Any:
    try:
        import open_microsoft_email
    except ImportError as exc:  # pragma: no cover - exercised only in a runner
        raise RuntimeError("无法加载微软邮箱流程模块") from exc

    auxiliary = None
    if auxiliary_credentials is not None:
        try:
            auxiliary = _to_open_credentials(coerce_account(auxiliary_credentials))
        except (TypeError, ValueError):
            auxiliary = auxiliary_credentials
    selected_auxiliary = auxiliary
    selected_auxiliary_box: list[Any] = []

    def load_auxiliary(path: Path) -> Any:
        nonlocal selected_auxiliary
        selected_auxiliary = open_microsoft_email.load_auxiliary_credentials(path)
        selected_auxiliary_box.clear()
        selected_auxiliary_box.append(selected_auxiliary)
        return selected_auxiliary

    del success_timeout, headless
    # The launcher already owns this attempt's page.  Navigate that page and
    # run the workflow in-place so one attempt never creates a second browser.
    open_microsoft_email.core.navigate_with_retry(
        page,
        open_microsoft_email.DEFAULT_URL,
        "微软邮箱登录页",
        timeout=float(form_timeout),
    )
    # Snapshot only the anonymous login-page cache, before any mailbox
    # credentials are submitted.  Never persist a post-login cache response.
    _save_static_cache(profile_dir, static_cache_dir)
    monitor = None
    try:
        monitor = open_microsoft_email.start_id_zero_monitor(page)
    except Exception:
        monitor = None
    try:
        aliases = open_microsoft_email.login_and_add_aliases(
            page,
            email=account.email,
            password=account.password,
            timeout=float(form_timeout),
            mail_timeout=float(mail_timeout),
            auxiliary_credentials=auxiliary,
            auxiliary_credentials_file=auxiliary_credentials_file
            or open_microsoft_email.DEFAULT_AUXILIARY_CREDENTIALS_FILE,
            auxiliary_loader=load_auxiliary,
            auxiliary_result=selected_auxiliary_box,
            diagnostic_dir=snapshot_dir,
        )
        return {
            "aliases": list(aliases),
            "auxiliary_email": (
                str(getattr(selected_auxiliary, "email", "") or "").strip()
            ),
        }
    finally:
        if monitor is not None:
            try:
                open_microsoft_email.stop_id_zero_monitor(monitor)
            except Exception:
                LOG.debug("按钮监控线程停止失败", exc_info=False)


def run_single_attempt(
    *,
    account: object,
    profile_dir: Path,
    snapshot_dir: Path,
    headless: bool = True,
    form_timeout: float = 60.0,
    mail_timeout: float = 180.0,
    success_timeout: float = 30.0,
    browser_launcher: Callable[..., Any] | None = None,
    performer: Callable[..., Any] | None = None,
    browser_closer: Callable[..., Any] | None = None,
    auxiliary_credentials: object | None = None,
    auxiliary_credentials_file: Path | None = None,
    static_cache_dir: Path | str | None = None,
) -> Any:
    """Execute one browser attempt and close its page in all outcomes."""

    normalized = coerce_account(account)
    profile_dir.mkdir(parents=True, exist_ok=True)
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    launcher = browser_launcher or _default_browser_launcher
    closer = browser_closer or _default_browser_closer
    page: Any = None
    try:
        page = _invoke(
            launcher,
            profile_dir=profile_dir,
            snapshot_dir=snapshot_dir,
            headless=bool(headless),
        )
        operation = performer or _default_performer
        return _invoke(
            operation,
            page,
            account=normalized,
            profile_dir=profile_dir,
            snapshot_dir=snapshot_dir,
            headless=bool(headless),
            form_timeout=float(form_timeout),
            mail_timeout=float(mail_timeout),
            success_timeout=float(success_timeout),
            auxiliary_credentials=auxiliary_credentials,
            auxiliary_credentials_file=auxiliary_credentials_file,
            static_cache_dir=static_cache_dir,
        )
    finally:
        if page is not None:
            try:
                _invoke(closer, page, profile_dir=profile_dir)
            except Exception as exc:
                # Closure failure must not leak a credential or hide the
                # original attempt error; the profile is removed by run_job.
                LOG.warning("浏览器清理失败（%s）", type(exc).__name__)


def _derive_aliases(email: str) -> list[str]:
    local, separator, domain = email.partition("@")
    if not separator:
        return []
    return [f"{local}01@{domain}", f"{local}02@{domain}"]


def _normalise_email(value: object) -> str:
    return str(value or "").strip()


def _normalise_alias(value: object, account_email: str) -> str:
    alias = _normalise_email(value)
    if alias and "@" not in alias:
        alias = f"{alias}@{account_email.rsplit('@', 1)[1]}"
    return alias


def _normalise_outcome(
    outcome: object,
    account: JobAccount,
    auxiliary_credentials: object | None,
) -> tuple[list[str], str, dict[str, bool]]:
    """Extract the two confirmed aliases from a hook result."""

    if isinstance(outcome, Mapping):
        if outcome.get("success") is False:
            raise RuntimeError(str(outcome.get("error") or "流程返回失败"))
        aliases_value = outcome.get("aliases")
        if aliases_value is None:
            aliases_value = (outcome.get("alias0"), outcome.get("alias1"))
        auxiliary_value = outcome.get("auxiliary_email", "")
        verification_value = outcome.get("verification")
    else:
        aliases_value = getattr(outcome, "aliases", None)
        auxiliary_value = getattr(outcome, "auxiliary_email", "")
        verification_value = getattr(outcome, "verification", None)

    # A two-item tuple is a convenient lightweight hook return: (aliases, aux).
    if (
        isinstance(outcome, (tuple, list))
        and len(outcome) == 2
        and not all(isinstance(item, str) for item in outcome)
        and aliases_value is None
    ):
        aliases_value, auxiliary_value = outcome

    if aliases_value is None:
        # ``None``/True is treated as a successful browser flow with the
        # conventional 01/02 aliases.  The production adapter returns actual
        # DOM-confirmed aliases; this fallback keeps test hooks minimal.
        if outcome is None or outcome is True:
            aliases_value = _derive_aliases(account.email)
        else:
            aliases_value = outcome

    if isinstance(aliases_value, str):
        aliases = [
            _normalise_alias(part, account.email)
            for part in re.split(r"[,\n;]+", aliases_value)
            if part.strip()
        ]
    else:
        try:
            aliases = [
                _normalise_alias(value, account.email) for value in aliases_value
            ]  # type: ignore[arg-type]
        except TypeError as exc:
            raise RuntimeError("流程未返回两个邮箱别名") from exc
    aliases = [value for value in aliases if value]
    if len(aliases) != 2 or any(not EMAIL_RE.fullmatch(value) for value in aliases):
        raise RuntimeError("流程未确认两个邮箱别名")

    if not auxiliary_value and auxiliary_credentials is not None:
        auxiliary_value = _field(auxiliary_credentials, "email", default="")
    auxiliary_email = _normalise_email(auxiliary_value)

    verification = {"alias0": True, "alias1": True}
    if isinstance(verification_value, Mapping):
        # Explicit false flags are never promoted to success.
        if verification_value.get("alias0") is False or verification_value.get("alias1") is False:
            raise RuntimeError("邮箱别名验证未完成")
        if "alias0" in verification_value and verification_value.get("alias0") is not True:
            raise RuntimeError("邮箱别名验证未完成")
        if "alias1" in verification_value and verification_value.get("alias1") is not True:
            raise RuntimeError("邮箱别名验证未完成")
    return aliases, auxiliary_email, verification


def _safe_error(exc: BaseException, account: JobAccount) -> str:
    """Return a bounded error label without copying exception text to artifacts.

    Browser and mail libraries may include request URLs, mailbox tokens, or
    auxiliary credentials in exception strings.  The result artifact is
    intentionally diagnostic only, so retain the exception type and discard
    the potentially sensitive message entirely.
    """

    del account
    error_type = type(exc).__name__ or "UnknownError"
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", error_type):
        error_type = "UnknownError"
    return f"操作失败: {error_type}"


def validate_timeout(
    value: float | int | str,
    *,
    label: str = "超时秒数",
    minimum: float = 0.1,
    maximum: float = 3600.0,
) -> float:
    """Validate a finite timeout before it reaches browser or mail polling."""

    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label}必须是数字") from exc
    if not math.isfinite(parsed) or not minimum <= parsed <= maximum:
        raise ValueError(f"{label}必须在 {minimum:g}-{maximum:g} 秒范围内")
    return parsed


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    try:
        temporary.write_text(text, encoding="utf-8", newline="\n")
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _seed_static_cache(profile_dir: Path, static_cache_dir: Path | str | None) -> None:
    """Copy only HTTP cache files into a fresh attempt profile.

    Cookies, storage, login databases and session files are never copied from
    the seed.  A missing cache is normal on the first Actions run.
    """

    if static_cache_dir is None:
        return
    source_root = Path(static_cache_dir).expanduser().resolve()
    source = source_root / "cache2" if (source_root / "cache2").is_dir() else source_root
    if not source.is_dir():
        return
    target = profile_dir / "cache2"
    shutil.copytree(source, target, dirs_exist_ok=True)


def _save_static_cache(profile_dir: Path, static_cache_dir: Path | str | None) -> None:
    """Persist only the browser HTTP cache, never account identity files."""

    if static_cache_dir is None:
        return
    source = profile_dir / "cache2"
    if not source.is_dir():
        return
    target_root = Path(static_cache_dir).expanduser().resolve()
    target = target_root / "cache2"
    target_root.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target, dirs_exist_ok=True)


def _write_result(path: Path, result: Mapping[str, Any]) -> None:
    _atomic_write_text(
        path,
        json.dumps(dict(result), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )


def format_account_record(
    *,
    auxiliary_email: str,
    assigned_email: str,
    aliases: Sequence[str],
) -> str:
    """Render the four-line, non-secret task output."""

    values = list(aliases)
    if len(values) != 2:
        raise ValueError("必须提供两个邮箱别名")
    return (
        f"辅邮：{auxiliary_email}\n"
        f"主邮：{assigned_email}\n"
        f"子邮1：{values[0]}\n"
        f"子邮2：{values[1]}\n"
    )


def _safe_page_for_hook(
    *,
    runner: Callable[..., Any],
    browser_launcher: Callable[..., Any] | None,
    profile_dir: Path,
    snapshot_dir: Path,
    headless: bool,
) -> tuple[Any, bool]:
    """Optionally create a page for a custom hook that explicitly requests it."""

    try:
        signature = inspect.signature(runner)
        wants_page = "page" in signature.parameters
    except (TypeError, ValueError):
        wants_page = False
    if wants_page and browser_launcher is not None:
        return (
            _invoke(
                browser_launcher,
                profile_dir=profile_dir,
                snapshot_dir=snapshot_dir,
                headless=headless,
            ),
            True,
        )
    # A stable sentinel lets a test hook exercise close semantics without
    # importing the optional browser package.
    return object(), False


def run_job(
    account: object,
    output_dir: Path | str,
    *,
    job_index: int = 1,
    max_attempts: int = MAX_ATTEMPTS,
    headless: bool = True,
    form_timeout: float = 60.0,
    mail_timeout: float = 180.0,
    success_timeout: float = 30.0,
    attempt_runner: Callable[..., Any] | None = None,
    browser_launcher: Callable[..., Any] | None = None,
    browser_closer: Callable[..., Any] | None = None,
    performer: Callable[..., Any] | None = None,
    auxiliary_credentials: object | None = None,
    auxiliary_credentials_file: Path | str | None = None,
    static_cache_dir: Path | str | None = None,
) -> dict[str, Any]:
    """Run one assigned mailbox with at most three isolated attempts.

    Every normal return writes ``output_dir/result.json``.  Failed attempts are
    represented only by a redacted ``error`` string; no password, token, or
    complete four-field credential line is persisted.
    """

    if isinstance(job_index, bool) or not isinstance(job_index, int) or job_index < 1:
        raise ValueError("任务索引必须是正整数")
    if isinstance(max_attempts, bool) or not isinstance(max_attempts, int):
        raise ValueError("最大尝试次数必须是整数")
    if not 1 <= max_attempts <= MAX_ATTEMPTS:
        raise ValueError(f"最大尝试次数必须在 1-{MAX_ATTEMPTS} 范围内")
    form_timeout = validate_timeout(form_timeout, label="表单超时秒数")
    mail_timeout = validate_timeout(mail_timeout, label="邮件超时秒数")
    success_timeout = validate_timeout(success_timeout, label="成功确认超时秒数")
    normalized = coerce_account(account)
    root = Path(output_dir).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    result_path = root / "result.json"
    account_path = root / "账号.txt"
    # Do not leave a stale success artifact when a rerun fails.
    try:
        account_path.unlink()
    except FileNotFoundError:
        pass

    auxiliary_path = (
        Path(auxiliary_credentials_file).expanduser().resolve()
        if auxiliary_credentials_file is not None
        else None
    )
    runner = attempt_runner
    if runner is None:
        runner = run_single_attempt

    aliases: list[str] = []
    auxiliary_email = ""
    last_error: str | None = None
    completed = 0

    for attempt_number in range(1, max_attempts + 1):
        completed = attempt_number
        profile_dir = root / "profiles" / f"attempt-{attempt_number}"
        snapshot_dir = root / "失败截图" / f"attempt-{attempt_number}"
        # A retry always starts from a clean identity directory.
        shutil.rmtree(profile_dir, ignore_errors=True)
        profile_dir.mkdir(parents=True, exist_ok=False)
        _seed_static_cache(profile_dir, static_cache_dir)
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        LOG.info("开始第 %d/%d 次邮箱别名尝试: %s", attempt_number, max_attempts, normalized.email)

        custom_page: Any = None
        custom_page_launched = False
        try:
            if runner is run_single_attempt:
                outcome = _invoke(
                    runner,
                    account=normalized,
                    profile_dir=profile_dir,
                    snapshot_dir=snapshot_dir,
                    headless=bool(headless),
                    form_timeout=form_timeout,
                    mail_timeout=mail_timeout,
                    success_timeout=success_timeout,
                    browser_launcher=browser_launcher,
                    performer=performer,
                    browser_closer=browser_closer,
                    auxiliary_credentials=auxiliary_credentials,
                    auxiliary_credentials_file=auxiliary_path,
                    static_cache_dir=static_cache_dir,
                )
            else:
                custom_page, custom_page_launched = _safe_page_for_hook(
                    runner=runner,
                    browser_launcher=browser_launcher,
                    profile_dir=profile_dir,
                    snapshot_dir=snapshot_dir,
                    headless=bool(headless),
                )
                outcome = _invoke(
                    runner,
                    account=normalized,
                    page=custom_page,
                    profile_dir=profile_dir,
                    snapshot_dir=snapshot_dir,
                    headless=bool(headless),
                    form_timeout=form_timeout,
                    mail_timeout=mail_timeout,
                    success_timeout=success_timeout,
                    auxiliary_credentials=auxiliary_credentials,
                    auxiliary_credentials_file=auxiliary_path,
                    static_cache_dir=static_cache_dir,
                )
            aliases, auxiliary_email, _verification = _normalise_outcome(
                outcome, normalized, auxiliary_credentials
            )
            # A later successful attempt supersedes errors from earlier
            # retries; retain only the final success state in the artifact.
            last_error = None
            LOG.info("邮箱别名确认完成: %s", normalized.email)
            break
        except Exception as exc:
            last_error = _safe_error(exc, normalized)
            LOG.warning("第 %d/%d 次尝试失败: %s", attempt_number, max_attempts, last_error)
        finally:
            if custom_page is not None and browser_closer is not None:
                # For a custom runner the caller owns the launch operation; an
                # explicit closer still receives one deterministic cleanup call.
                try:
                    _invoke(browser_closer, custom_page, profile_dir=profile_dir)
                except Exception as exc:
                    LOG.warning("浏览器清理失败（%s）", type(exc).__name__)
            # Never retain browser identity state between attempts.  Keep a
            # non-empty snapshot directory as a diagnostic artifact.
            shutil.rmtree(profile_dir, ignore_errors=True)
            try:
                if snapshot_dir.exists() and not any(snapshot_dir.iterdir()):
                    snapshot_dir.rmdir()
            except OSError:
                pass

    success = len(aliases) == 2 and last_error is None
    if success:
        result: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "success": True,
            "job_index": job_index,
            "assigned_email": normalized.email,
            "aliases": aliases,
            "auxiliary_email": auxiliary_email,
            "verification": {"alias0": True, "alias1": True},
            "error": None,
        }
        _atomic_write_text(
            account_path,
            format_account_record(
                auxiliary_email=auxiliary_email,
                assigned_email=normalized.email,
                aliases=aliases,
            ),
        )
    else:
        result = {
            "schema_version": SCHEMA_VERSION,
            "success": False,
            "job_index": job_index,
            "assigned_email": normalized.email,
            "aliases": [],
            "auxiliary_email": "",
            "verification": {"alias0": False, "alias1": False},
            "error": last_error or "邮箱别名流程未完成",
        }
    _write_result(result_path, result)
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="执行一个微软邮箱别名矩阵任务")
    parser.add_argument("--邮箱文件", default="verified_email.txt", help="四字段邮箱池文件")
    parser.add_argument("--任务索引", type=int, required=True, help="从 1 开始的任务索引")
    parser.add_argument("--输出目录", required=True, help="当前任务 Artifact 目录")
    parser.add_argument(
        "--最大尝试次数",
        type=int,
        choices=range(1, MAX_ATTEMPTS + 1),
        default=MAX_ATTEMPTS,
        help="单个邮箱最多尝试三次，每次使用独立身份目录",
    )
    parser.add_argument(
        "--表单超时秒数",
        type=lambda value: validate_timeout(value, label="表单超时秒数"),
        default=60.0,
    )
    parser.add_argument(
        "--邮件超时秒数",
        type=lambda value: validate_timeout(value, label="邮件超时秒数"),
        default=180.0,
    )
    parser.add_argument(
        "--成功确认超时秒数",
        type=lambda value: validate_timeout(value, label="成功确认超时秒数"),
        default=30.0,
    )
    parser.add_argument("--绑定辅助邮箱", default=None, help="可选的辅助邮箱四字段凭据")
    parser.add_argument("--静态缓存目录", default=None, help="仅恢复 HTTP 静态资源缓存，不恢复身份状态")
    parser.add_argument("--无头", action="store_true", help="使用无头浏览器")
    return parser


def _configure_logging(output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    if not logging.getLogger().handlers:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s [%(levelname)s] %(message)s",
            handlers=(logging.StreamHandler(sys.stdout),),
        )


def _parse_auxiliary(raw: str | None) -> object | None:
    if not raw or not raw.strip():
        raw = os.environ.get("AUXILIARY_CREDENTIALS", "")
    if not raw or not raw.strip():
        return None
    try:
        from email_pool import parse_credential_line

        return parse_credential_line(raw, source_index=1)
    except Exception as exc:
        raise ValueError("绑定辅助邮箱格式错误") from exc


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    output_dir = Path(args.输出目录).expanduser().resolve()
    _configure_logging(output_dir)
    try:
        from email_pool import select_email

        account = select_email(Path(args.邮箱文件), args.任务索引)
        auxiliary = _parse_auxiliary(args.绑定辅助邮箱)
        secrets = [account.password, account.client_id, account.token]
        if auxiliary is not None:
            secrets.extend(
                [
                    str(_field(auxiliary, "password", "mailbox_password", default="")),
                    str(_field(auxiliary, "client_id", default="")),
                    str(_field(auxiliary, "token", "refresh_token", default="")),
                ]
            )
        install_secret_redaction(secrets)
        result = run_job(
            account,
            output_dir,
            job_index=args.任务索引,
            max_attempts=args.最大尝试次数,
            headless=bool(args.无头),
            form_timeout=args.表单超时秒数,
            mail_timeout=args.邮件超时秒数,
            success_timeout=args.成功确认超时秒数,
            auxiliary_credentials=auxiliary,
            static_cache_dir=args.静态缓存目录,
        )
    except KeyboardInterrupt:
        LOG.warning("收到中断，当前任务停止")
        return 130
    except (ValueError, IndexError, FileNotFoundError) as exc:
        LOG.error("任务输入无效: %s", str(exc))
        return 2
    except Exception as exc:
        # Do not echo exception text here: an unexpected dependency may embed
        # request data.  run_job's artifact path remains the diagnostic source.
        LOG.error("任务执行异常: %s", type(exc).__name__)
        return 2
    return 0 if result.get("success") is True else 1


parse_args = build_parser


__all__ = [
    "EMAIL_RE",
    "JobAccount",
    "MAX_ATTEMPTS",
    "SCHEMA_VERSION",
    "build_parser",
    "coerce_account",
    "format_account_record",
    "main",
    "parse_args",
    "run_job",
    "run_single_attempt",
    "install_secret_redaction",
    "SecretRedactionFilter",
]


if __name__ == "__main__":
    raise SystemExit(main())
