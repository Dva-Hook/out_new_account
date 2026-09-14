from __future__ import annotations

from pathlib import Path

import pytest

from email_pool import (
    EmailCredential,
    matrix_indices,
    load_email_pool,
    parse_credential_line,
    remove_consumed,
    remove_consumed_emails,
    resolve_max_parallel,
    resolve_task_count,
    select_email,
    select_email_credential,
)


def _line(local: str, password: str = "password", client: str = "client", token: str = "token") -> str:
    return f"{local}@example.com----{password}----{client}----{token}"


def test_parse_four_fields_strips_outer_whitespace_and_redacts_repr() -> None:
    credential = parse_credential_line(
        "  Person@example.com ---- pass-1 ---- client-1 ---- refresh-1  ",
        source_index=7,
    )

    assert credential == EmailCredential(
        email="Person@example.com",
        password="pass-1",
        client_id="client-1",
        token="refresh-1",
        raw_line="  Person@example.com ---- pass-1 ---- client-1 ---- refresh-1  ",
        source_index=7,
    )
    assert credential.mailbox_password == "pass-1"
    assert credential.refresh_token == "refresh-1"
    assert credential.normalized_line == _line("Person", "pass-1", "client-1", "refresh-1")
    rendered = repr(credential)
    assert "pass-1" not in rendered
    assert "client-1" not in rendered
    assert "refresh-1" not in rendered
    assert "Person@example.com" in rendered


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "person@example.com----password----client",
        "person@example.com--------client----token",
        "person@example.com----password--------token",
        "not-an-email----password----client----token",
        "person@example----password----client----token",
    ],
)
def test_parse_rejects_malformed_or_empty_fields(raw: str) -> None:
    with pytest.raises(ValueError, match="凭据格式|邮箱格式"):
        parse_credential_line(raw, source_index=1)


def test_parse_preserves_separator_inside_token() -> None:
    credential = parse_credential_line(
        "person@example.com----password----client----token----with-separator",
        source_index=1,
    )
    assert credential.token == "token----with-separator"


def test_load_ignores_blank_lines_and_utf8_bom(tmp_path: Path) -> None:
    path = tmp_path / "verified_email.txt"
    path.write_text("\ufeff" + _line("first") + "\n\n  \n" + _line("second") + "\n", encoding="utf-8")

    pool = load_email_pool(path)

    assert [item.email for item in pool] == ["first@example.com", "second@example.com"]
    assert [item.source_index for item in pool] == [1, 4]


def test_load_rejects_case_insensitive_duplicate_email(tmp_path: Path) -> None:
    path = tmp_path / "verified_email.txt"
    path.write_text(_line("Person") + "\n" + _line("person") + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="重复"):
        load_email_pool(path)


def test_load_rejects_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="邮箱文件不存在"):
        load_email_pool(tmp_path / "missing.txt")


def test_matrix_indices_are_unique_and_one_based() -> None:
    assert matrix_indices(0) == []
    assert matrix_indices(3) == [1, 2, 3]
    with pytest.raises(ValueError, match="任务数量"):
        matrix_indices(-1)


def test_matrix_indices_and_limits_never_exceed_v6_caps() -> None:
    assert len(matrix_indices(256)) == 256
    with pytest.raises(ValueError, match="256"):
        matrix_indices(257)
    with pytest.raises(ValueError, match="任务数量"):
        matrix_indices("2")  # type: ignore[arg-type]


def test_select_email_uses_one_based_matrix_index(tmp_path: Path) -> None:
    path = tmp_path / "verified_email.txt"
    path.write_text(_line("first") + "\n" + _line("second") + "\n", encoding="utf-8")

    assert select_email(path, 1).email == "first@example.com"
    assert select_email(path, 2).email == "second@example.com"
    assert select_email_credential(path, 1).email == "first@example.com"
    with pytest.raises(IndexError, match="无法分配"):
        select_email(path, 3)
    with pytest.raises(IndexError, match="无法分配"):
        select_email(path, 0)


def test_resolve_task_count_defaults_to_pool_size_capped_at_256(tmp_path: Path) -> None:
    path = tmp_path / "verified_email.txt"
    path.write_text("\n".join(_line(f"user{i}") for i in range(260)) + "\n", encoding="utf-8")

    assert resolve_task_count(path, None) == 256
    assert resolve_task_count(path, "") == 256
    assert resolve_task_count(path, "  ") == 256


def test_resolve_task_count_validates_requested_range_and_capacity(tmp_path: Path) -> None:
    path = tmp_path / "verified_email.txt"
    path.write_text(_line("first") + "\n" + _line("second") + "\n", encoding="utf-8")

    assert resolve_task_count(path, 1) == 1
    assert resolve_task_count(path, "2") == 2
    for requested in (0, -1, 257, "abc"):
        with pytest.raises(ValueError, match="任务数量"):
            resolve_task_count(path, requested)
    with pytest.raises(ValueError, match="邮箱池"):
        resolve_task_count(path, 3)


def test_resolve_max_parallel_caps_at_twenty_and_task_count() -> None:
    assert resolve_max_parallel(20, 256) == 20
    assert resolve_max_parallel(20, 3) == 3
    assert resolve_max_parallel("5", 3) == 3
    for requested in (0, -1, 21, "abc"):
        with pytest.raises(ValueError, match="并发"):
            resolve_max_parallel(requested, 10)
    with pytest.raises(ValueError, match="任务数量"):
        resolve_max_parallel(1, 0)


def test_remove_consumed_is_case_insensitive_atomic_and_preserves_other_lines(tmp_path: Path) -> None:
    path = tmp_path / "verified_email.txt"
    original = "  First@example.com----p1----c1----t1\r\n" + _line("Second", "p2", "c2", "t2") + "\n"
    path.write_bytes(original.encode("utf-8"))

    result = remove_consumed(path, {"FIRST@EXAMPLE.COM"})

    assert result.requested == 1
    assert result.removed == 1
    assert result.removed_emails == ("First@example.com",)
    assert result.remaining == 1
    assert path.read_text(encoding="utf-8") == _line("Second", "p2", "c2", "t2") + "\n"
    assert not Path(f"{path}.tmp").exists()
    assert not path.with_name(path.name + ".tmp").exists()


def test_remove_consumed_keeps_pool_when_no_email_matches(tmp_path: Path) -> None:
    path = tmp_path / "verified_email.txt"
    original = _line("first") + "\n" + _line("second") + "\n"
    path.write_text(original, encoding="utf-8")

    result = remove_consumed_emails(path, ["missing@example.com"])

    assert result.requested == 1
    assert result.removed == 0
    assert result.removed_emails == ()
    assert result.remaining == 2
    assert path.read_text(encoding="utf-8") == original


def test_remove_consumed_rejects_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="邮箱文件不存在"):
        remove_consumed(tmp_path / "missing.txt", ["x@example.com"])
