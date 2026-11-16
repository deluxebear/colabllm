# Cloudflare R2 → Google Drive 断点续传迁移（`r2-migrate`）

默认 **Colab CPU** 运行时（可选 `--high-mem` 高内存 CPU），自动挂载 Google Drive，提供内置 Web UI 与 CLI（Caddy Basic Auth + Bearer Token）。
专为**跨多桶、上百万级小文件**场景设计：保留完整目录结构、断点续传（`StartAfter` 游标 + NVMe SQLite WAL 定时备份至 Drive）、验证后可选删除 R2 原文件，并最大化利用 Cloudflare R2 免费调用额度。

共享的安装前置、域名、认证、超时和停止行为见 [运行时通用操作](runtime-common.md)。以下命令均在本机执行。

## 启动与凭据配置

```bash
colabllm r2-migrate up
colabllm r2-migrate up --high-mem --workers 48 --writer-workers 16 --spool-gb 6
colabllm r2-migrate auth --account-id YOUR_ACCOUNT_ID --access-key-id YOUR_KEY_ID --secret-access-key YOUR_SECRET
colabllm r2-migrate auth --memory-only --account-id YOUR_ACCOUNT_ID --access-key-id YOUR_KEY_ID --secret-access-key YOUR_SECRET
colabllm r2-migrate buckets
```

- **凭据安全**：默认保存在本机 `~/.colabllm/r2-credentials.json`（权限 `0600`）与 Drive 运行目录；加 `--memory-only` 则远端仅驻留内存，不写入 Drive（VM 重建后需重新下发）。Web UI 与 `/api/credentials` 永不回显 `secret_access_key`。
- 欧盟或 FedRAMP 合规桶可加 `--jurisdiction eu` 或 `--jurisdiction fedramp`，或通过 `--endpoint-url` 指定自定义 S3 端点。
- 清除已保存凭据：`colabllm r2-migrate auth --clear`。

## 迁移全桶与多桶目录结构

```bash
colabllm r2-migrate sync --output-dir r2-backup
colabllm r2-migrate sync --buckets assets,backups --prefix 2026/ --output-dir r2-backup
colabllm r2-migrate sync --buckets assets --flatten-single-bucket --output-dir r2-assets
colabllm r2-migrate ls-drive r2-backup
```

- `sync` 别名为 `migrate`、`run`；默认迁移账号内**全部桶**（可用 `--buckets` / `--exclude-buckets` 与 `--prefix` 过滤）。
- **目录结构保留**：默认映射到 `MyDrive/<output-dir>/<bucket>/<key>`；单桶模式下可加 `--flatten-single-bucket` 省略 `<bucket>/` 层。自动清理不安全控制字符并处理 S3 中“同名文件与目录前缀共存”的路径冲突（将冲突文件自动重命名为 `...__r2_object__`）。

## 断连自动续传与百万小文件并发

```bash
colabllm r2-migrate sync --output-dir r2-backup --archive-tar --tar-shard-mb 512 --no-wait
colabllm r2-migrate jobs
colabllm r2-migrate job JOB_ID --wait --timeout 600
colabllm r2-migrate pause JOB_ID
colabllm r2-migrate resume JOB_ID --archive-tar --tar-shard-mb 512 --consolidate-existing --wait --timeout 0
```

- **真正断点续传**：本地 NVMe 使用 SQLite WAL 批量提交，按固定周期（默认 60s）及退出前以原子 `sqlite3.backup()` 同步至 Drive（`MyDrive/colab-llm/r2-migrate/jobs/<JOB_ID>/checkpoint.sqlite3`）。每个桶记录 `StartAfter` 游标与对象状态（`pending` / `copied` / `deleted` / `ia_deferred` / `failed`）。
- **零浪费预检查**：重建 VM 或重启后，自动从 Drive 恢复 Checkpoint 并从 `StartAfter` 续列；在 `files` 模式下通过有界 `os.scandir` 目录缓存核对已落盘文件（一致则跳过下载）；在 `tar` 模式下自动重放 `.tar.manifest.jsonl` 与整理未清理散列。
- **百万小文件加速（方案 A + 方案 B `--archive-tar`）**：
  - 默认并发已提升至 **48 路下载 + 16 路写入**，`files` 模式采用无全局 FUSE 锁直写与 O(log N) 复合索引游标调度，`DeleteObjects` 独立后台异步批量清理。
  - **`.tar` 分卷归档（推荐用于海量小文件）**：启用 `--archive-tar`（即 `--storage-mode tar`，默认单卷 `--tar-shard-mb 512`）后，下载器先在本地 NVMe 高速打包为未压缩 POSIX PAX `.tar` 分卷，再以顺序大块整卷写入 `MyDrive/<output-dir>/archives/<bucket>/<bucket>-part-NNNNN.tar`（附 `.tar.manifest.jsonl` 与 `README-RESTORE.txt`），彻底绕过 Google Drive FUSE 对百万级小文件逐个 `open/close/rename/fsync` 的限流瓶颈；恢复解压可执行：
    ```bash
    find MyDrive/<output-dir>/archives -name '*.tar' -print0 | xargs -0 -n1 -I{} tar -xf '{}' -C ./restored
    ```
  - **已有散列文件整合打包（`--consolidate-existing`，默认开启）**：若此前已按 `files` 散列模式写入过部分文件，加 `--consolidate-existing` 在切换/续传到 `--archive-tar` 时会先以 24 线程并发预读并校验已落盘散列文件，先写入并校验 `.tar` 分卷与 SQLite 状态后，再清理已归并的散列副本与空目录，确保文件零丢失与结构完整。

## R2 API 费用优化与免费额度保护

```bash
colabllm r2-migrate sync --cost-mode free-first --output-dir r2-backup
colabllm r2-migrate sync --cost-mode free-only --max-free-class-a 950000 --max-free-class-b 9500000
colabllm r2-migrate sync --public-url assets=https://cdn.example.com --output-dir r2-backup
colabllm r2-migrate resume JOB_ID --cost-mode free-first --wait
colabllm r2-migrate usage --json
```

R2 默认月免费额度为 **1,000,000 次 Class A**（`ListBuckets`、`ListObjectsV2`）与 **10,000,000 次 Class B**（`GetObject`、`HeadObject`）；`DeleteObjects` / `DeleteObject` 在 R2 中免费。本运行时采用多层省费设计：

1. **零 `HeadObject`**：迁移列表直接复用 `ListObjectsV2`（每次最多 1,000 key + `encoding-type=url`）返回的 `Size`、`ETag` 与 `StorageClass`。
2. **零字节与目录标记对象免请求**：`size == 0` 或以 `/` 结尾的目录标记对象直接在 Drive 创建空文件/目录，**0 次 Class B**。
3. **Custom Domain CDN 缓存命中免 Class B**：通过 `--public-url BUCKET=https://...` 配置绑定的自定义域名并开启 Cloudflare 缓存后，命中 CDN 缓存（如 `CF-Cache-Status: HIT` / `REVALIDATED` 等）的读取不计入本地 Class B 账本且尽量减少 R2 回源；非命中或未配置自动回退 SigV4 S3 签名下载。
4. **分层与预算策略（`--cost-mode`）**：
   - `free-first`（默认）：Pass 1 优先迁移免费层兼容的 `STANDARD` 对象并将 `STANDARD_IA`（含最低存储期/检索费）暂存为 `ia_deferred`；Pass 1 完成后再自动迁移 Pass 2 延后队列，且优先将操作控制在月免费余量内，耗尽后自动回落按需付费并继续完成任务。
   - `free-only`：严格限制在 `--max-free-class-a`（默认 `950000`）与 `--max-free-class-b`（默认 `9500000`）以内且不拉取 `STANDARD_IA`；触及安全上限时自动以 `paused_budget` 状态暂停并落盘 Checkpoint，确保 `$0.00` 额外调用费，可次月免费额度刷新或切换 `--cost-mode free-first` 后 `resume`。
   - `unlimited`：无延迟单轮全速迁移。
5. **跨月持续对账**：`MyDrive/colab-llm/r2-migrate/usage/YYYY-MM.json` 按 UTC 月追踪 Class A/B 用量；可用 `usage --set-class-a` / `--set-class-b` 对齐 Cloudflare 控制台历史已用量。

## 校验后安全删除 R2 原文件

```bash
colabllm r2-migrate sync --buckets tmp-cache --delete-source --output-dir r2-backup
colabllm r2-migrate cleanup JOB_ID --wait
colabllm r2-migrate cancel JOB_ID
```

- **默认不删除 R2 原文件**（`--no-delete-source` 为默认）。
- **严格校验门槛与云端同步门控**：仅当 R2 下载返回 HTTP 200、字节数精确一致、单分片非加密标准 MD5 ETag 校验通过、本地持久化写入 Drive 大小校验一致，**且 Google Drive 云端 API 确认分卷/文件已真实完成上传（含非零大小与 `md5Checksum`）** 后，才允许进入待删队列；若触发 Google Drive 单账号 **750 GB / 24h 上传限流**（`userRateLimitExceeded`）或本地 DriveFS 上传缓存剩余空间不足，任务会自动切换为 `paused_quota` 并安全暂停，未完成云端同步的 R2 原文件绝不会被误删。
- **零调用费批量删除**：自动按每批最多 **1,000 个 Key** 聚合调用 S3 `DeleteObjects`（含 `Content-MD5`），属于 R2 免费操作。未开 `--delete-source` 完成迁移、或待 Drive 24h 限流窗口解锁并同步完成后，可随时用 `cleanup JOB_ID --wait` 分批核验补删已落云的 R2 原文件。

## Web UI 与远端 API

服务启动后可在 `colabllm web` 控制台点击 **打开 Web UI**，或直接打开 `up` 返回的服务 URL 登录操作：支持账号凭据配置、桶浏览、实时进度与预估费用看板、`Paused` / `Resume` / `Cleanup` 按钮及 Drive 目录检查。

常用环境变量与远端端点：`R2_MIGRATE_URL` / `--url`、`R2_MIGRATE_API_TOKEN` / `--api-token`；主要接口包括 `/health`、`/api/credentials`、`/api/buckets`、`/api/usage`、`/api/jobs`、`/api/jobs/ID/{pause,resume,cleanup,cancel}` 与 `/api/drive-list`。
