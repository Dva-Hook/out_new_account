# Microsoft 邮箱别名 Actions V6

项目通过 GitHub Actions 调用 RuyiPage，为 Microsoft 主邮箱添加 `01`、`02` 两个别名。单次最多处理 256 个主邮箱，最大并发 20，每个矩阵 job 只处理一个账号。

## 仓库文件

在根目录 `verified_email.txt` 放置主邮箱，每行格式：

```text
邮箱----密码----client_id----令牌
```

可选的根目录 `辅助邮箱.txt` 使用相同格式。不要把真实凭据提交到公开仓库。

## 运行工作流

进入 GitHub 仓库的 **Actions**，选择 **Microsoft 邮箱别名 V6**，点击 **Run workflow**：

- `任务数量`：留空时自动使用最多 256 条可用主邮箱。
- `最大并发`：范围 1-20，默认 20。
- `绑定辅助邮箱`：可输入一整行辅助邮箱凭据。非空时优先使用；留空时优先读取仓库 Secret `AUXILIARY_CREDENTIALS`，再从 `辅助邮箱.txt` 随机读取。
- `页面超时秒数`、`邮件超时秒数`：按页面与取件速度调整。

工作流输入会出现在 GitHub 运行元数据中。长期运行建议把四字段辅助凭据放入仓库 Secret `AUXILIARY_CREDENTIALS`，并将输入框留空。项目会立即对凭据调用 Actions mask，并且不会在日志或 Summary 打印完整凭据；仍建议使用私有仓库。

## 工作流结构

1. `prepare` 校验输入，创建 1-based 矩阵索引。
2. `register` 为每个账号启动一个独立 runner，内部最多尝试三次；Graph 取件失败时回退 O2。
3. `collect` 始终执行，只接受两条别名都确认成功的 Artifact，生成 `成功账号.txt` 并从 `verified_email.txt` 删除成功主邮箱。

`成功账号.txt` 只上传到名为 `ALL-所有成功邮件` 的 Artifact，不提交到仓库；仓库只回写已成功处理的主邮箱池删除结果。Artifact 中的四行使用完整凭据格式：

成功文件每个账号固定四行：

```text
辅邮：helper@example.com----helper_password----helper_client_id----helper_token
主邮：main@example.com----main_password----main_client_id----main_token
子邮1：main01@example.com----main_password----main_client_id----main_token
子邮2：main02@example.com----main_password----main_client_id----main_token
```

完整凭据只在 `collect` job 的工作区生成并上传 Artifact，不写入仓库；矩阵任务的 `result.json`、Actions 日志和 Summary 仍只保存状态与邮箱地址。

每个矩阵任务另外上传 `microsoft-email-diagnostic-<index>` Artifact。该 Artifact 只包含 credentialaction 页面阶段的诊断截图；运行日志会记录阶段名、页面类型、readyState 和关键元素存在状态，不记录完整 URL、密码或令牌。

失败、超时、Artifact 损坏或只完成一个别名的主邮箱不会被删除。

## 本地验证

```powershell
python -m pip install -r requirements.txt
python -m pytest -q
python -m py_compile *.py tests/*.py
```
