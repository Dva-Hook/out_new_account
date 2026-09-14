#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Validate matrix results and atomically consume successful mailboxes.

The register jobs deliberately write a small, non-secret ``result.json``.
This module is the only component that mutates ``verified_email.txt`` and the
human-readable success file.  It accepts only results that prove both aliases
were confirmed, so a failed or incomplete browser attempt remains available
for a later workflow run.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from email_pool import remove_consumed_emails


SCHEMA_VERSION = 1
SUCCESS_OUTPUT_LABELS = ("辅邮：", "主邮：", "子邮1：", "子邮2：")
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


@dataclass(frozen=True, slots=True, repr=False)
class SuccessRecord:
    """Safe metadata for one fully verified account.

    No password, client id, token, or raw credential line is retained here.
    ``repr`` is intentionally redacted as this object is often logged by
    test runners and Actions diagnostics.
    """

    main_email: str
    aliases: tuple[str, str]
    auxiliary_email: str
    job_index: int

    @property
    def email(self) -> str:
        """Compatibility alias used by older result consumers."""

        return self.main_email

    @property
    def assigned_email(self) -> str:
        return self.main_email

    @property
    def alias1(self) -> str:
        return self.aliases[0]

    @property
    def alias2(self) -> str:
        return self.aliases[1]

    def __repr__(self) -> str:
        return (
            f"SuccessRecord(main_email={self.main_email!r}, "
            f"aliases={self.aliases!r}, auxiliary_email={self.auxiliary_email!r}, "
            f"job_index={self.job_index!r})"
        )


# Names used by earlier V6 drafts.  They are aliases, not separate schemas.
AcceptedAccount = SuccessRecord
AcceptedResult = SuccessRecord


@dataclass(frozen=True, slots=True)
class CollectionSummary:
    accepted: tuple[SuccessRecord, ...]
    total_artifacts: int
    failed: int
    ignored: int
    missing: int = 0

    @property
    def successful(self) -> int:
        return len(self.accepted)

    @property
    def total_tasks(self) -> int:
        return self.total_artifacts + self.missing

    @property
    def total(self) -> int:
        return self.total_tasks


@dataclass(frozen=True, slots=True)
class ApplySummary:
    total_tasks: int
    successful: int
    failed: int
    ignored: int
    missing: int
    appended: int
    removed: int
    remaining: int

    @property
    def total_artifacts(self) -> int:
        return self.total_tasks - self.missing

    @property
    def accepted(self) -> int:
        return self.successful


def _nonempty_string(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _email(value: object) -> str | None:
    normalized = _nonempty_string(value)
    if normalized is None or EMAIL_RE.fullmatch(normalized) is None:
        return None
    return normalized


def _positive_job_index(value: object) -> int | None:
    if type(value) is not int or value < 1:
        return None
    return value


def _normalise_alias(value: object, *, main_email: str) -> str | None:
    alias = _nonempty_string(value)
    if alias is None:
        return None
    if "@" not in alias:
        domain = main_email.rsplit("@", 1)[1]
        alias = f"{alias}@{domain}"
    return alias if EMAIL_RE.fullmatch(alias) else None


def _expected_aliases(main_email: str) -> tuple[str, str]:
    local, domain = main_email.rsplit("@", 1)
    return f"{local}01@{domain}", f"{local}02@{domain}"


def _validate_success_payload(payload: Mapping[str, Any]) -> SuccessRecord | None:
    """Return safe metadata when *payload* proves a complete success."""

    if type(payload.get("schema_version")) is not int or payload["schema_version"] != SCHEMA_VERSION:
        return None
    if payload.get("success") is not True:
        return None

    assigned_email = _email(payload.get("assigned_email"))
    job_index = _positive_job_index(payload.get("job_index"))
    auxiliary_raw = payload.get("auxiliary_email", "")
    auxiliary_email = "" if auxiliary_raw in (None, "") else _email(auxiliary_raw)
    if assigned_email is None or job_index is None or auxiliary_email is None:
        return None

    verification = payload.get("verification")
    if not isinstance(verification, Mapping):
        return None
    # ``is True`` intentionally rejects truthy strings and integer 1 values.
    if verification.get("alias0") is not True or verification.get("alias1") is not True:
        return None

    aliases_value = payload.get("aliases")
    if not isinstance(aliases_value, (list, tuple)) or len(aliases_value) != 2:
        return None
    aliases = tuple(
        _normalise_alias(value, main_email=assigned_email) for value in aliases_value
    )
    if any(value is None for value in aliases):
        return None
    alias_pair = (aliases[0], aliases[1])
    expected = _expected_aliases(assigned_email)
    if tuple(value.casefold() for value in alias_pair) != tuple(
        value.casefold() for value in expected
    ):
        return None

    # Some writers include an optional account object.  If its public email is
    # present, ensure it agrees with assigned_email; never inspect/copy secret
    # fields from that object.
    account = payload.get("account")
    if account is not None:
        if not isinstance(account, Mapping):
            return None
        account_email = account.get("email")
        if account_email is not None:
            account_email = _email(account_email)
            if account_email is None or account_email.casefold() != assigned_email.casefold():
                return None

    # Type narrowing for the tuple after the None checks above.
    assert all(isinstance(value, str) for value in alias_pair)
    return SuccessRecord(
        main_email=assigned_email,
        aliases=(alias_pair[0], alias_pair[1]),
        auxiliary_email=auxiliary_email,
        job_index=job_index,
    )


def validate_result(payload: object) -> SuccessRecord | None:
    """Public, side-effect-free validator for one decoded result payload."""

    if not isinstance(payload, Mapping):
        return None
    return _validate_success_payload(payload)


def _result_paths(results_dir: Path | str) -> list[Path]:
    root = Path(results_dir).expanduser().resolve()
    if root.is_file():
        return [root] if root.name.casefold() == "result.json" else []
    if not root.is_dir():
        return []
    return sorted(
        (path for path in root.rglob("result.json") if path.is_file()),
        key=lambda path: path.as_posix().casefold(),
    )


def _read_payload(path: Path) -> object | None:
    try:
        text = path.read_text(encoding="utf-8-sig")
        return json.loads(text)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None


def collect_results(
    results_dir: Path | str,
    *,
    expected_tasks: int | None = None,
) -> CollectionSummary:
    """Collect valid result artifacts without exposing credential fields.

    Missing, unreadable, malformed, mismatched, and partially verified results
    are counted as ignored.  A schema-valid ``success: false`` artifact is
    counted as failed.  Duplicate successful main addresses are idempotently
    ignored using case-insensitive comparison.
    """

    if expected_tasks is not None:
        if type(expected_tasks) is not int or expected_tasks < 0:
            raise ValueError("预期任务数量必须是非负整数")

    paths = _result_paths(results_dir)
    if expected_tasks is not None and expected_tasks < len(paths):
        raise ValueError(
            f"预期任务数量 {expected_tasks} 小于已发现 Artifact 数量 {len(paths)}"
        )

    accepted: list[SuccessRecord] = []
    seen_emails: set[str] = set()
    expected_indices = (
        set(range(1, expected_tasks + 1))
        if expected_tasks is not None
        else None
    )
    observed_indices: set[int] = set()
    failed = 0
    ignored = 0
    for path in paths:
        payload = _read_payload(path)
        if not isinstance(payload, Mapping):
            ignored += 1
            continue
        # A matrix result is authoritative only for one expected, unique
        # one-based index.  This prevents an out-of-range or duplicated
        # artifact from being mistaken for a different mailbox.  The missing
        # index remains in the pool and is reported below.
        if expected_indices is not None:
            job_index = _positive_job_index(payload.get("job_index"))
            if (
                job_index is None
                or job_index not in expected_indices
                or job_index in observed_indices
            ):
                ignored += 1
                continue
        schema_ok = type(payload.get("schema_version")) is int and payload.get(
            "schema_version"
        ) == SCHEMA_VERSION
        if not schema_ok:
            ignored += 1
            continue
        if expected_indices is not None:
            # Only a schema-valid result occupies its matrix slot.  A malformed
            # artifact must remain visible as missing so it cannot silently
            # consume a mailbox or make the run look complete.
            observed_indices.add(job_index)
        if schema_ok and payload.get("success") is False:
            failed += 1
            continue
        record = _validate_success_payload(payload)
        if record is None:
            ignored += 1
            continue
        key = record.main_email.casefold()
        if key in seen_emails:
            ignored += 1
            continue
        seen_emails.add(key)
        accepted.append(record)

    # Matrix jobs are identified by their one-based index.  Sorting here makes
    # output deterministic even when artifact download order changes.
    accepted.sort(key=lambda item: (item.job_index, item.main_email.casefold()))
    expected = len(paths) if expected_tasks is None else expected_tasks
    missing = (
        0
        if expected_indices is None
        else len(expected_indices - observed_indices)
    )
    return CollectionSummary(
        accepted=tuple(accepted),
        total_artifacts=len(paths),
        failed=failed,
        ignored=ignored,
        missing=missing,
    )


def format_success_record(record: SuccessRecord) -> str:
    """Render exactly the four safe labels used in ``成功账号.txt``."""

    return (
        f"辅邮：{record.auxiliary_email}\n"
        f"主邮：{record.main_email}\n"
        f"子邮1：{record.aliases[0]}\n"
        f"子邮2：{record.aliases[1]}\n\n"
    )


# Compatibility spelling used by a few callers.
format_account_record = format_success_record


def _atomic_write_text(path: Path, text: str) -> None:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    try:
        if temporary.exists():
            temporary.unlink()
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except OSError:
                pass
        os.replace(temporary, path)
    finally:
        try:
            if temporary.exists():
                temporary.unlink()
        except OSError:
            pass


def _existing_main_emails(text: str) -> set[str]:
    """Find already-rendered main addresses, case-insensitively."""

    found: set[str] = set()
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith("主邮："):
            value = line[len("主邮：") :].strip()
            if _email(value) is not None:
                found.add(value.casefold())
    return found


def apply_results(
    results_dir: Path | str,
    pool_path: Path | str,
    output_path: Path | str,
    *,
    expected_tasks: int | None = None,
) -> ApplySummary:
    """Append new safe records and consume only their confirmed main emails."""

    pool = Path(pool_path).expanduser().resolve()
    output = Path(output_path).expanduser().resolve()
    if pool == output:
        raise ValueError("邮箱文件和成功输出文件不能是同一路径")

    collection = collect_results(results_dir, expected_tasks=expected_tasks)
    existing = output.read_text(encoding="utf-8-sig") if output.is_file() else ""
    completed = _existing_main_emails(existing)
    additions = [
        record
        for record in collection.accepted
        if record.main_email.casefold() not in completed
    ]
    if additions:
        prefix = existing
        if prefix and not prefix.endswith(("\n", "\r")):
            prefix += "\n"
        _atomic_write_text(output, prefix + "".join(format_success_record(item) for item in additions))
    elif not output.is_file():
        _atomic_write_text(output, "")

    # Remove all accepted addresses, including records already present in the
    # output file.  This makes a rerun recover from a crash between the output
    # replacement and pool replacement while retaining idempotence.
    removal = remove_consumed_emails(
        pool, (record.main_email for record in collection.accepted)
    )
    total_tasks = len(collection.accepted) + collection.failed + collection.ignored + collection.missing
    if expected_tasks is not None:
        total_tasks = expected_tasks
    return ApplySummary(
        total_tasks=total_tasks,
        successful=len(collection.accepted),
        failed=collection.failed,
        ignored=collection.ignored,
        missing=collection.missing,
        appended=len(additions),
        removed=removal.removed,
        remaining=removal.remaining,
    )


def render_actions_summary(summary: ApplySummary) -> str:
    """Render a count-only Actions Summary; no credential values are included."""

    return (
        "# 微软邮箱别名汇总\n\n"
        "| 项目 | 数量 |\n"
        "| --- | ---: |\n"
        f"| 任务总数 | {summary.total_tasks} |\n"
        f"| 成功账号 | {summary.successful} |\n"
        f"| 失败任务 | {summary.failed} |\n"
        f"| 忽略结果 | {summary.ignored} |\n"
        f"| 缺失结果 | {summary.missing} |\n"
        f"| 新增输出 | {summary.appended} |\n"
        f"| 删除主邮箱 | {summary.removed} |\n"
        f"| 剩余邮箱 | {summary.remaining} |\n"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="汇总微软邮箱别名任务结果并回写邮箱池")
    parser.add_argument("--结果目录", "--results-dir", "--input", dest="results_dir", required=True)
    parser.add_argument("--邮箱文件", "--pool", dest="pool_path", default="verified_email.txt")
    parser.add_argument(
        "--输出文件",
        "--成功账号文件",
        "--output",
        dest="output_path",
        default="成功账号.txt",
    )
    parser.add_argument("--摘要文件", "--summary", dest="summary_path")
    parser.add_argument("--预期任务数量", "--expected-tasks", dest="expected_tasks", type=int)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        summary = apply_results(
            args.results_dir,
            args.pool_path,
            args.output_path,
            expected_tasks=args.expected_tasks,
        )
    except Exception as exc:
        # Exception text is generated from paths/counts only.  Never print a
        # decoded artifact or a credential line.
        print(f"结果汇总失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    rendered = render_actions_summary(summary)
    summary_path = args.summary_path or os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        target = Path(summary_path).expanduser().resolve()
        existing = target.read_text(encoding="utf-8") if target.is_file() else ""
        _atomic_write_text(target, existing + rendered)
    print(
        "结果汇总完成："
        f"任务 {summary.total_tasks}，成功 {summary.successful}，"
        f"失败 {summary.failed}，忽略 {summary.ignored}，"
        f"缺失 {summary.missing}，删除主邮箱 {summary.removed}，"
        f"剩余邮箱 {summary.remaining}"
    )
    return 0


__all__ = [
    "AcceptedAccount",
    "AcceptedResult",
    "ApplySummary",
    "CollectionSummary",
    "SuccessRecord",
    "apply_results",
    "build_parser",
    "collect_results",
    "format_account_record",
    "format_success_record",
    "main",
    "render_actions_summary",
    "validate_result",
]


if __name__ == "__main__":
    raise SystemExit(main())
