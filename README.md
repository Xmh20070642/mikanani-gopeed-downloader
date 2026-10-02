# mikan-gopeed

macOS 本地的 Mikan 追番自动下载服务：监听你的 Mikan 订阅 RSS，有更新就自动提取磁力链、调用 [Gopeed](https://github.com/GopeedLab/gopeed) 桌面版下载，并把每一集按「番剧目录 → 时间 + 标题」归档、记录到桌面的 Markdown 文档里。

> ⚠️ 仅供个人学习与自动化研究。请支持正版，资源版权归原作者所有。

## 功能

- **RSS 轮询**：每 10 分钟（可配）读取 Mikan `MyBangumi` 订阅，只处理新增条目
- **磁力提取三级回退**：条目页面抓 `magnet:` → 下载 `.torrent` 计算 infohash 拼磁力 → 最后才把 torrent 直链交给 Gopeed
- **番剧目录匹配**：繁简转换、别名表、相似度匹配（阈值可调），对不上时自动建新目录；两个候选无法区分时宁可不下并弹通知
- **四重去重**：本地状态文件、infohash、RSS GUID、番剧目录内同集文件，绝不重复下载
- **下载状态跟踪**：任务创建后服务会持续盯梢，下载完成后约 15~30 秒内自动把记录写进桌面文档；超大文件超出等待上限（`record_wait_seconds`，默认 15 分钟）时由下一轮巡检兜底，记录不丢。失败自动重试（默认 3 次）并弹 macOS 通知
- **双保险**：记录文档在服务目录有一份实时备份，桌面文件出意外可自动重建

## 环境要求

- macOS（依赖 launchd、`osascript` 通知、Gopeed 桌面版）
- [Gopeed 桌面版](https://github.com/GopeedLab/gopeed/releases)（已验证 dev 版；TCP API 不需要开启，服务走它本地 socket 的 `/forward` 通道）
- 系统 Python 3.9+（`/usr/bin/python3` 即可，无第三方依赖，只用标准库）

## 快速开始

```bash
git clone https://github.com/Xmh20070642/mikanani-gopeed-downloader.git && cd mikan-gopeed

# 1. 编辑 config.json，填入你的 RSS 订阅链接和番剧根目录
vim config.json

# 2. 先空跑一轮看看会做什么（不会真的下载）
python3 mikan_gopeed.py --once --dry-run --verbose

# 3. 手动跑一轮（真的下载）
python3 mikan_gopeed.py --once --verbose

# 4. 没问题后安装 launchd 定时服务（每 10 分钟一轮，登录自启）
./setup_launchd.sh
```

启动 Gopeed 不是必须的：服务发现 Gopeed 没在运行时会自动 `open -a Gopeed` 拉起它。

## 获取 RSS 订阅链接

在 [Mikan](https://mikanani.me) 登录后进入「我的 Bangumi」订阅页面，复制 RSS 订阅链接（形如 `https://mikanani.me/RSS/MyBangumi?token=...`）。**这个链接等于账号凭证，不要外传、不要提交进仓库。**

## 已验证环境与已知限制

- 仅在 macOS + Gopeed 桌面版（2026-03 dev 构建）验证过。Gopeed 上游若改动 `/forward` 转发通道或任务状态字符串（`done`/`error`），需要同步适配。
- 季数识别支持「第X季」和标题末尾的独立数字（如「碧蓝之海 3」），不支持 `S2` / `Season 2` / 罗马数字写法——这类番剧请在 `aliases` 里补上 RSS 标题写法。
- 繁简转换表只覆盖常见字，没覆盖到的字同样用 `aliases` 兜底。
- 记录文档和通知文案是中文；`completed_dir_name`、`record_path` 等可按自己习惯配置。
- 两个候选目录打分接近时该条目会被搁置（不下载、每轮通知），直到你在 `aliases` 里消歧。

## 配置说明

所有路径都支持 `~` 展开。完整键位见 `config.json`（仓库自带的就是占位模板）：

| 键 | 说明 |
|---|---|
| `rss_url` | Mikan 订阅链接（含 token，注意保密） |
| `anime_root` | 番剧根目录，只匹配它下面的一级子目录 |
| `completed_dir_name` | 完结番剧所在目录名，匹配时跳过（默认 `已完結`） |
| `state_path` | 去重状态文件 |
| `record_path` | 更新记录 Markdown（一般放桌面） |
| `record_backup_path` | 记录的备份位置 |
| `pending_path` | 下载中任务的跟踪文件 |
| `max_task_attempts` | 单集下载失败的最大尝试次数 |
| `record_wait_seconds` | 任务创建后盯梢下载完成的最长等待时间（默认 900 秒） |
| `poll_interval_seconds` | 常驻模式的轮询间隔（launchd 模式下不生效，间隔由 plist 定） |
| `match_threshold` / `match_margin` | 标题匹配的相似度阈值 / 决胜差距 |
| `gopeed_api.unix_socket` | Gopeed 桌面版本地 socket 路径 |
| `gopeed_api.base_url` / `token` | 备用：Gopeed TCP API（需要在 Gopeed 设置里手动开启） |
| `gopeed_api.start_app` / `app_name` | Gopeed 未运行时是否自动拉起 |
| `aliases` | 番剧目录名 → RSS 标题写法的别名表 |

番剧目录匹配是「最贴近」策略：繁简、别名、相似度综合打分，不要求完全一致；分差太小时视为歧义，跳过并通知，不会猜。

## 安全护栏

服务对"番剧库被移动/改名"这类不会报错但后果严重的操作做了防护，两者都会弹通知并跳过下载，绝不自作主张：

- **根目录不存在**（整库被移动/删除）：拒绝重建目录树，跳过本轮下载。移动番剧库后请同步更新 `anime_root`。
- **系列目录疑似改名**：每个系列下载过的目录都会被记住；当某系列突然匹配不到任何目录时，拒绝自动建新文件夹（防止库裂成两半）。在 `aliases` 里补上新目录名后即恢复。

## 更换 Mikan token

RSS 链接等同于 Mikan 账号凭证。若需要更换：在 Mikan「我的 Bangumi」页面重新生成订阅链接（旧链接立即失效），然后把 `config.json` 里 `rss_url` 的**整个值**替换为新链接。更换完成前服务会每小时提醒一次拉取失败，已有记录不受影响。

## 卸载

```bash
launchctl bootout gui/$(id -u)/com.mikan-gopeed
rm ~/Library/LaunchAgents/com.mikan-gopeed.plist
```

## 版本历史

- **v1.1.0**（当前）：任务创建后服务持续盯梢，下载完成约 15~30 秒内即写入记录（原先依赖 10 分钟巡检，平均延迟 5 分钟）；修复失败重试机制——失败任务现在会自动从 Gopeed 清理后重新创建，最多尝试 `max_task_attempts` 次；仓库直接附带可编辑的 `config.json` 占位模板；新增 `record_wait_seconds` 配置键
- **v1.0.0**：首个公开发布版本

## 实现里踩过的坑（对后来者有用）

- **Gopeed 桌面版的 `/forward` 通道有编码 bug**：Dart 侧逐字节解码请求 JSON，中文路径会被读成乱码，任务会下到一个乱码文件夹。本项目对所有请求负载做纯 ASCII 转义（`\uXXXX`）绕开它。
- **Gopeed 的 TCP API 默认不开**，桌面版只暴露 `~/Library/Application Support/com.gopeed.gopeed/gopeed_host.sock`，通过它 `/forward` 转发标准 REST API。
- **Gopeed 解析磁力有约 60 秒的内部超时**：引擎刚启动、DHT 未预热时解析容易失败，重试即可。
- **macOS 给每个文件打「由哪个 App 创建」的 provenance 标记**：A 进程创建的文件，launchd 拉起的 B 进程可能被拒访（`Operation not permitted`）。所以桌面记录文件必须由服务进程自己创建和维护，其他进程（包括终端里的人工操作）不要替它写这个文件。
- 服务的 `resolve` 请求里不要带 `selectFiles`（那是 create 的参数）；真正选文件要靠 `rid` + create 的 `opts.selectFiles`。

## 故障排查

- 服务日志：`~/Library/Logs/mikan-gopeed.log`（launchd 模式）
- 手动跑一轮看细节：`python3 mikan_gopeed.py --once --verbose`
- 只想看看会做什么：加 `--dry-run`
- 记录文档没更新？先确认 Gopeed 任务列表里任务已完成，再看日志有没有 `RECORDED`
- 桌面记录文件可以用 TextEdit 等常见编辑器直接编辑，服务会在你的修改基础上插入新记录（已实测）。若某天日志出现 `record write failed`，删掉桌面文件即可，服务会从备份自动重建，历史不会丢
- 单元测试：`python3 test_mikan_gopeed.py`

## 问题反馈与交流

遇到报错或行为异常：[提交 Issue](https://github.com/Xmh20070642/mikanani-gopeed-downloader/issues/new?template=bug_report.md)，按模板附上诊断信息（⚠️ 贴日志前抹掉 `token=` 后面的内容）。

安装配置疑问、用法讨论：[Discussions 交流区](https://github.com/Xmh20070642/mikanani-gopeed-downloader/discussions)。

> 不需要也不建议私下留邮箱联系——Issues 和 Discussions 会把回复通知到你自己的 GitHub 邮箱，同时讨论过程能帮到后来的人。

## License

[MIT](LICENSE)
