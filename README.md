# unzipfast

> 一个更快、更安全的 ZIP 解压命令行工具：多进程并行、路径穿越防护、原子写入、实时进度条。

系统自带的解压工具大多单线程，遇到「几千个小文件」的压缩包时慢得明显；网上一些脚本又缺少安全检查（`../` 路径穿越、符号链接、写到一半中断）。`unzipfast` 用约 1000 行 Python（含注释）把这两件事一起解决：**并行解压 + 默认安全**。

```console
$ unzipfast dataset.zip
Archive: dataset.zip
Output:  dataset
Files:   4,000 (62.5 MiB)
Workers: 16
 100.0% [========================]  62.5 MiB / 62.5 MiB  17.3 MiB/s  ETA 00:00  4,000/4,000
Extracted 4,000 file(s), 62.5 MiB in 3.62 s (17.3 MiB/s)
```

## 特性

- **多进程并行**：按成员分片，每个进程独立解压，自动根据数据量选择进程数（小压缩包不会因为创建进程而变慢）。
- **默认安全**：拒绝 `../` 路径穿越、绝对路径逃逸、Windows 盘符路径、符号链接和 Windows 保留文件名，只写入目标目录内部。
- **原子写入**：先写临时文件再 `os.replace`，中断或 CRC 错误不会留下半个文件。
- **CRC 校验**：`-t/--test` 可只校验不落盘；解压过程中每个文件也会校验 CRC。
- **实时进度**：速度、剩余时间、文件计数；输出不是终端时自动关闭，不污染日志。
- **筛选与整理**：`--include` / `--exclude` glob 过滤、`--flat` 去掉目录层级、`skip` / `overwrite` / `rename` 三种覆盖策略。
- **加密支持**：传统 ZipCrypto 开箱即用；WinZip AES 安装可选依赖 `pyzipper` 即可。
- **零依赖**：核心功能只用标准库，Python 3.9+，Windows / Linux / macOS 通用。

## 安装

需要 Python 3.9 或更高版本。

```bash
# 发布到 PyPI 之后可以直接安装（当前请用下面的源码方式）
pipx install unzipfast

# 或者直接 pip
pip install unzipfast

# 从源码 / GitHub 安装
pipx install git+https://github.com/xccd5/unzipfast.git

# 需要 AES 加密压缩包时
pip install "unzipfast[aes]"
```

> 项目尚未发布到 PyPI 时，请使用 `git+https://...` 或本地 `pip install -e .` 的方式安装。

也可以不安装，直接运行：

```bash
git clone https://github.com/xccd5/unzipfast.git
cd unzipfast
python -m pip install -e .
python -m unzipfast --help
```

## 快速开始

```bash
# 解压到与压缩包同名的目录（dataset.zip -> ./dataset/）
unzipfast dataset.zip

# 指定输出目录
unzipfast dataset.zip -o D:/data

# 只解压 CSV，跳过旧数据
unzipfast dataset.zip -i "*.csv" -x "old/*"

# 用 8 个进程
unzipfast dataset.zip -j 8

# 只看内容，不解压
unzipfast dataset.zip -l

# 校验压缩包是否损坏
unzipfast dataset.zip -t

# 加密压缩包（可重复 -p 尝试多个密码）
unzipfast secret.zip -p "my password"
```

## 常用示例

### 只看某一层目录

```bash
unzipfast release.zip -l | Select-String "docs/"
```

### 把一堆文件平铺出来（重名自动加序号）

```bash
unzipfast images.zip -f --overwrite rename -o ./images
# a/photo.jpg -> images/photo.jpg
# b/photo.jpg -> images/photo (1).jpg
```

### 断点续解

默认 `--overwrite skip` 会跳过已存在的文件，中断后重新执行即可继续：

```bash
unzipfast big.zip        # 中断
unzipfast big.zip        # 已完成的文件自动跳过
```

### 在脚本里静默使用

```bash
unzipfast big.zip -q --no-progress
if ($LASTEXITCODE -ne 0) { Write-Error "解压失败" }
```

## 参数

| 参数 | 说明 |
| --- | --- |
| `ARCHIVE` | 要解压的 `.zip` 文件 |
| `-o, --output DIR` | 输出目录，默认是压缩包去掉扩展名的名字 |
| `-j, --jobs N` | 进程数，`0` 表示自动（默认）；显式指定时按指定值执行 |
| `-p, --password PWD` | 压缩包密码，可重复传入以尝试多个密码 |
| `-i, --include GLOB` | 只解压匹配的成员，可重复；`*` 可跨目录 |
| `-x, --exclude GLOB` | 跳过匹配的成员，可重复 |
| `-f, --flat` | 忽略压缩包内的目录结构 |
| `--overwrite {skip,overwrite,rename}` | 目标文件已存在时的策略，默认 `skip` |
| `-l, --list` | 列出内容后退出 |
| `-t, --test` | 校验所有成员的 CRC 后退出 |
| `--max-size SIZE` | 解压总量超过 SIZE（如 `500M`、`2G`）就拒绝，防压缩炸弹 |
| `-v, --verbose` | 显示所有被跳过的条目 |
| `-q, --quiet` | 只输出错误 |
| `--no-progress` | 关闭进度条 |
| `--version` | 显示版本 |

环境变量：

| 变量 | 说明 |
| --- | --- |
| `UNZIPFAST_PASSWORD` | 未传 `-p` 时使用的密码 |
| `UNZIPFAST_JOBS` | 默认进程数 |

退出码：`0` 成功，`1` 出错（含个别文件失败），`2` 参数错误，`130` 用户中断。

## 性能

`scripts/benchmark.py` 会生成一个「很多小文件」的压缩包，分别用 1 个和 N 个进程解压：

```bash
python scripts/benchmark.py -n 4000 -s 16384
```

本机一次实测（4,000 个 16 KiB 文件，共 62.5 MiB；Windows 10 / Python 3.11 / 16 核）：

| 模式 | 耗时 | 吞吐 |
| --- | --- | --- |
| 单进程 | 15.46 s | 4.0 MiB/s |
| 16 进程 | 3.62 s | 17.3 MiB/s |
| **加速比** | | **4.27x** |

磁盘、杀毒软件和文件大小都会影响结果，请以自己机器上的 `benchmark.py` 输出为准。并行收益来自「多个文件同时解压」；单个超大文件无法拆分，此时进程数会自动降为 1。

## 安全性

解压工具是经典的攻击面，`unzipfast` 默认采取以下措施：

1. **路径穿越防护**：含 `..` 的成员、绝对路径、Windows 盘符路径直接跳过并报告，绝不会写到目标目录之外。
2. **符号链接不落地**：压缩包里的符号链接一律跳过，避免后续写入被重定向到任意位置。
3. **Windows 非法名**：`CON`、`NUL`、`aux.txt`、结尾带点/空格、`<>:"|?*` 等会导致写入异常的名字会被跳过。
4. **原子写入**：每个文件先写到同目录的 `.unzipfast-*.part`，成功后再原子替换，失败自动清理。
5. **完整性校验**：解压时由 `zipfile` 校验 CRC，损坏的成员会被单独报告，不影响其他文件。
6. **默认不覆盖**：已存在的文件默认跳过，避免误删数据。
7. **输出目录内的链接**：目标目录里已有的符号链接或联接点会被解析，指向目录外的写入会被跳过。
8. **体积上限**：`--max-size` 在写入任何文件之前，按压缩包声明的大小检查总量。

被跳过的条目会在结束时汇总显示，`-v` 可查看全部。

## 工作原理

1. **规划阶段**（`archive.py`）：只读取中央目录，过滤、规范化路径、决定每个成员的落点，得到任务列表和总字节数。此阶段不写任何文件。
2. **执行阶段**（`extractor.py`）：
   - 小压缩包或成员数很少时走单进程，避免进程创建开销；
   - 否则用 `multiprocessing` 的 `spawn` 启动 N 个进程，每个进程各自打开压缩包、解压分配到的成员；
   - 父进程通过 `imap_unordered` 汇总结果并刷新进度条。
3. **收尾**：报告成功数、失败数和被跳过的条目，返回退出码。

选择「每个进程独立打开压缩包」而不是共享一个 `ZipFile`，是因为 `fork` 后父子进程共享文件偏移量，并发读取会互相破坏；独立句柄简单且可移植。

## 项目结构

```
unzipfast/
├── src/unzipfast/
│   ├── archive.py      # 元数据、路径安全、解压计划
│   ├── extractor.py    # 并行 / 串行解压、CRC 校验
│   ├── progress.py     # 零依赖进度条
│   ├── cli.py          # 命令行接口
│   └── errors.py
├── tests/              # pytest：路径穿越、并行、加密、损坏包……
├── scripts/benchmark.py
├── .github/workflows/ci.yml
└── pyproject.toml
```

## 开发

```bash
git clone https://github.com/xccd5/unzipfast.git
cd unzipfast

python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate

pip install -e ".[dev]"
python -m pytest
python scripts/benchmark.py
```

CI 会在 Ubuntu / Windows / macOS 和 Python 3.9、3.12、3.13 上跑测试。

## 发布到 GitHub

在 GitHub 上先创建一个**空仓库**（不要勾选自动生成 README），然后：

```bash
cd unzipfast

git init
git add .
git commit -m "Initial commit: unzipfast, a fast and safe parallel ZIP extractor"
git branch -M main
git remote add origin https://github.com/xccd5/unzipfast.git
git push -u origin main
```

如果安装了 [GitHub CLI](https://cli.github.com/)，可以一步完成：

```bash
gh repo create unzipfast --public --source=. --push
```

首次提交前如果 Git 还没配置身份：

```bash
git config --global user.name "你的名字"
git config --global user.email "you@example.com"
```

发布到 PyPI（可选）：

```bash
python -m pip install build twine
python -m build
python -m twine upload dist/*
```

## 已知限制

- 一个超大文件无法并行解压（ZIP 的 deflate 流不支持随机切分）。
- 不支持分卷 ZIP（`.z01`、`.z02`…）。
- 符号链接和硬链接会被跳过，而不是重建。
- 只处理 ZIP；7z、rar、tar 不在范围内。

## 许可证

[MIT](LICENSE)

---

## English

`unzipfast` is a small, dependency-free CLI that extracts ZIP archives with
multiple processes, a live progress bar, glob filtering and safe defaults
(path-traversal and symlink protection, atomic writes, CRC verification).

```bash
pipx install git+https://github.com/xccd5/unzipfast.git
unzipfast archive.zip -o output -j 8
unzipfast archive.zip -l        # list
unzipfast archive.zip -t        # verify CRCs
```

Requires Python 3.9+. Optional AES support: `pip install "unzipfast[aes]"`.
Licensed under MIT.
