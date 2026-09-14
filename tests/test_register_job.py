from __future__ import annotations

import json
from pathlib import Path

import pytest

import register_job


@pytest.fixture
def account() -> object:
    """Credential-shaped object without using a real credential."""
    return register_job.JobAccount(
        email="person@example.test",
        password="secret-placeholder",
        client_id="client-placeholder",
        token="refresh-placeholder",
    )


def test_job_uses_one_account_and_three_distinct_profiles(tmp_path: Path, account: object) -> None:
    profiles: list[Path] = []
    seen_accounts: list[object] = []
    outcomes = iter((RuntimeError("first"), RuntimeError("second"), {"aliases": [
        "person01@example.test",
        "person02@example.test",
    ], "auxiliary_email": "helper@example.test"}))

    def attempt_runner(*, account: object, profile_dir: Path, snapshot_dir: Path, **_: object) -> object:
        seen_accounts.append(account)
        profiles.append(profile_dir)
        outcome = next(outcomes)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    result = register_job.run_job(account, output_dir=tmp_path, attempt_runner=attempt_runner)

    assert result["success"] is True
    assert result["job_index"] == 1
    assert result["assigned_email"] == "person@example.test"
    assert result["aliases"] == ["person01@example.test", "person02@example.test"]
    assert result["verification"] == {"alias0": True, "alias1": True}
    assert len(profiles) == 3
    assert len(set(profiles)) == 3
    assert all(path.parent == tmp_path / "profiles" for path in profiles)
    assert all(value is account for value in seen_accounts)
    assert (tmp_path / "result.json").is_file()
    assert (tmp_path / "账号.txt").is_file()


def test_failed_job_writes_sanitized_result_and_cleans_profiles(
    tmp_path: Path, account: object
) -> None:
    secret_text = "secret-placeholder client-placeholder refresh-placeholder"
    closed: list[object] = []

    def attempt_runner(*, page: object, **_: object) -> object:
        raise RuntimeError(f"operation failed: {secret_text}")

    result = register_job.run_job(
        account,
        output_dir=tmp_path,
        max_attempts=3,
        attempt_runner=attempt_runner,
        browser_closer=closed.append,
    )

    artifact = tmp_path / "result.json"
    payload = json.loads(artifact.read_text(encoding="utf-8"))
    serialized = json.dumps(payload, ensure_ascii=False)
    assert result["success"] is False
    assert result["verification"] == {"alias0": False, "alias1": False}
    assert result["error"]
    assert secret_text not in serialized
    assert account.password not in serialized
    assert account.client_id not in serialized
    assert account.token not in serialized
    assert not list((tmp_path / "profiles").glob("attempt-*"))
    assert not list((tmp_path / "失败截图").glob("attempt-*"))


def test_result_schema_is_collect_compatible(tmp_path: Path, account: object) -> None:
    def attempt_runner(**_: object) -> dict[str, object]:
        return {
            "aliases": ("person01@example.test", "person02@example.test"),
            "auxiliary_email": "helper@example.test",
            "verification": {"alias0": True, "alias1": True},
        }

    result = register_job.run_job(
        account,
        output_dir=tmp_path,
        job_index=7,
        attempt_runner=attempt_runner,
    )
    assert set(result) == {
        "schema_version",
        "success",
        "job_index",
        "assigned_email",
        "aliases",
        "auxiliary_email",
        "verification",
        "error",
    }
    assert result["schema_version"] == 1
    assert result["job_index"] == 7


def test_run_job_rejects_invalid_attempt_limit(tmp_path: Path, account: object) -> None:
    with pytest.raises(ValueError, match="尝试"):
        register_job.run_job(account, output_dir=tmp_path, max_attempts=4)


def test_cli_reads_auxiliary_credentials_from_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pool = tmp_path / "verified_email.txt"
    pool.write_text(
        "person@example.test----p----c----t\n",
        encoding="utf-8",
    )
    captured: dict[str, object] = {}

    def fake_run_job(account: object, output_dir: Path, **kwargs: object) -> dict[str, object]:
        captured["account"] = account
        captured.update(kwargs)
        return {
            "schema_version": 1,
            "success": True,
            "job_index": 1,
            "assigned_email": "person@example.test",
            "aliases": ["person01@example.test", "person02@example.test"],
            "auxiliary_email": "helper@example.test",
            "verification": {"alias0": True, "alias1": True},
            "error": None,
        }

    monkeypatch.setenv(
        "AUXILIARY_CREDENTIALS",
        "helper@example.test----hp----hc----ht",
    )
    monkeypatch.setattr(register_job, "run_job", fake_run_job)
    code = register_job.main(
        [
            "--邮箱文件",
            str(pool),
            "--任务索引",
            "1",
            "--输出目录",
            str(tmp_path / "out"),
        ]
    )
    assert code == 0
    auxiliary = captured["auxiliary_credentials"]
    assert getattr(auxiliary, "email") == "helper@example.test"


def test_run_job_seeds_each_attempt_from_static_cache(tmp_path: Path, account: object) -> None:
    seed = tmp_path / "seed" / "cache2"
    seed.mkdir(parents=True)
    (seed / "entry").write_text("static", encoding="utf-8")
    seen: list[bool] = []

    def attempt_runner(*, profile_dir: Path, **_: object) -> object:
        seen.append((profile_dir / "cache2" / "entry").read_text(encoding="utf-8") == "static")
        return {
            "aliases": ["person01@example.test", "person02@example.test"],
            "auxiliary_email": "helper@example.test",
        }

    result = register_job.run_job(
        account,
        output_dir=tmp_path / "out",
        attempt_runner=attempt_runner,
        static_cache_dir=tmp_path / "seed",
    )
    assert result["success"] is True
    assert seen == [True]


def test_run_job_expands_alias_local_parts_to_main_domain(tmp_path: Path, account: object) -> None:
    result = register_job.run_job(
        account,
        output_dir=tmp_path,
        attempt_runner=lambda **_: {
            "aliases": ["person01", "person02"],
            "auxiliary_email": "helper@example.test",
        },
    )
    assert result["success"] is True
    assert result["aliases"] == ["person01@example.test", "person02@example.test"]


def test_run_single_attempt_passes_account_once_to_performer(tmp_path: Path, account: object) -> None:
    seen: list[object] = []

    def launcher(**_: object) -> object:
        return object()

    def closer(*_: object, **__: object) -> None:
        return None

    def performer(page: object, account: object, **_: object) -> dict[str, object]:
        seen.append(account)
        return {
            "aliases": ["person01@example.test", "person02@example.test"],
            "auxiliary_email": "helper@example.test",
        }

    outcome = register_job.run_single_attempt(
        account=account,
        profile_dir=tmp_path / "profile",
        snapshot_dir=tmp_path / "snapshots",
        browser_launcher=launcher,
        browser_closer=closer,
        performer=performer,
    )

    assert outcome["aliases"] == ["person01@example.test", "person02@example.test"]
    assert seen == [account]


def test_default_performer_can_capture_random_auxiliary_email(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, account: object
) -> None:
    import open_microsoft_email

    helper_path = tmp_path / "helper.txt"
    helper_path.write_text("helper@example.test----hp----hc----ht\n", encoding="utf-8")

    def fake_login(page: object, **kwargs: object) -> list[str]:
        loader = kwargs["auxiliary_loader"]
        loader(kwargs["auxiliary_credentials_file"])
        return ["person01@example.test", "person02@example.test"]

    monkeypatch.setattr(
        open_microsoft_email,
        "login_and_add_aliases",
        fake_login,
    )
    monkeypatch.setattr(
        open_microsoft_email.core,
        "navigate_with_retry",
        lambda *args, **kwargs: {"href": open_microsoft_email.DEFAULT_URL},
    )

    result = register_job._default_performer(
        object(),
        register_job.coerce_account(account),
        profile_dir=tmp_path / "profile",
        snapshot_dir=tmp_path / "snapshots",
        form_timeout=30,
        mail_timeout=30,
        success_timeout=30,
        headless=True,
        auxiliary_credentials_file=helper_path,
    )
    assert result["auxiliary_email"] == "helper@example.test"


@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf"), "not-a-number"])
def test_run_job_rejects_non_finite_or_non_positive_timeouts(
    tmp_path: Path, account: object, value: object
) -> None:
    with pytest.raises(ValueError, match="超时"):
        register_job.run_job(account, output_dir=tmp_path, form_timeout=value)  # type: ignore[arg-type]
