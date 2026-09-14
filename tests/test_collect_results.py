from __future__ import annotations

import json
from pathlib import Path

import pytest

import collect_results as module


def _write_result(
    path: Path,
    *,
    email: str = "Person@example.test",
    auxiliary_email: str = "helper@example.test",
    success: bool = True,
    aliases: list[str] | None = None,
    verification: dict[str, object] | None = None,
    job_index: int = 1,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, object] = {
        "schema_version": 1,
        "success": success,
        "job_index": job_index,
        "assigned_email": email,
        "aliases": aliases
        if aliases is not None
        else ["Person01@example.test", "Person02@example.test"],
        "auxiliary_email": auxiliary_email,
        "verification": verification
        if verification is not None
        else {"alias0": True, "alias1": True},
        # A result writer may include these fields; the collector must never
        # copy them into the safe output file.
        "main_credential_line": "Person@example.test----secret----client----token",
    }
    if not success:
        payload["error"] = "placeholder failure"
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_collect_accepts_only_fully_verified_matching_aliases(tmp_path: Path) -> None:
    _write_result(tmp_path / "job-1" / "result.json")
    _write_result(
        tmp_path / "job-2" / "result.json",
        email="Other@example.test",
        aliases=["wrong01@example.test", "wrong02@example.test"],
        job_index=2,
    )
    _write_result(
        tmp_path / "job-3" / "result.json",
        email="Third@example.test",
        verification={"alias0": True, "alias1": False},
        job_index=3,
    )
    _write_result(
        tmp_path / "job-4" / "result.json",
        email="Failed@example.test",
        success=False,
        job_index=4,
    )
    (tmp_path / "job-5" / "result.json").parent.mkdir()
    (tmp_path / "job-5" / "result.json").write_text("{broken", encoding="utf-8")

    summary = module.collect_results(tmp_path)

    assert [item.main_email for item in summary.accepted] == ["Person@example.test"]
    assert summary.total_artifacts == 5
    assert summary.failed == 1
    assert summary.ignored == 3


def test_collect_deduplicates_email_case_insensitively(tmp_path: Path) -> None:
    _write_result(tmp_path / "job-1" / "result.json")
    _write_result(
        tmp_path / "job-2" / "result.json",
        email="person@EXAMPLE.TEST",
        aliases=["person01@EXAMPLE.TEST", "person02@EXAMPLE.TEST"],
        job_index=2,
    )

    summary = module.collect_results(tmp_path)

    assert len(summary.accepted) == 1
    assert summary.ignored == 1


def test_apply_writes_four_line_output_and_is_idempotent(tmp_path: Path) -> None:
    results = tmp_path / "artifacts"
    pool = tmp_path / "verified_email.txt"
    output = tmp_path / "成功账号.txt"
    pool.write_text(
        "Person@example.test----p----c----t\n"
        "Failed@example.test----p2----c2----t2\n",
        encoding="utf-8",
    )
    _write_result(results / "job-1" / "result.json")
    _write_result(
        results / "job-2" / "result.json",
        email="Failed@example.test",
        success=False,
        job_index=2,
    )

    first = module.apply_results(results, pool, output, expected_tasks=2)
    second = module.apply_results(results, pool, output, expected_tasks=2)

    assert first.appended == 1
    assert first.removed == 1
    assert second.appended == 0
    assert second.removed == 0
    assert output.read_text(encoding="utf-8") == (
        "辅邮：helper@example.test\n"
        "主邮：Person@example.test\n"
        "子邮1：Person01@example.test\n"
        "子邮2：Person02@example.test\n\n"
    )
    assert "Person@example.test" not in pool.read_text(encoding="utf-8")
    assert "Failed@example.test" in pool.read_text(encoding="utf-8")


def test_existing_output_dedupes_case_insensitively_and_retries_pool_removal(
    tmp_path: Path,
) -> None:
    results = tmp_path / "artifacts"
    pool = tmp_path / "verified_email.txt"
    output = tmp_path / "成功账号.txt"
    pool.write_text("Person@example.test----p----c----t\n", encoding="utf-8")
    output.write_text(
        "辅邮：old@example.test\n"
        "主邮：person@EXAMPLE.TEST\n"
        "子邮1：person01@example.test\n"
        "子邮2：person02@example.test\n\n",
        encoding="utf-8",
    )
    _write_result(results / "job-1" / "result.json")

    result = module.apply_results(results, pool, output)

    assert result.appended == 0
    assert result.removed == 1
    assert output.read_text(encoding="utf-8").count("主邮：") == 1
    assert pool.read_text(encoding="utf-8") == ""


def test_invalid_schema_and_secret_fields_are_not_echoed(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write_result(tmp_path / "job-1" / "result.json")
    payload = json.loads((tmp_path / "job-1" / "result.json").read_text())
    payload["schema_version"] = 2
    payload["main_credential_line"] = "secret-placeholder"
    (tmp_path / "job-1" / "result.json").write_text(json.dumps(payload), encoding="utf-8")

    summary = module.collect_results(tmp_path)

    assert not summary.accepted
    assert summary.ignored == 1
    assert "secret-placeholder" not in capsys.readouterr().out


def test_expected_task_count_cannot_be_less_than_artifacts(tmp_path: Path) -> None:
    _write_result(tmp_path / "job-1" / "result.json")

    with pytest.raises(ValueError, match="预期任务数量"):
        module.collect_results(tmp_path, expected_tasks=0)


def test_success_without_credentialaction_allows_empty_auxiliary_email(tmp_path: Path) -> None:
    _write_result(tmp_path / "job-1" / "result.json", auxiliary_email="")
    summary = module.collect_results(tmp_path)
    assert summary.successful == 1
    assert module.format_success_record(summary.accepted[0]).startswith("辅邮：\n")


def test_apply_full_credentials_uses_auxiliary_and_main_pool_lines(tmp_path: Path) -> None:
    results = tmp_path / "artifacts"
    pool = tmp_path / "verified_email.txt"
    auxiliary_pool = tmp_path / "辅助邮箱.txt"
    output = tmp_path / "成功账号.txt"
    pool.write_text(
        "Person@example.test----main-pass----main-client----main-token\n",
        encoding="utf-8",
    )
    auxiliary_pool.write_text(
        "helper@example.test----helper-pass----helper-client----helper-token\n",
        encoding="utf-8",
    )
    _write_result(results / "job-1" / "result.json")

    summary = module.apply_results(
        results,
        pool,
        output,
        expected_tasks=1,
        include_credentials=True,
        auxiliary_pool_path=auxiliary_pool,
    )
    rerun = module.apply_results(
        results,
        pool,
        output,
        expected_tasks=1,
        include_credentials=True,
        auxiliary_pool_path=auxiliary_pool,
    )

    assert summary.appended == 1
    assert rerun.appended == 0
    assert rerun.removed == 0
    assert output.read_text(encoding="utf-8") == (
        "辅邮：helper@example.test----helper-pass----helper-client----helper-token\n"
        "主邮：Person@example.test----main-pass----main-client----main-token\n"
        "子邮1：Person01@example.test----main-pass----main-client----main-token\n"
        "子邮2：Person02@example.test----main-pass----main-client----main-token\n\n"
    )


def test_apply_full_credentials_accepts_workflow_auxiliary_override(tmp_path: Path) -> None:
    results = tmp_path / "artifacts"
    pool = tmp_path / "verified_email.txt"
    output = tmp_path / "成功账号.txt"
    pool.write_text(
        "Person@example.test----main-pass----main-client----main-token\n",
        encoding="utf-8",
    )
    _write_result(results / "job-1" / "result.json")

    module.apply_results(
        results,
        pool,
        output,
        expected_tasks=1,
        include_credentials=True,
        auxiliary_credentials_raw=(
            "helper@example.test----override-pass----override-client----override-token"
        ),
    )

    assert output.read_text(encoding="utf-8").startswith(
        "辅邮：helper@example.test----override-pass----override-client----override-token\n"
    )


def test_workflow_auxiliary_override_rejects_result_for_another_helper(tmp_path: Path) -> None:
    results = tmp_path / "artifacts"
    pool = tmp_path / "verified_email.txt"
    auxiliary_pool = tmp_path / "辅助邮箱.txt"
    output = tmp_path / "成功账号.txt"
    pool.write_text(
        "Person@example.test----main-pass----main-client----main-token\n",
        encoding="utf-8",
    )
    auxiliary_pool.write_text(
        "result-helper@example.test----file-pass----file-client----file-token\n",
        encoding="utf-8",
    )
    _write_result(
        results / "job-1" / "result.json",
        auxiliary_email="result-helper@example.test",
    )

    with pytest.raises(ValueError, match="辅助邮箱凭据与结果邮箱不匹配"):
        module.apply_results(
            results,
            pool,
            output,
            expected_tasks=1,
            include_credentials=True,
            auxiliary_pool_path=auxiliary_pool,
            auxiliary_credentials_raw=(
                "workflow-helper@example.test----override-pass----override-client----override-token"
            ),
        )
