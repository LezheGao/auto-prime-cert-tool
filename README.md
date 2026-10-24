# Auto Prime Cert Tool

> 自动从 FactorDB 拉取 PRP（probable prime），用 cypari 生成 ECPP 素性证书，并自动上传回 FactorDB。
> Automatically fetch PRPs from FactorDB, generate ECPP primality certificates with cypari, and upload them back to FactorDB.

> **当前平台支持 / Platform support**：仅支持 **Windows 10/11 (64-bit)**，推荐直接下载预编译的 `.exe`，开箱即用，无需安装 Python 或 PARI/GP。Linux 与 macOS **暂不支持**（路径处理目前为 Windows 格式，后续版本会改进）。
> **Currently Windows 10/11 (64-bit) only.** Download the prebuilt `.exe` — no Python or PARI/GP installation required. Linux and macOS are **not yet supported** (path handling is currently Windows-specific; planned for a future release).

---

## 中文

### 简介

本工具面向数论爱好者与 FactorDB 贡献者：它从 FactorDB 的 JSON-RPC 接口批量拉取尚未被证明的 PRP，调用 cypari（内置 PARI 库）生成 ECPP 证书，再把证书自动上传回 FactorDB，使这些数字从"可能素数"升级为"已证明素数"。

程序采用 **多进程 worker + 单 fetcher 线程 + 单 collector 线程** 的流水线结构：

- **Fetcher**：按需向 FactorDB 请求 `primo_batch`，并做去重与背压控制。
- **Worker**（可多个）：各自持有独立的 cypari 实例，生成并写出 `.out` 证书文件，然后上传。
- **Collector**：汇总统计、维护 `in_flight` / `recent_done`、统一打印日志。

### 功能特性

- 多进程并行 ECPP，支持每个 worker 独立设置 `parisizemax`。
- 自动处理 HTTP 429 限流，指数退避重试。
- `in_flight` + `recent_done` 双层去重，避免服务端缓存延迟导致的重复计算。
- 上传成功自动删除本地 `.out`；失败保留文件以便手动重试。
- 证书 `[Comments]` 段自动改写为工具署名（含版本与仓库链接）。
- User-Agent 可选附带 `mailto`，便于 FactorDB 管理员联系。
- 双语界面（中文 / English），所有配置通过命令行参数提供，无交互式输入。
- 优雅退出：`Ctrl+C` 后停止拉取、通知 worker、30 秒超时强杀。

### 环境要求

- **Windows 10 / 11（64 位）**
- 使用预编译 `.exe` 时：**无需任何额外依赖**（cypari 及其内置的 PARI 库已打包进程序）。
- 从源码运行（开发者）：Python 3.8+ 与 `pip install cypari`（Windows 版 wheel 已自带 PARI，无需单独安装 PARI/GP）。

> Linux / macOS 暂不支持，原因是程序内部的路径处理目前按 Windows 格式编写，后续版本会加入跨平台支持。

### 快速开始（Windows）

1. 从 [Releases](https://github.com/LezheGao/auto-prime-cert-tool/releases) 下载最新的 `autoprimecert.exe`。
2. 建议在同目录下打开 `cmd` 或 PowerShell 运行（便于查看日志和收集 `.out` 文件）。
3. 常用命令：

```bat
:: 只生成、不上传（无 token）
autoprimecert.exe --count 5 --token none

:: 生成并上传（推荐用环境变量提供 token）
set FACTORDB_TOKEN=your_token_here
autoprimecert.exe --count 10 --min-digits 1000 --workers 4

:: 无限循环模式（持续拉取，Ctrl+C 停止）
autoprimecert.exe --workers 2
```

默认输出目录为当前目录下的 `.\certs`，可用 `--output-dir` 指定。

### 命令行参数

| 参数 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `--lang` | `zh` / `en` | 环境变量 `APP_LANG`，否则 `zh` | 界面语言 |
| `--output-dir` | 路径 | `.\certs` | 证书 `.out` 文件输出目录 |
| `--min-digits` | int | `1000` | 拉取 PRP 的最小十进制位数 |
| `--parisizemax` | int | `1000000000` | 每个 worker 的 PARI 内存上限（字节） |
| `--token` | str | 环境变量 `FACTORDB_TOKEN` | FactorDB API token；空或 `none` 表示跳过上传 |
| `--email` | str | 无 | 可选联系邮箱，会写入 User-Agent 的 `mailto` 字段 |
| `--workers` | int | `5` | 并发 worker 进程数 |
| `--max-retries` | int | `5` | 上传失败最大重试次数（主要用于 429） |
| `--base-backoff` | float | `2.0` | 指数退避基数（秒） |
| `--count` | int | 无（无限循环） | 任务总数；省略则进入循环模式；上限 32000 |
| `--queue-size` | int | 自动 | `task_q` 容量；默认 `max(workers*2, 4)`，有限模式再与 `count` 取小 |
| `--recent-maxlen` | int | `200` | `recent_done` 容量；`0` 关闭去重缓存 |
| `--debug` | flag | 关闭 | 输出 DEBUG 日志（含 `X-Fdb-Resources`、上传响应体） |

### 使用示例

```bat
:: 英文界面 + 邮箱 + 调试日志
autoprimecert.exe ^
    --lang en ^
    --token 97b9a068c963ee3b6f7eb5a463f2b00d ^
    --email me@example.com ^
    --count 20 ^
    --min-digits 1200 ^
    --workers 3 ^
    --output-dir .\certs ^
    --debug

:: 使用 2 个 worker、10 次退避、最近去重缓存 500
autoprimecert.exe --count 100 --workers 2 --max-retries 10 --recent-maxlen 500
```

### 输出与文件

- 证书文件命名为 `factordb_<fid>.out`，使用 Primo 文本格式。
- 上传成功后自动删除；上传失败则保留，文件名会出现在最终统计的"失败清单"里。
- 证书 `[Comments]` 段会被改写为：

  ```
  [Comments]
  Uploaded by auto primecert tool vx.x via cypari vx.x
  https://github.com/LezheGao/auto-prime-cert-tool
  ```

（x.x为版本号）
  其余 `[Candidate]`、各 `[...]` 数学数据保持 cypari 原样输出。

### 环境变量

| 变量 | 作用 |
|---|---|
| `FACTORDB_TOKEN` | 默认上传 token（可被 `--token` 覆盖） |
| `APP_LANG` | 默认界面语言（`zh` / `en`），可被 `--lang` 覆盖 |
| `LOG_LEVEL` | 启动时的初始日志级别（默认 `INFO`） |

### 礼貌使用与合规

本工具会向 **公共实例 FactorDB** 发送请求并上传公开证书，请务必：

- **自备 token**，不要把别人的 token 写进脚本或分享出去。
- **尊重限流**：遇到 `429` 或 `X-Fdb-Resources` 偏低时请降低频率或暂停。
- **上传即公开**：你上传的证书会进入 FactorDB 公共数据库，请确认所证明的数字你有意公开。
- 本项目与 FactorDB 官方无关，使用即表示你同意遵守其服务条款。

### 故障排查

| 现象 | 可能原因与处理 |
|---|---|
| 双击 exe 闪退或窗口一闪而过 | 从 `cmd` / PowerShell 中运行，便于看到完整日志 |
| 杀毒软件误报 | PyInstaller 打包程序偶有误报，可加信任或从源码自行编译 |
| 上传一直 429 | 降低 `--workers`、调大 `--base-backoff`，或暂停一段时间 |
| 上传返回 `added=0` | 证书可能已存在或不被接受，保留的 `.out` 可手动检查后重传 |
| 拉不到 PRP（有限模式提前结束） | FactorDB 当前没有满足 `--min-digits` 的可用 PRP，稍后再试 |
| `parisizemax` 相关报错 | 调小 `--parisizemax`，并确认系统可用内存 |
| 输出目录写入失败 | 检查 `--output-dir` 是否有写权限，避免使用系统保护目录 |

### 许可证

本项目已附带 `LICENSE` 文件，详见仓库根目录。

---

## English

### Overview

This tool is for number-theory enthusiasts and FactorDB contributors. It batch-fetches PRPs from FactorDB's JSON-RPC API, generates ECPP primality certificates using cypari (which bundles PARI), and uploads the certificates back to FactorDB — upgrading those numbers from "probable prime" to "proven prime".

The pipeline is built from **multiple worker processes + a single fetcher thread + a single collector thread**:

- **Fetcher**: requests `primo_batch` from FactorDB on demand, with deduplication and backpressure.
- **Workers** (N processes): each owns an independent cypari instance, generates and writes a `.out` certificate, then uploads it.
- **Collector**: aggregates statistics, maintains `in_flight` / `recent_done`, and is the sole emitter of worker log lines.

### Features

- Parallel multi-process ECPP with per-worker `parisizemax`.
- Automatic HTTP 429 handling with exponential backoff.
- Two-layer deduplication (`in_flight` + `recent_done`) to survive server-side cache delays.
- Deletes local `.out` after a successful upload; keeps it on failure for manual retry.
- Rewrites the certificate `[Comments]` block with tool attribution (version + repo link).
- Optional `mailto` appended to the User-Agent so FactorDB admins can reach you.
- Bilingual UI (Chinese / English). All settings are supplied via CLI; no interactive prompts.
- Graceful shutdown: `Ctrl+C` stops fetching, notifies workers, and force-terminates after a 30 s timeout.

### Requirements

- **Windows 10 / 11 (64-bit)**
- When using the prebuilt `.exe`: **no extra dependencies** (cypari and its bundled PARI library are packaged inside).
- When running from source (developers): Python 3.8+ with `pip install cypari` (the Windows wheel already bundles PARI — no separate PARI/GP install needed).

> Linux and macOS are **not yet supported** — path handling in the program is currently Windows-specific. Cross-platform support is planned for a future release.

### Quick Start (Windows)

1. Download the latest `autoprimecert.exe` from [Releases](https://github.com/LezheGao/auto-prime-cert-tool/releases).
2. Open `cmd` or PowerShell in the same folder (so you can see logs and collect `.out` files).
3. Common commands:

```bat
:: Generate only, no upload
autoprimecert.exe --count 5 --token none

:: Generate and upload (token from environment)
set FACTORDB_TOKEN=your_token_here
autoprimecert.exe --count 10 --min-digits 1000 --workers 4

:: Loop forever (Ctrl+C to stop)
autoprimecert.exe --workers 2
```

The default output directory is `.\certs` under the current working directory; override with `--output-dir`.

### Command-line Arguments

| Option | Type | Default | Description |
|---|---|---|---|
| `--lang` | `zh` / `en` | env `APP_LANG`, else `zh` | UI language |
| `--output-dir` | path | `.\certs` | Directory for generated `.out` files |
| `--min-digits` | int | `1000` | Minimum decimal digits of PRPs to fetch |
| `--parisizemax` | int | `1000000000` | PARI memory cap per worker (bytes) |
| `--token` | str | env `FACTORDB_TOKEN` | FactorDB API token; empty or `none` skips upload |
| `--email` | str | none | Optional contact email for the UA `mailto` field |
| `--workers` | int | `5` | Number of worker processes |
| `--max-retries` | int | `5` | Max upload retries (mainly for 429) |
| `--base-backoff` | float | `2.0` | Exponential backoff base (seconds) |
| `--count` | int | none (loop) | Total tasks; omit for loop mode; max 32000 |
| `--queue-size` | int | auto | `task_q` capacity; default `max(workers*2, 4)`, capped by `count` in finite mode |
| `--recent-maxlen` | int | `200` | `recent_done` capacity; `0` disables the dedup cache |
| `--debug` | flag | off | Verbose logs (incl. `X-Fdb-Resources`, upload bodies) |

### Examples

```bat
:: English UI + email + debug logs
autoprimecert.exe ^
    --lang en ^
    --token 97b9a068c963ee3b6f7eb5a463f2b00d ^
    --email me@example.com ^
    --count 20 ^
    --min-digits 1200 ^
    --workers 3 ^
    --output-dir .\certs ^
    --debug

:: 2 workers, up to 10 backoff steps, recent cache of 500
autoprimecert.exe --count 100 --workers 2 --max-retries 10 --recent-maxlen 500
```

### Output Files

- Certificates are written as `factordb_<fid>.out` in Primo text format.
- Removed on successful upload; kept on failure and listed in the final "Failed list".
- The `[Comments]` block is rewritten to:

  ```
  [Comments]
  Uploaded by auto primecert tool vx.x via cypari vx.x
  https://github.com/LezheGao/auto-prime-cert-tool
  ```

(x.x is the version number)
  All mathematical data (`[Candidate]`, `[1]`, etc.) is left as produced by cypari.

### Environment Variables

| Variable | Purpose |
|---|---|
| `FACTORDB_TOKEN` | Default upload token (overridden by `--token`) |
| `APP_LANG` | Default UI language (`zh` / `en`), overridden by `--lang` |
| `LOG_LEVEL` | Initial log level at startup (default `INFO`) |

### Responsible Use

This tool talks to the **public FactorDB** instance. Please:

- **Use your own token**; never hard-code or share someone else's.
- **Respect rate limits**: back off when you see `429` or low `X-Fdb-Resources`.
- **Uploads are public**: certificates enter FactorDB's public database.
- This project is not affiliated with FactorDB. Use of it implies acceptance of FactorDB's terms of service.

### Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| Double-clicking the exe flashes and closes | Run from `cmd` / PowerShell to see the full log |
| Antivirus false positive | Common with PyInstaller-built binaries; add an exclusion or build from source |
| Persistent 429 | Lower `--workers`, raise `--base-backoff`, or pause |
| Upload returns `added=0` | Certificate may already exist or be rejected; inspect the kept `.out` |
| No PRPs (finite mode ends early) | FactorDB has no eligible PRPs right now; retry later |
| `parisizemax` errors | Lower `--parisizemax`; check available RAM |
| Cannot write to output directory | Verify write permission for `--output-dir`; avoid protected system folders |

### License

This project ships with a `LICENSE` file; see the repository root for details.
