# memory_monitor

独立的进程树内存监控工具，适用于 FAW、data_prepare、RNN 和其他长时间运行的命令。代码不导入这些项目，也不修改其计算、配置或产物。

需要 **Python 3.10+，Linux 或 macOS**。仅使用标准库，无需安装依赖。Linux 读取 `/proc`；macOS 调用系统 `ps`。以下命令从 `factor_sys` 执行。

## 启动并监控 FAW

```bash
nohup python memory_monitor/run_monitor.py run \
  --output memory_monitor/runs \
  --interval 1 \
  --report-interval 30 \
  -- python -u faw/run_faw.py run --config faw/configs/d01.toml \
  > memory-d01.log 2>&1 &
```

`--` 后是原始业务命令，按参数直接执行，不经过 shell。监控器继承当前环境；目标命令默认使用当前工作目录，可用 `--cwd` 指定。Python 长任务建议保留 `-u`，便于及时识别日志阶段。

每次使用一个**新的输出目录**，已有目录会拒绝执行，不覆盖旧记录。目标程序的 stdout/stderr 默认合并到该目录的 `command.log`；监控摘要输出到上例的 `memory-d01.log`。查看业务日志：

```bash
tail -f memory_monitor/runs/d01-20261005-01/command.log
```

若需要单独指定业务日志文件，可在 `--` 前增加 `--command-log /path/to/new-faw.log`；这个文件也必须不存在，其父目录需已经存在。工具不提供交互式终端透传，适用于批处理任务。

## 附加到已经运行的任务

将 `12345` 替换为当前 FAW **主进程 PID**：

```bash
nohup python memory_monitor/run_monitor.py attach \
  --pid 12345 \
  --output memory_monitor/runs/d01-attach-01 \
  --stage-log faw2.log \
  > memory-d01-attach.log 2>&1 &
```

`--stage-log` 可省略。附加监控只包含开始监控后的采样，不追溯此前峰值。停止 attach 监控不会向被监控任务发送信号；不能附加到监控器自身或祖先进程。首次读取已有日志只读末尾最多 256 KiB，避免把几小时前的旧阶段当成当前阶段。

## 输出和口径

| 文件 | 内容 |
| --- | --- |
| `samples.jsonl` | 每次采样一行 JSON，逐条刷新；不在内存中累计全部历史 |
| `summary.json` | 原子替换的摘要，默认每 30 秒及结束时更新；全程/分阶段峰值、时间、进程数、监控错误与退出码 |
| `command.log` | run 模式目标程序日志；attach 不创建或修改目标日志 |

监控器自身的每条运行日志（启动信息、内存摘要、告警和错误）都有生成时的 UTC 时间戳，精确到毫秒，例如：

```text
[2026-10-05T10:18:30.123+00:00] [memory] current_tree_rss=12.345 GiB sampled_peak_tree_rss=14.567 GiB processes=33 samples=120 stage=[6/7] rolling walk-forward member selection
```

控制台输出和重定向后的 `memory-d01.log` 使用相同格式，无需额外配置。`samples.jsonl` 每条记录已有相同格式的 `timestamp` 字段，表示采样开始时间；日志前缀表示该条消息输出时的时间。`command.log` 原样保存目标程序输出，其时间戳由目标程序提供。

`summary.json` 中重点看：

- `peaks.tree_rss.value_bytes`：同一次采样中，主进程和已发现后代的 RSS 合计之最大值。不是各进程自身历史峰值的相加。
- `peaks.root_rss`：主进程采样峰值；`peaks.monitor_rss`：监控器自身 RSS，独立记录，不加到目标进程树。
- `peaks.tree_rss.timestamp/stage`：峰值采样时间及当时的近似阶段。
- `stages`：各日志阶段的采样峰值及样本数，最多保留 256 个命名阶段和一个 `__other__` 汇总桶。
- `incomplete_sample_count`、`sampling_error_count`：进程读取权限、进程退出等造成的不完整观测。不可读值以 `null` 或覆盖数表示，不填成零。
- `sampling_seconds_total/max`：读取进程指标、阶段日志和可选 PSS/cgroup 的耗时，不含写出 JSON/摘要的全部开销，也不等于业务增加的墙钟时间。
- `metadata.monitor_cpu_seconds_excluding_helpers`：监控器自身累计 CPU 时间，不含 macOS 的 `ps` 子进程。
- `metadata.status/returncode/exit_code`：任务与监控状态。run 返回目标命令的退出码；目标被信号终止时，包装命令退出码为 `128 + 信号编号`。退出码 137 本身不能证明发生 OOM。

RSS 包含共享页。FAW 的 fork worker 可共享大量行情数据，因此 **RSS 相加会重复计算共享内存，不能直接与机器的 256 GiB 对比来判断 OOM**。每秒采样也可能遗漏瞬时尖峰、短命 worker，读取进程树并非原子快照。Linux `/proc` RSS 本身也是内核近似计数。

默认从已有 FAW 日志识别顶层阶段、部分 fold/scope 开始标记。只消费开始标记，不把结束时的耗时打印当成开始。阶段归属是“采样时最近识别到的日志标记”，受日志缓冲和读取滞后影响；没有开始标记的细分步骤会合并，不能视为精确的逐函数内存归因。

同时支持旧日志和带 `[2026-10-05T15:04:05+08:00]` 时间前缀的新日志；时间不计入阶段名称。自定义 `--stage-regex` 仍匹配原始整行，包括时间前缀。

其他项目可以传入自定义开始标记，例如日志为 `STAGE loading`：

```bash
python memory_monitor/run_monitor.py run \
  --output memory_monitor/runs/custom-01 \
  --stage-regex '^STAGE (?P<stage>.+)$' \
  -- python -u your_program.py
```

正则须包含名为 `stage` 的捕获组或至少一个捕获组。每次最多读取 256 KiB 日志，忽略超过 64 KiB 的单行；阶段名最长 256 字符。日志轮转/截断会重新识别。

## 可选：PSS 和 cgroup

Linux 可增加 `--pss-interval 10`。PSS 按共享者数量分摊共享页，更适合观察 fork 共享后的驻留内存，但读取比 RSS 昂贵，默认关闭。`peaks.pss` 同时记录 `pss_process_count/process_count`；权限不足、进程退出会产生部分覆盖，不能当成完整总量。PSS 读取和 RSS 不同步，进程快速退出或 PID 复用仍可能造成观测竞态。此项不是全系统内存压力或 OOM 判据。

已有 cgroup v2 隔离环境时，可以增加：

```text
--cgroup /sys/fs/cgroup/path/to/your-task
```

工具只读 `memory.current`、可用时的 `memory.peak` 和 `memory.events`，记录在每次样本的 `cgroup` 及摘要 `latest.cgroup` 中；启动读数另保存在 `metadata.cgroup_initial`。**不会创建 cgroup、移动任务、设置限制或重置峰值**。目录不存在、无支持的计数文件时启动失败。

这些数值属于所指定的整个 cgroup，可能包含别的任务、文件缓存和内核内存；`memory.peak` 可能包含本次运行之前的峰值。只有任务确实在独立且正确的 cgroup 内时，才能用它判断该隔离范围的压力。工具不自动把登录会话或整台机器的 cgroup 数字认作当前任务内存。

## 运行开销和异常行为

- 默认每 1 秒读取一次操作系统计数，每 30 秒打印/保存摘要。Linux 每次扫描可见 PID，成本随机器进程数增加；macOS 每次启动一次 `ps`。不遍历 Python 对象、不扫描因子矩阵、不调用 GC、不复制业务数据。
- 内存保留量随当前可见进程数和阶段上限增长，不随任务运行时长增长。轻量监控进程可先按 20–60 MiB 预留，但这是容量估算，实际值看 `peaks.monitor_rss`；日志每次最多读 256 KiB，阶段表有上限。采样历史只写磁盘，文件大小随采样次数增长。
- 采样、阶段读取或摘要写入出现问题时，尽可能告警并保留目标执行和退出码；操作系统彻底拒绝进程读取时，run 会在启动业务前报错。磁盘写满可能无法保存最终摘要；文件刷新不承诺断电持久性。
- run 包装器收到 SIGINT/SIGTERM 时向自己启动的目标进程组转发；设置 5 秒宽限期，到期后在下一次控制循环强制清理（正在进行的系统读取可能延迟该步骤）。若根进程先退出，剩余同组进程立即清理。attach 只停止监控。主动脱离该进程组的守护进程不保证能被取消清理。
- 正常运行监控到目标主进程退出为止，不继续监控有意留下的后台任务。已观察后代重设父进程后仍可在后续采样中识别；两次采样间生成并脱离树的进程可能遗漏。
- `samples.jsonl` 持续刷新，OOM/SIGKILL 后通常能保留此前样本；若监控器也被杀死，不保证存在最终 summary 或捕捉到最后一瞬间的峰值。

是否明显拖慢业务，应以同一机器和数据的监控开/关对照为准。工具自身的计时不替代这项对照；生产规模峰值和开销未在本机合成测试中证明。

## 安装和测试

直接用 `run_monitor.py` 无需安装。也可以单独复制整个目录后安装：

```bash
python -m pip install -e ./memory_monitor
memory-monitor --help
```

运行测试，无需第三方测试依赖：

```bash
PYTHONPATH=memory_monitor/src python -m unittest discover -s memory_monitor/tests -v
```

测试包含模拟 Linux `/proc`/cgroup、PID 复用、记录内存边界、日志轮转以及真实子进程树、信号转发和 attach 退出。若安全沙箱禁止进程读取，真实进程测试会明确跳过；需在服务器或允许读取进程列表的本机环境补跑。

2026-10-05 本机 macOS 验证：49 项测试通过，无跳过；包含真实关闭输出管道、监控读取/写盘关闭异常后保留目标退出码的回归验证。Linux `/proc`、PSS 和 cgroup 路径使用模拟文件测试，尚未在生产 Linux 服务器实测。

默认 1 秒采样的父子进程演示运行约 4.7 秒，记录 6 个样本并识别三个 FAW 开始阶段；目标树采样 RSS 峰值约 79.9 MiB，监控器自身约 19.4 MiB。6 次采样读取合计约 158 毫秒，包含 macOS `ps` 调用；这是监控器的采样耗时，不是目标程序被拖慢的时间，也不能推算生产运行开销。
# memory_monitor
