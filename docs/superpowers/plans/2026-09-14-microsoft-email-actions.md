# Microsoft 邮箱 Actions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans. Steps use checkbox syntax.

**Goal:** 构建可在 GitHub Actions 中以 256 任务、20 并发运行的 Microsoft 邮箱别名项目。

**Architecture:** `prepare -> register(matrix) -> collect(always)`；单账号运行器负责三次独立浏览器尝试，汇总 job 负责严格校验、幂等追加 `成功账号.txt` 和原子消费 `verified_email.txt`。

**Tech Stack:** Python 3.12、RuyiPage Firefox、requests、pytest、PyYAML、GitHub Actions。

**Spec:** `docs/superpowers/specs/2026-09-14-microsoft-email-actions-design.md`

## Global Constraints

- `verified_email.txt` 每行四字段：`邮箱----密码----client_id----令牌`。
- 单次任务数范围 `1..256`，矩阵 job 最大并发 `1..20`。
- 每个 job 只处理一个主邮箱，内部最多三次独立 profile 尝试。
- 只有 `idAliasEmail0` 和 `idAliasEmail1` 均确认时才删除主邮箱。
- 成功输出只写 `辅邮/主邮/子邮1/子邮2` 四行，不把凭据写入日志或 Summary。

### Task 1: 邮箱池与凭据解析

**Files:** `email_pool.py`, `tests/test_email_pool.py`

- [ ] 写失败测试：四字段、重复、索引、256/20、原子删除。
- [ ] 实现 `EmailCredential`、`load_email_pool`、`select_email`、`resolve_task_count`、`resolve_max_parallel`、`remove_consumed`。
- [ ] 运行 `python -m pytest tests/test_email_pool.py -q`。

### Task 2: 自包含 Microsoft 流程与取件

**Files:** `open_microsoft_email.py`, `ruyipage_core.py`, `outlook_battlenet_ticket_http.py`, `tests/test_open_microsoft_email.py`

- [ ] 将核心导入改为项目内模块，支持单账号调用和辅助凭据覆盖。
- [ ] 保留 credentialaction、Graph/O2、OTP 间隔和按钮监控逻辑；CI 模式跳过回车等待。
- [ ] 运行流程离线回归测试与 `python -m py_compile`。

### Task 3: 单矩阵任务运行器

**Files:** `register_job.py`, `tests/test_register_job.py`

- [ ] 写失败测试：一 job 一账号、三次互异 profile、失败脱敏 Artifact。
- [ ] 实现 CLI、结果 schema、浏览器清理、始终生成 `result.json`。
- [ ] 运行任务测试。

### Task 4: 汇总与原子回写

**Files:** `collect_results.py`, `tests/test_collect_results.py`

- [ ] 写失败测试：成功条件、坏 Artifact、大小写去重、重复运行和失败邮箱保留。
- [ ] 实现四行输出、原子池更新和 Summary 摘要（仅计数）。
- [ ] 运行汇总测试。

### Task 5: GitHub Actions 与文档

**Files:** `.github/workflows/microsoft-email-v6.yml`, `requirements.txt`, `README.md`, `.gitignore`, `verified_email.txt`, `辅助邮箱.txt`, `tests/test_workflow_contract.py`

- [ ] 写 YAML 契约测试。
- [ ] 实现中文三 job 工作流、缓存、Artifact、权限和推送冲突重试。
- [ ] 运行全量 pytest、编译检查、YAML 解析和凭据扫描。
