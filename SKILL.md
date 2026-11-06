---
name: plex-badge-refresh
description: 刷新 Plex 中 MetaTube 刮削条目的元数据，并把重新合成的角标海报设为封面。当用户说「刷新元数据」「重新刮削」「重刮一下」「封面没更新」「海报还是旧的」「把新合成海报设成封面」「角标不对」「-C -U -UC -LTX 的视频批量处理」并涉及 Plex / MetaTube 时使用。也用于核对某批视频的封面角标是否与文件名后缀一致。
agent_created: true
---

# Plex MetaTube 角标 poster 刷新

一条命令做完两件事：**强制重新刮削元数据** → **把这次新合成的角标海报钉成封面**。

## 何时使用

| 用户说的话 | 跑什么 |
|---|---|
| 「把这几部的元数据刷新一下，封面也换成新的」 | `all --apply` |
| 「新增了一批片子，处理一下」 | `all --apply --recent 3` |
| 「看看哪些能处理 / 会改哪些」 | `scan` |
| 「只要换封面，别重刮」 | `covers --apply` |
| 「检查一下角标对不对」 | `verify` |

## 快速上手

```powershell
# 路径按本机实际 skill 目录调整
python C:\Users\SAYA\.workbuddy\skills\plex-badge-refresh\scripts\plex_badge_refresh.py all --apply
```

默认读 `config.json` 里的 `list_file`，只处理**文件名带角标后缀**的条目（`-C` `-U` `-UC` `-LTX` …）。
`--apply` 是安全闸：不加就只打印计划。

## 命令

全部命令共用同一套目标选择：

| 参数 | 含义 |
|---|---|
| `--list FILE` | 视频路径清单（每行一个，UTF-8），筛出带角标的 |
| `--item KEY` | 直接指定 Plex ratingKey，可重复 |
| `--recent DAYS` | 按 `addedAt` 取最近 N 天入库的条目 |
| `--limit N` | 只处理前 N 条（先小样本验证） |
| `--tag-filter UC` | 只处理某一类后缀 |
| `--all-files` | 连带没有角标后缀的文件一起（一般不需要） |

| 子命令 | 作用 |
|---|---|
| `scan` | 列出目标 + 当前封面角标 + Plex 标题，不写任何东西 |
| `refresh --apply` | 批量 `PUT .../refresh?force=1&async=1`，跟着插件日志等完成 |
| `covers --apply` | 按日志里的最新提交，把对应海报设为封面 |
| `verify` | 逐条核对「选中的海报 == 最新提交」且角标与后缀匹配，有问题返回 1 |
| `all --apply` | refresh → covers → verify 全流程 |

先小样本试跑，确认无误再全量：

```powershell
$S = "C:\Users\SAYA\.workbuddy\skills\plex-badge-refresh\scripts\plex_badge_refresh.py"
python $S all --apply --limit 2      # 先 2 条
python $S all --apply                # 再全量
```

## 配置

`~/.workbuddy/skills/plex-badge-refresh/config.json`（已按当前 NAS 填好）：

```json
{
  "plex_url": "http://192.168.100.221:32400",
  "prefs_path": "//192.168.100.221/Plex/.../Preferences.xml",
  "plugin_log": "//192.168.100.221/Plex/.../PMS Plugin Logs/com.plexapp.agents.metatube.log",
  "agent": "com.plexapp.agents.metatube",
  "sections": [],
  "share_root": "/volume1",
  "list_file": "C:\\Users\\SAYA\\Desktop\\最近一个月视频列表.txt"
}
```

- `sections` 留空会自动发现全部媒体库。
- `prefs_path` / `plugin_log` 留空则按 `plex_url` 的主机名推导 `//host/Plex/Library/Application Support/Plex Media Server/...`。
- Token 取 `--token`→`$PLEX_TOKEN`→`Preferences.xml` 的 `PlexOnlineToken`。
- 换机器/换 NAS 只改这个文件，或 `--config other.json`。

## 为什么这样做（踩过的坑，别改回去）

1. **`POST /refresh` 必须带 `?force=1&async=1`**。裸 PUT 返回 200，但什么都没发生。
2. **刷新成功不会改 `updatedAt`**，所以不能用 `updatedAt` 判断完成。唯一可靠的完成信号是插件日志里的 `Poster submitted: <图片URL>`。
3. **Plex 的海报 ratingKey 就是 `sha1(图片URL)`**，而**同一 URL 重复提交会被静默跳过**。所以「最新决定」的那张海报可能排在候选列表中间，取「最后一张」会把封面切回旧图。
4. **删除/覆盖旧海报在插件里全都无效**（PMS 1.40 实测）：`posters.clear()` 没有这个方法、`validate_keys()` 空转、`del posters[key]` 只清内存、`DELETE /metadata/{k}/poster` 返回 404、`metadata.thumb` 不可写。唯一能做的是 `PUT /library/metadata/{k}/poster?url=<ratingKey>` 切换选中项。
5. 插件在 update 期间拿不到新海报的 ratingKey（保存发生在 update 返回之后），所以「钉封面」必须由外部脚本在刷新完成后做，这是本 skill 存在的理由。

## 前置条件

- 安装的是**打过日志补丁的 MetaTube.bundle**（`update()` 里会 `Log.Debug('Poster submitted for ...')`）。上游原版没有这行，脚本会提示 `no "Poster submitted" line`。
- `Logs/PMS Plugin Logs/com.plexapp.agents.metatube.log` 可读（SMB 即可，插件日志级别要是 Debug 或更高）。
- 清单里的路径是 NAS 上的原始路径（`\\192.168.100.221\Plex\...`），脚本会按 `share_root` 换成 Plex 视角的 `/volume1/...`；换不出来时退回按文件名匹配。

## 结果状态怎么读

| state | 含义 | 处理 |
|---|---|---|
| `ok` | 已经是这张封面 | 无需动作 |
| `switched` | 刚切过去 | 成功 |
| `poster-missing` | 日志里的 URL 在候选里找不到 | 该条目没刮完，重跑 `refresh` |
| `switch-failed` | PUT 返回了但没选中 | 检查 Plex 权限/版本 |
| `no-log-line` | 插件没提交过海报 | 刷新失败，看日志 |
| `BADGE-OFF` | 封面选对了，但角标和文件名后缀不符 | 同名多版本共用一个 id 的已知坑，检查文件 |
| `NOT-SELECTED` | 选中项不是最新提交 | 重跑 `covers --apply` |

## 排查

```bash
# 看最近插件日志
tail -40 "//192.168.100.221/Plex/Library/Application Support/Plex Media Server/Logs/PMS Plugin Logs/com.plexapp.agents.metatube.log"
```

- 整批 `no update`：插件没加载。查日志有没有 `SyntaxError` / `Exception starting plug-in`（Plex 的 RestrictedPython 禁止以下划线开头的标识符）。
- 单条 `no update`：外部图源挂了（JAV321 偶发 500 EOF），日志里是 `Failed to download poster`，单独重试该条即可。
- 文件名后缀没被识别：角标必须用 `-` 或 `_` 隔开，`FSDSS983C.mp4` 不算，要改成 `FSDSS-983-C.mp4`。

## 改代码之后

```powershell
python C:\Users\SAYA\.workbuddy\skills\plex-badge-refresh\scripts\selftest.py
```

离线自测（不碰 Plex、不碰 NAS），覆盖角标正则、路径换算、日志解析（含轮转优先级）、刷新跟踪的超时与完成判定。20 项全绿再上真机。
