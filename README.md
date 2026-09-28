# WeChat Portal

本机微信数据的只读入口，供 Claude Code 和 Codex 调用。读取群聊与私聊消息，以及聊天中的图片、视频、文件、合并转发记录和飞书文档链接；每条命令输出一段 JSON，理解与判断由调用它的 AI 会话完成。

不发消息、不改数据、不依赖 Dashboard，也不需要常驻服务或定时任务。

## 能力

| 内容 | 读取方式 | 说明 |
|---|---|---|
| 会话列表 | `sessions` | 默认过滤公众号入口 |
| 消息历史 | `history` | 群聊与私聊；文本、引用回复、链接卡片、文件、系统消息；支持按类型筛选和分页 |
| 合并转发 | `history` 自动展开 | 还原被转发记录的发送人、时间和正文，最多嵌套两层 |
| 链接 | `history`、`links` | 抽取并分类消息里的网址，标出飞书文档类型 |
| 图片 | `image` | 高清、普通、缩略图三档缓存依次尝试 |
| 视频 | `video` | 元数据、均匀抽帧，可选复制原片；不转写语音 |
| 文件 | `file` | 定位本机文件并提取正文：PDF、docx、doc、pptx、ppt、xlsx、xls、csv、md、txt 等；zip 只列目录 |
| PDF 页面 | `file --pages` | 扫描件、整页图片、幻灯片型 PDF 自动渲染前 3 页 |
| 飞书文档 | `feishu` | 经用户本人的 lark-cli 登录只读获取；文档转 Markdown，表格按子表导出 CSV |
| 多会话材料 | `prepare` / `cleanup` | 生成私有临时目录，按需附带图片、视频、文件和飞书文档 |

媒体和文件只能取到本机已下载的部分。未在微信里点开过的视频、文件，以及已被清理的缓存，返回 `MEDIA_NOT_CACHED`。

## 安装

要求 macOS、Python 3.9 及以上，且 wx-cli 的密钥库中已有当前账号的有效密钥（安装器不提取、不复制、不修改密钥）。

```sh
python3 install.py --account-dir '~/Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files/<账号目录>'
wechat-portal status
```

首次安装须指定账号目录（直接包含 `db_storage` 的那一层）。安装内容：

- `~/.local/share/wechat-portal/config.json`：账号绑定，权限 0600
- `~/.local/bin/wechat-portal`：启动器，以隔离模式（`python -I`）运行本仓库的 `run.py`
- `~/.claude/skills/wechat-portal`、`~/.codex/skills/wechat-portal`：指向本仓库 `skill/` 的软链，两种 AI 共用同一份说明
- `~/.claude/commands/wechat-portal.md`、`~/.claude/commands/wx-image.md`

再次运行会保留账号绑定；已有文件内容不同时先备份到 `~/.local/share/wechat-portal/backups/` 再替换。`--agent claude` 或 `--agent codex` 只装一侧。

从旧版 wechat-work 升级时加 `--migrate-wechat-work`：沿用旧配置里的账号，把旧的 Skill 软链、命令、启动器和运行目录备份后移除。

## 命令

```sh
wechat-portal sessions --limit 100
wechat-portal history --chat '<chat_id>' --since 2026-09-24 --until 2026-09-26 --limit 200 [--type file]
wechat-portal links   --chat '<chat_id>' --feishu-only
wechat-portal media   --chat '<chat_id>' --kind image|video|file
wechat-portal image   --ref '<media.ref>' --output /私有目录/img
wechat-portal video   --ref '<media.ref>' --output /私有目录/vid [--frames 6] [--copy]
wechat-portal file    --ref '<media.ref>' --output /私有目录/doc [--pages N] [--copy] [--no-text]
wechat-portal feishu  --url '<飞书链接>' --output /私有目录/fs
wechat-portal prepare --chat A --chat B --images 4 --files 3 --videos 1 --feishu 2
wechat-portal cleanup --job '<job>'
```

输出前缀所在目录必须属于当前用户且权限为 0700；输出文件一律 0600，不覆盖已有文件。完整用法和解读规则见 `skill/SKILL.md`。

## 结构

```
run.py                    启动入口
wechat_portal/
  cli.py                  命令行
  portal.py               会话、消息、引用校验、各类读取的编排
  backend.py              调用 wx-cli；只读访问 wx-cli 的解密缓存（消息库、hardlink 索引）
  messages.py             消息正文、链接抽取与分类、合并转发展开
  media.py                图片、视频、文件的精确定位与提取
  documents.py            文档转文本、PDF 页面渲染
  feishu.py               经 lark-cli 读取飞书文档与表格
  jobs.py                 多会话材料与清理
  common.py               错误码、脱敏、私有文件读写、子进程
skill/SKILL.md            Claude 与 Codex 共用的 Skill
commands/                 Claude 命令模板
vendor/wx-cli/            内置 wx-cli 0.7.4（MIT，见 LICENSE 与 SHA256SUMS）
tests/                    单元测试，全部为合成数据
install.py                安装与迁移
```

### 定位原理

- 图片：消息里的 md5，外加消息库 `packed_info_data` 中的资源 md5，对应 `msg/attach/<md5(会话)>/<月份>/Img/<md5>[_h|_t].dat`，由 wx-cli 解密。
- 视频：同样的 md5 集合，经 hardlink 索引 `video_hardlink_info_v4` 或文件名，对应 `msg/video/<月份>/<md5>.mp4`；`_raw.mp4` 为原片。视频文件未加密。
- 文件：消息里的文件 md5 经 `file_hardlink_info_v4` 对应 `msg/file/<月份>/<文件名>`；索引滞后时按「消息月份 + 文件名 + 大小」精确匹配。同 md5 的多个副本内容相同，优先取与原文件名一致的那份。
- 同一条消息匹配到内容不同的多个文件时停止并返回 `MEDIA_SOURCE_AMBIGUOUS`，不猜测。

所有媒体读取都先用引用重新查询那条消息，校验账号、会话、消息 ID、排序号、时间和类型完全一致，再去找文件。

## 依赖

| 工具 | 用途 | 缺失时 |
|---|---|---|
| wx-cli（内置） | 消息查询、图片解密、缓存刷新 | 无法使用 |
| sips（系统自带） | 图片尺寸 | 图片不可用 |
| ffprobe / ffmpeg | 视频元数据、抽帧 | 只返回源文件路径和封面 |
| pdftotext / pdfinfo / pdftoppm（poppler） | PDF 正文、页数、页面渲染 | 改用 markitdown，且不能渲染页面 |
| markitdown | docx、pptx、xlsx、xls、csv 转 Markdown | docx 改用 textutil，其余用简易 XML 提取 |
| textutil（系统自带） | doc、docx、rtf | 改用 LibreOffice |
| soffice（LibreOffice） | textutil 读不了的 doc（常见于 WPS 生成）、ppt | doc 用近似文本扫描，ppt 不可读 |
| lark-cli | 飞书文档与表格 | 飞书链接只列出不读取 |

`wechat-portal status` 会逐项报告以上工具是否就绪。

## 与其他项目的关系

- **wx-cli**（`CCworks/wx-cli/`，上游 pandorafuture/wx-cli）：本仓库内置其 0.7.4 发布版二进制，不依赖其源码仓库或 `~/.local/bin/wx-cli`。密钥库和解密缓存仍是 wx-cli 自己的目录（`~/Library/Application Support/wx-cli/`、`~/Library/Caches/wx-cli/`）。
- **Wechat-Dashborad**：互不依赖。Dashboard 使用 npm 版 `wx`（0.3.0）和它的 daemon，本仓库不调用。
- **wechat-work**：本项目的前身，2026-09-26 作为 wx-cli fork `feat/standalone-work-reader` 分支中的 `tools/wechat-work` 编写（PR mjlens-spec/wx-cli#1）。2026-09-28 独立成本仓库并改名，在其基础上增加视频、文件、合并转发、链接和飞书文档读取。

## 验证

```sh
python3 -m unittest discover -s tests -v
```

57 项测试，使用合成数据，覆盖引用精确匹配与账号校验、类型错配拦截、字段脱敏、wx-cli 诊断输出不外泄、分页、图片清晰度回退、视频原片归并与歧义拦截、文件副本选择与索引滞后兜底、合并转发展开与实体声明拒绝、链接分类（含仿冒域名）、文档提取回退链、飞书错误码映射与非飞书网址不访问、材料准备的部分失败记录、清理边界，以及安装、迁移和备份。Python 3.9 与 3.13 均通过。

2026-09-28 本机实测记录见 `VERIFICATION.md`。真实聊天记录、配置和缓存不得提交到仓库。
