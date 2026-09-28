---
description: 按消息出处提取本机微信图片并结合上下文理解
argument-hint: 会话和图片时间
---

用户请求：$ARGUMENTS

使用 `~/.claude/skills/wechat-portal/SKILL.md` 的媒体分支。
先用 `~/.local/bin/wechat-portal media --kind image` 取得精确引用，再用 `image` 提取并实际打开图片。
