from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "microsoft-email-v6.yml"


def load_workflow() -> dict:
    # BaseLoader keeps the YAML 1.1 parser from coercing the key `on` to True.
    return yaml.load(WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


def test_workflow_has_required_matrix_limits_and_jobs() -> None:
    data = load_workflow()
    assert "workflow_dispatch" in data["on"]
    assert set(data["jobs"]) == {"prepare", "register", "collect"}
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "fail-fast: false" in text
    assert "max-parallel: ${{ fromJSON(needs.prepare.outputs.max_parallel) }}" in text
    assert "if: always()" in text
    assert "contents: write" in text
    assert "xvfb-run -a python register_job.py" in text


def test_workflow_exposes_auxiliary_input_and_one_index_per_job() -> None:
    data = load_workflow()
    inputs = data["on"]["workflow_dispatch"]["inputs"]
    assert "auxiliary_credentials" in inputs
    assert "绑定辅助邮箱" in inputs["auxiliary_credentials"]["description"]
    matrix = data["jobs"]["register"]["strategy"]["matrix"]
    assert matrix["index"] == "${{ fromJSON(needs.prepare.outputs.indices) }}"
    assert "for attempt in 1 2 3" not in WORKFLOW.read_text(encoding="utf-8")


def test_only_collect_can_write_repository() -> None:
    data = load_workflow()
    assert data["permissions"]["contents"] == "read"
    assert data["jobs"]["collect"]["permissions"]["contents"] == "write"
    assert "permissions" not in data["jobs"]["register"]


def test_workflow_does_not_interpolate_credentials_into_commands() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "${{ inputs.auxiliary_credentials }}" not in "\n".join(
        line for line in text.splitlines() if line.lstrip().startswith("run:")
    )
    assert "cat verified_email.txt" not in text
    assert "::add-mask::" in text


def test_success_file_is_artifact_only() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "git add -- verified_email.txt 成功账号.txt" not in text
    assert "git add -- verified_email.txt" in text
    assert "path: 成功账号.txt" in text
    assert "name: ALL-所有成功邮件" in text
    assert "name: microsoft-email-success-${{ github.run_id }}" not in text
    assert "--完整凭据输出" in text
    assert "AUXILIARY_CREDENTIALS: ${{ inputs.auxiliary_credentials || secrets.AUXILIARY_CREDENTIALS }}" in text
