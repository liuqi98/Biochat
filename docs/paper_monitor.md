# 论文监测器

监测器定期查询官方文献 API，把符合主题的论文及变化保存到本地收件箱。
不依赖 LLM、GPU、原 CD19 项目或重型文献解析依赖；不会执行论文中的代码或将未验证方法注册为可用技术。

## 运行一次

在安装 BioChat 的 Python 环境中运行：

```bash
python -m biochat.papers scan --lookback-days 7 --max-pages 20
python -m biochat.papers updates --limit 20
```

默认主题是“抗体/VHH/H3”与“机器学习/生成/扩散/逆折叠等技术”的交集。
默认查询 Europe PMC 和 bioRxiv。可以用 `--sources europepmc` 或 `--sources biorxiv` 单独运行。
首次回看 7 天；后续按检查点扫描，每次回查 2 天的重叠区间，以补抓延迟入库或窗口边界的记录。
日期窗口使用 UTC。`--lookback-days` 不会重置已有检查点；需要独立回溯时使用新的 `--state-dir`。

每个来源默认最多请求 20 页。CLI 在 stderr 显示来源和页码，在 stdout 输出新增/更新概要。
`--json` 输出完整 JSON，包括摘要。来源失败、未扫描完或旧窗口尚未追到当前日期时返回 `partial`，退出码为 2；
再次执行相同命令会继续。完整扫描退出码为 0，配置/运行错误退出码为 1。
一个来源失败不会阻止另一个来源保存结果。部分扫描时，**零新增不代表没有新论文**。

## 数据与去重

默认数据目录为 `$XDG_DATA_HOME/biochat/paper_monitor`，未设置时为 `~/.local/share/biochat/paper_monitor`。
可以用 `--state-dir /path/to/paper_monitor` 指定固定目录。

- `papers.sqlite3`：主题、来源、窗口检查点、论文版本记录和持久化事件。
- `scan_lock.sqlite3`：跨进程扫描锁；并发第二个扫描返回 `busy`，不会重复推进游标。
- `runs/<scan_id>.json`：每次扫描的结果、来源错误和检查点。收件箱独立于报告保留。

主题配置哈希与来源共同决定检查点。每页的论文记录和分页游标在同一事务中提交；
遇到中断或失败，已完成页保留，未完成页重试。暂停的日期窗口保持固定，完成后再追赶新日期。

事件类型：

| 类型 | 含义 |
|---|---|
| `new` | 当前主题首次发现的论文 |
| `new_version` | 同一来源论文出现更高/新版本 |
| `metadata_updated` | 已知版本的标题、摘要或其他已跟踪元数据变化 |
| `source_added` | 同一 DOI 在另一个来源/记录中出现，补充来源而不计为第二篇新论文 |
| `historical_version` | 后来遇到较早版本；保留历史，不当作升级 |

DOI 标准化后用于跨来源去重；缺少 DOI 时使用来源及其记录 ID，不凭相似标题强行合并。
预印本与正式发表论文若 DOI 不同，保持独立记录；bioRxiv 提供的正式发表 DOI 作为关联信息保存。
没有明确版本号的 Europe PMC 记录用元数据指纹发现变化，不将其声称为论文 v2/v3。
引用次数不参与变化检测。元数据改变并不自动表示研究方法发生变化。

`updates` 按事件 ID 翻页：

```bash
python -m biochat.papers updates --after-id 0 --limit 20 --json
# 将返回的 next_after_id 用于下一次读取。
```

## 自定义主题

参考 [配置示例](../examples/antibody_paper_watch.json)。组内是 OR，组间是 AND，匹配标题和摘要。
配置示例与内置默认主题一致；实际修改主题或名称会获得独立检查点。

```bash
python -m biochat.papers scan --config examples/antibody_paper_watch.json
python -m biochat.papers updates --config examples/antibody_paper_watch.json
```

Europe PMC 在服务端检索主题并使用 `UPDATE_DATE` 日期过滤；bioRxiv 日期接口返回的记录在本地过滤。
关键词筛选会有漏检或误检，摘要缺失也可能影响召回；可调整同义词或分开配置多个主题。
重叠窗口降低延迟入库漏检风险，但不保证捕获无限期延迟的记录，较长范围的定期回溯需独立安排。

## 每 6 小时自动运行

监测器是单次任务，适合由 cron 或系统定时器调用。例如 crontab：

```cron
0 */6 * * * /absolute/path/to/python -m biochat.papers scan --state-dir /absolute/path/to/paper_monitor >> /absolute/path/to/paper_monitor.log 2>&1
```

替换 Python 和路径；定时进程必须使用安装了 BioChat 的环境。检查 `partial` 和来源错误，必要时增大分页预算或提高运行频率。
仓库仅提供配置示例，运行一次 `scan` 不会自动安装定时任务，也不会发送邮件或其他外部消息。

## Agent 接口

```python
from biochat.tool.paper_monitor import monitor_antibody_papers, list_paper_updates

result = monitor_antibody_papers(lookback_days=7, max_pages=20)
events = list_paper_updates(after_id=0, limit=20)
```

两个工具在 minimal/full 模式中都注册。扫描工具返回紧凑结果和完整报告路径，读取工具按 ID 翻页。
Agent 的代码执行权限继续遵守现有配置；监测器 CLI 可以在安装环境中独立运行。
论文摘要和代码链接属于未验证的外部资料；链接只从返回摘要中提取，不会自动抓取仓库、下载权重或执行命令。
输出统一标为 `discovered_not_validated`，供后续的方法卡提取器处理。

## 来源和范围

- [Europe PMC REST API](https://europepmc.org/RestfulWebService)：core 元数据与 cursorMark 分页。
- [bioRxiv API](https://api.biorxiv.org/)：日期区间、游标分页、DOI 和版本信息。

本版不包括 arXiv、GitHub release 监测、全文解析、方法卡生成或模型复现；这些可以消费同一收件箱继续扩展。
接口有索引延迟和服务可用性限制，因此“已追到今日”表示已扫描 API 当前可返回的记录，并非保证覆盖所有刚发表的论文。

验证：`pytest -q tests/test_paper_monitor.py`；测试使用合成元数据，不依赖真实网络。
