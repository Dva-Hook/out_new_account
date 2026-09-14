#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""邮箱池解析、矩阵分配和原子消费工具。

The Actions workflow deliberately keeps all mutation in the collect job.  The
helpers in this module therefore read the pool afresh for every operation and
write replacements through ``os.replace`` so a partially written pool is never
observed by a later step.
"""

from __future__ import annotations

import os
import re
import argparse
import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence


MAX_TASKS = 256
MAX_PARALLEL = 20
FIELD_SEPARATOR = "----"
EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
PAGE_TIMEOUT_MIN = 30.0
PAGE_TIMEOUT_MAX = 300.0
MAIL_TIMEOUT_MIN = 30.0
MAIL_TIMEOUT_MAX = 600.0


@dataclass(frozen=True, slots=True, repr=False)
class EmailCredential:
    """A normalised four-field mailbox record.

    ``raw_line`` is retained without its line ending so the collect job can
    preserve untouched records.  Secret values intentionally do not appear in
    ``repr``; this keeps accidental logging of a credential object harmless.
    """

    email: str
    password: str
    client_id: str
    token: str
    raw_line: str = ""
    source_index: int = 0

    @property
    def mailbox_password(self) -> str:
        """Compatibility name used by the earlier V6 components."""

        return self.password

    @property
    def refresh_token(self) -> str:
        """Compatibility name used by Graph/O2 mail readers."""

        return self.token

    @property
    def normalized_line(self) -> str:
        """Return the canonical four-field representation."""

        return FIELD_SEPARATOR.join(
            (self.email, self.password, self.client_id, self.token)
        )

    @property
    def api_line(self) -> str:
        """Alias retained for result writers that call the credential line API."""

        return self.normalized_line

    def __repr__(self) -> str:
        # Do not include password, client_id, token, raw_line, or any derived
        # string containing them.  The source index is useful when diagnosing a
        # malformed pool and is not sensitive.
        return (
            f"EmailCredential(email={self.email!r}, "
            f"source_index={self.source_index!r})"
        )


@dataclass(frozen=True, slots=True)
class RemovalResult:
    """Summary returned by :func:`remove_consumed`."""

    requested: int
    removed: int
    removed_emails: Sequence[str]
    remaining: int


def _path(path: Path | str) -> Path:
    value = Path(path)
    return value.expanduser()


def _format_error(source_index: int | None = None) -> ValueError:
    if source_index is None:
        return ValueError("凭据格式错误，需要四段：邮箱----密码----client_id----令牌")
    return ValueError(
        f"第 {source_index} 行凭据格式错误，需要四段：邮箱----密码----client_id----令牌"
    )


def _validate_source_index(source_index: int) -> int:
    if isinstance(source_index, bool) or not isinstance(source_index, int) or source_index < 1:
        raise ValueError("source_index 必须是正整数")
    return source_index


def parse_credential_line(raw: str, *, source_index: int) -> EmailCredential:
    """Parse one ``邮箱----密码----client_id----令牌`` record.

    Surrounding whitespace is ignored for each field, while ``raw_line`` keeps
    the original text (apart from a possible line ending).  Values are not
    echoed in validation errors.
    """

    source_index = _validate_source_index(source_index)
    if not isinstance(raw, str):
        raise _format_error(source_index)

    raw_line = raw.rstrip("\r\n")
    parse_value = raw_line.lstrip("\ufeff")
    # Split only the three separators between fields; a token may itself
    # contain the separator sequence.
    parts = parse_value.split(FIELD_SEPARATOR, 3)
    if len(parts) != 4:
        raise _format_error(source_index)
    fields = tuple(part.strip() for part in parts)
    if not all(fields):
        raise _format_error(source_index)

    email, password, client_id, token = fields
    if not EMAIL_PATTERN.fullmatch(email):
        raise ValueError(f"第 {source_index} 行邮箱格式无效")

    return EmailCredential(
        email=email,
        password=password,
        client_id=client_id,
        token=token,
        raw_line=raw_line,
        source_index=source_index,
    )


def _read_pool_lines(path: Path) -> tuple[list[str], bool, bool]:
    """Read text lines while retaining line-ending and BOM information.

    The first boolean reports whether the file had a UTF-8 BOM and the second
    reports whether its final non-empty/empty line ended with a line terminator.
    ``newline=''`` prevents Python from silently changing CRLF while reading.
    """

    if not path.is_file():
        raise FileNotFoundError(f"邮箱文件不存在: {path}")
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        text = handle.read()
    # utf-8-sig consumes the marker, so inspect the bytes only when present.
    try:
        had_bom = path.read_bytes().startswith(b"\xef\xbb\xbf")
    except OSError:
        had_bom = False
    if not text:
        return [], had_bom, False
    raw_lines = text.splitlines(keepends=True)
    lines: list[str] = []
    for value in raw_lines:
        lines.append(value.rstrip("\r\n"))
    final_terminated = bool(raw_lines and raw_lines[-1].endswith(("\n", "\r")))
    return lines, had_bom, final_terminated


def load_email_pool(path: Path | str) -> list[EmailCredential]:
    """Load and validate all non-blank records from ``path``.

    Records are returned in file order.  Duplicate email addresses are rejected
    case-insensitively because matrix indexes must identify one stable mailbox.
    """

    pool_path = _path(path)
    lines, _had_bom, _final_terminated = _read_pool_lines(pool_path)
    pool: list[EmailCredential] = []
    seen: set[str] = set()
    for physical_index, line in enumerate(lines, start=1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        credential = parse_credential_line(line, source_index=physical_index)
        key = credential.email.casefold()
        if key in seen:
            raise ValueError(f"第 {physical_index} 行邮箱重复")
        seen.add(key)
        pool.append(credential)
    return pool


def matrix_indices(task_count: int) -> list[int]:
    """Return one-based matrix indexes, enforcing the V6 task ceiling."""

    if isinstance(task_count, bool) or not isinstance(task_count, int):
        raise ValueError("任务数量必须是整数")
    if task_count < 0 or task_count > MAX_TASKS:
        raise ValueError(f"任务数量必须在 0-{MAX_TASKS} 范围内")
    return list(range(1, task_count + 1))


def _parse_positive_int(value: str | int, *, label: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{label}必须是整数")
    if isinstance(value, int):
        result = value
    elif isinstance(value, str):
        text = value.strip()
        if not text or not re.fullmatch(r"[+-]?\d+", text):
            raise ValueError(f"{label}必须是整数")
        try:
            result = int(text, 10)
        except ValueError as exc:  # pragma: no cover - guarded by regex
            raise ValueError(f"{label}必须是整数") from exc
    else:
        raise ValueError(f"{label}必须是整数")
    return result


def validate_timeout(
    value: str | int | float,
    *,
    label: str,
    minimum: float,
    maximum: float,
) -> float:
    """Validate a finite workflow timeout in seconds.

    GitHub ``workflow_dispatch`` inputs arrive as strings.  Rejecting non-finite
    values here prevents ``nan``/``inf`` from reaching browser polling loops,
    where they could otherwise disable a deadline indefinitely.
    """

    if isinstance(value, bool):
        raise ValueError(f"{label}必须是有限数字")
    if isinstance(value, (int, float)):
        candidate = float(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError(f"{label}不能为空")
        try:
            candidate = float(text)
        except ValueError as exc:
            raise ValueError(f"{label}必须是有限数字") from exc
    else:
        raise ValueError(f"{label}必须是有限数字")
    if not math.isfinite(candidate):
        raise ValueError(f"{label}必须是有限数字")
    if candidate < minimum or candidate > maximum:
        raise ValueError(f"{label}必须在 {minimum:g}-{maximum:g} 秒范围内")
    return candidate


def resolve_task_count(path: Path | str, requested: str | int | None) -> int:
    """Resolve the number of matrix jobs from the pool and workflow input."""

    pool_size = len(load_email_pool(path))
    if pool_size == 0:
        raise ValueError("邮箱池为空，无法创建任务")
    if requested is None or (isinstance(requested, str) and not requested.strip()):
        return min(pool_size, MAX_TASKS)

    count = _parse_positive_int(requested, label="任务数量")
    if count < 1 or count > MAX_TASKS:
        raise ValueError(f"任务数量必须在 1-{MAX_TASKS} 范围内")
    if count > pool_size:
        raise ValueError("任务数量不能超过邮箱池数量")
    return count


def resolve_max_parallel(requested: str | int, task_count: int) -> int:
    """Validate and cap matrix parallelism at both 20 and task count."""

    if isinstance(task_count, bool) or not isinstance(task_count, int):
        raise ValueError("任务数量必须是整数")
    if task_count < 1 or task_count > MAX_TASKS:
        raise ValueError(f"任务数量必须在 1-{MAX_TASKS} 范围内")
    value = _parse_positive_int(requested, label="并发数量")
    if value < 1 or value > MAX_PARALLEL:
        raise ValueError(f"并发数量必须在 1-{MAX_PARALLEL} 范围内")
    return min(value, task_count, MAX_PARALLEL)


def select_email(path: Path | str, job_index: int) -> EmailCredential:
    """Select the credential identified by a one-based matrix index."""

    if isinstance(job_index, bool) or not isinstance(job_index, int):
        raise IndexError("无法分配邮箱：任务索引必须是整数")
    if job_index < 1 or job_index > MAX_TASKS:
        raise IndexError(f"无法分配邮箱：任务索引必须在 1-{MAX_TASKS} 范围内")
    pool = load_email_pool(path)
    if job_index > len(pool):
        raise IndexError("无法分配邮箱：任务索引超过当前邮箱池")
    return pool[job_index - 1]


def _requested_email_keys(emails: Iterable[str]) -> tuple[set[str], int]:
    keys: set[str] = set()
    for value in emails:
        if not isinstance(value, str):
            continue
        normalized = value.strip().casefold()
        if normalized:
            keys.add(normalized)
    return keys, len(keys)


def _write_atomic(path: Path, lines: Sequence[str], *, had_bom: bool, final_terminated: bool) -> None:
    """Write normalised lines to a same-directory temporary file and replace."""

    temporary = path.with_name(path.name + ".tmp")
    # A stale temp file can only be from an interrupted prior collect attempt;
    # it is never part of the source pool.
    try:
        if temporary.exists():
            temporary.unlink()
        output = "\n".join(lines)
        if lines and final_terminated:
            output += "\n"
        encoding = "utf-8-sig" if had_bom else "utf-8"
        with temporary.open("w", encoding=encoding, newline="\n") as handle:
            handle.write(output)
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


def remove_consumed(path: Path | str, emails: Iterable[str]) -> RemovalResult:
    """Atomically remove successfully consumed emails from the latest pool.

    Matching is case-insensitive and only the email field is inspected.  Blank
    or malformed records are retained so an unexpected row never causes a
    successful mailbox to be lost.  Untouched content is preserved apart from
    normalising line endings to LF.
    """

    pool_path = _path(path)
    lines, had_bom, final_terminated = _read_pool_lines(pool_path)
    requested_keys, requested_count = _requested_email_keys(emails)
    if not requested_keys:
        # Still perform an atomic normalisation so callers receive the same
        # durability contract as a non-empty request.
        _write_atomic(
            pool_path,
            lines,
            had_bom=had_bom,
            final_terminated=final_terminated,
        )
        remaining = sum(1 for line in lines if line.strip())
        return RemovalResult(0, 0, (), remaining)

    kept: list[str] = []
    removed_emails: list[str] = []
    for line in lines:
        if not line.strip():
            kept.append(line)
            continue
        candidate = line.split(FIELD_SEPARATOR, 1)[0].strip()
        key = candidate.casefold()
        if key in requested_keys and EMAIL_PATTERN.fullmatch(candidate):
            removed_emails.append(candidate)
            continue
        kept.append(line)

    _write_atomic(
        pool_path,
        kept,
        had_bom=had_bom,
        final_terminated=final_terminated,
    )
    remaining = sum(1 for line in kept if line.strip())
    return RemovalResult(
        requested=requested_count,
        removed=len(removed_emails),
        removed_emails=tuple(removed_emails),
        remaining=remaining,
    )


def _prepare_command(arguments: argparse.Namespace) -> int:
    """Generate the JSON matrix outputs consumed by the Actions prepare job."""

    task_count = resolve_task_count(arguments.file, arguments.tasks)
    max_parallel = resolve_max_parallel(arguments.parallel, task_count)
    validate_timeout(
        arguments.page_timeout,
        label="页面超时",
        minimum=PAGE_TIMEOUT_MIN,
        maximum=PAGE_TIMEOUT_MAX,
    )
    validate_timeout(
        arguments.mail_timeout,
        label="邮件超时",
        minimum=MAIL_TIMEOUT_MIN,
        maximum=MAIL_TIMEOUT_MAX,
    )
    outputs = {
        "indices": json.dumps(matrix_indices(task_count), ensure_ascii=False),
        "task_count": str(task_count),
        "max_parallel": str(max_parallel),
    }
    output_path = str(arguments.github_output or "").strip()
    if output_path:
        destination = Path(output_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("a", encoding="utf-8", newline="\n") as handle:
            for key, value in outputs.items():
                handle.write(f"{key}={value}\n")
    else:
        for key, value in outputs.items():
            print(f"{key}={value}")
    return 0


def _build_cli() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Microsoft 邮箱池工具")
    subparsers = parser.add_subparsers(dest="command")
    prepare = subparsers.add_parser("prepare", help="校验邮箱池并生成矩阵输出")
    prepare.add_argument(
        "--file",
        "--邮箱文件",
        dest="file",
        default="verified_email.txt",
        help="邮箱池文件路径",
    )
    prepare.add_argument(
        "--tasks",
        "--任务数量",
        dest="tasks",
        default="",
        help="任务数量；留空自动计算",
    )
    prepare.add_argument(
        "--parallel",
        "--最大并发",
        dest="parallel",
        default="20",
        help="最大并发数量（1-20）",
    )
    prepare.add_argument(
        "--github-output",
        dest="github_output",
        default="",
        help="GitHub Actions GITHUB_OUTPUT 文件",
    )
    prepare.add_argument(
        "--page-timeout",
        dest="page_timeout",
        default="90",
        help="页面超时秒数（30-300）",
    )
    prepare.add_argument(
        "--mail-timeout",
        dest="mail_timeout",
        default="240",
        help="邮件取件超时秒数（30-600）",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_cli()
    arguments = parser.parse_args(argv)
    if arguments.command != "prepare":
        parser.error("请指定命令 prepare")
    try:
        return _prepare_command(arguments)
    except (FileNotFoundError, IndexError, ValueError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2


# Names used by the earlier standalone V6 plan.  Keeping these aliases avoids
# coupling the collect job to a particular naming revision.
select_email_credential = select_email
remove_consumed_emails = remove_consumed


__all__ = [
    "EmailCredential",
    "RemovalResult",
    "FIELD_SEPARATOR",
    "MAX_TASKS",
    "MAX_PARALLEL",
    "MAIL_TIMEOUT_MAX",
    "MAIL_TIMEOUT_MIN",
    "PAGE_TIMEOUT_MAX",
    "PAGE_TIMEOUT_MIN",
    "load_email_pool",
    "matrix_indices",
    "parse_credential_line",
    "remove_consumed",
    "remove_consumed_emails",
    "resolve_max_parallel",
    "resolve_task_count",
    "select_email",
    "select_email_credential",
    "validate_timeout",
    "main",
]


def prepare_matrix(
    path: Path | str,
    requested_tasks: str | int | None,
    requested_parallel: str | int,
) -> tuple[list[int], int, int]:
    """Validate workflow inputs and return matrix data for GitHub Actions."""

    task_count = resolve_task_count(path, requested_tasks)
    max_parallel = resolve_max_parallel(requested_parallel, task_count)
    return matrix_indices(task_count), task_count, max_parallel


def _prepare_cli(argv: list[str] | None = None) -> int:
    import argparse
    import json
    import os

    parser = argparse.ArgumentParser(description="校验 verified_email.txt 并生成 Actions 矩阵")
    parser.add_argument("prepare", nargs="?")
    parser.add_argument("--file", default="verified_email.txt")
    parser.add_argument("--tasks", default="")
    parser.add_argument("--parallel", default="20")
    parser.add_argument("--github-output", default=os.environ.get("GITHUB_OUTPUT", ""))
    args = parser.parse_args(argv)
    try:
        indices, task_count, max_parallel = prepare_matrix(
            args.file,
            args.tasks,
            args.parallel,
        )
    except Exception as exc:
        print(f"任务准备失败: {type(exc).__name__}: {exc}")
        return 2
    values = {
        "indices": json.dumps(indices, ensure_ascii=False),
        "task_count": str(task_count),
        "max_parallel": str(max_parallel),
    }
    if args.github_output:
        with Path(args.github_output).open("a", encoding="utf-8", newline="\n") as handle:
            for key, value in values.items():
                handle.write(f"{key}={value}\n")
    else:
        for key, value in values.items():
            print(f"{key}={value}")
    return 0


# Names used by earlier V6 drafts.  Keeping these aliases avoids coupling the
# collect job to a particular naming revision.
select_email_credential = select_email
remove_consumed_emails = remove_consumed


__all__ = [
    "EmailCredential",
    "RemovalResult",
    "FIELD_SEPARATOR",
    "MAX_TASKS",
    "MAX_PARALLEL",
    "load_email_pool",
    "matrix_indices",
    "parse_credential_line",
    "prepare_matrix",
    "remove_consumed",
    "remove_consumed_emails",
    "resolve_max_parallel",
    "resolve_task_count",
    "select_email",
    "select_email_credential",
    "main",
]


if __name__ == "__main__":
    raise SystemExit(main())
