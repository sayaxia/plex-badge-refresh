# plex-badge-refresh

刷新 Plex 中 MetaTube 条目的元数据，并把这次重新合成的角标海报钉成封面。

它既是一个命令行工具，也是一个 [WorkBuddy](https://www.workbuddy.cn) skill —— 放进 `~/.workbuddy/skills/` 后，说一句「刷新元数据、把封面换成新合成的海报」就能触发。

## 它解决什么

给视频文件名加 `-C` / `-U` / `-UC` / `-LTX` 后缀、让 MetaTube 在刮削时合成角标，这套做法在 Plex 上有一个绕不开的问题：

**插件提交了新海报，封面却还是旧的。**

原因有三层，缺一层都解释不通：

1. Plex 把海报的 `ratingKey` 定为**图片 URL 的 `sha1`**。角标变了 → URL 变了 → Plex 认为这是**另一张**新海报，追加而不是替换。
2. 海报容器在 PMS 1.40 上是 **append-only**，并且所有清理路径都是空操作：`posters.clear()` 没有这个方法、`validate_keys()` 返回正常但一张不删、`del posters[key]` 只清内存、`DELETE /metadata/{k}/poster` 返回 404、`metadata.thumb` 不可写。
3. 封面被钉在**最早**那张。插件在 `update()` 期间又拿不到新海报的 `ratingKey`（它要等 update 返回后才生成），所以插件自己也没法把封面切过去。

结果就是「角标明明更新了，封面纹丝不动」。这个工具在刷新完成之后，从外部把选中项切到正确的那一张。

还有第二个坑：**Plex 会静默跳过重复 URL 的提交**。所以「最新一次判定」的那张海报可能排在候选列表**中间**，而不是最后。取「最后一张」会把封面切回旧图 —— 必须按插件日志里最新的 `Poster submitted:` URL 来定位。

## 前置条件

- **打过日志补丁的 MetaTube.bundle**：`update()` 里会写 `Log.Debug('Poster submitted: {url}')`。上游原版没有这行，本工具会直接告诉你缺数据源。
- 能读到 `Logs/PMS Plugin Logs/com.plexapp.agents.metatube.log`（SMB 即可）。
- PMS 的 `Preferences.xml` 可读（用于取 `PlexOnlineToken`），或直接给 `--token` / `$PLEX_TOKEN`。
- Python 3.8+，无第三方依赖。

## 安装

### 方式一：让 agent 装（推荐）

在 WorkBuddy 里新建对话，把下面这段**整段**发给它：

```text
帮我把 plex-badge-refresh 安装成 WorkBuddy skill。

仓库：https://github.com/sayaxia/plex-badge-refresh（public，直接 clone 即可）

步骤：
1. clone 到 ~/.workbuddy/skills/plex-badge-refresh
   Windows 上是 C:\Users\<用户名>\.workbuddy\skills\plex-badge-refresh
   如果目录已存在，先告诉我要覆盖还是保留，不要直接删。
2. 把 config.example.json 复制成 config.json（config.json 被 .gitignore 排除，只在本地存在）：
   Windows: copy config.example.json config.json
   macOS/Linux: cp config.example.json config.json
3. 按我给的信息填 config.json 的这几项，其余留空用默认推导：
   - plex_url     Plex 地址，例如 http://192.168.1.10:32400
   - prefs_path   Plex 的 Preferences.xml（SMB 路径或本地路径）
   - plugin_log   插件日志 .../Logs/PMS Plugin Logs/com.plexapp.agents.metatube.log
   - list_file    视频清单 txt 的绝对路径（可先留空，用 --item / --recent 也行）
   我还没给的话，先问我要，不要猜。
4. 验证：
   python scripts/selftest.py                         # 必须 20/20 通过
   python scripts/plex_badge_refresh.py scan --limit 3   # 必须能列出条目
5. 把 config.json 的最终内容（token 之类的敏感值可以打码）和上面两条命令的输出贴给我。

约束：
- 不要 git add / commit / push config.json，里面有内网地址。
- 不要改 scripts/ 下的代码，先按原样跑通。
- 装完提醒我：新装的 skill 要**新开一次对话**才会出现在可调用列表里。
```

装完就可以直接说「刷新元数据，把封面换成新合成的海报」。

### 方式二：手动装

```bash
git clone https://github.com/sayaxia/plex-badge-refresh.git \
          ~/.workbuddy/skills/plex-badge-refresh
cp ~/.workbuddy/skills/plex-badge-refresh/config.example.json \
   ~/.workbuddy/skills/plex-badge-refresh/config.json
# 编辑 config.json：至少填 plex_url，其余可留空自动推导
```

只想当命令行工具用的话，clone 到任意目录也一样，加 `--config` 指过去即可。

## 用法

```bash
S=~/.workbuddy/skills/plex-badge-refresh/scripts/plex_badge_refresh.py

python $S all --apply                 # 刷新元数据 → 钉封面 → 校验
python $S all --apply --recent 3      # 只处理最近 3 天入库的
python $S all --apply --limit 2       # 先跑 2 条看看
python $S scan                        # 只看会动哪些，不写
python $S refresh --apply             # 只重刮
python $S covers --apply              # 只换封面
python $S verify                      # 只核对，有问题返回 1
```

`--apply` 是安全闸：不加只打印计划。

目标选择（所有子命令通用）：

| 参数 | 含义 |
|---|---|
| `--list FILE` | 视频路径清单，每行一个；只处理带角标后缀的 |
| `--item KEY` | 直接给 Plex ratingKey，可重复 |
| `--recent DAYS` | 按 `addedAt` 取最近 N 天入库 |
| `--limit N` | 只处理前 N 条 |
| `--tag-filter UC` | 只处理某一类后缀 |
| `--all-files` | 连没有角标后缀的一起处理 |

## 配置

```json
{
  "plex_url": "http://<plex-host>:32400",
  "prefs_path": "//<plex-host>/Plex/Library/Application Support/Plex Media Server/Preferences.xml",
  "plugin_log": "//<plex-host>/Plex/Library/Application Support/Plex Media Server/Logs/PMS Plugin Logs/com.plexapp.agents.metatube.log",
  "agent": "com.plexapp.agents.metatube",
  "sections": [],
  "share_root": "/volume1",
  "list_file": "/path/to/videos.txt",
  "timeout": 90
}
```

- `prefs_path` / `plugin_log` 留空会按 `plex_url` 的主机名推导（群晖套件版布局）。
- `sections` 留空自动发现全部媒体库。
- `share_root` 用于把 UNC 路径（`\\host\Plex\Movies\x.mp4`）换算成 Plex 视角的 `/volume1/Plex/Movies/x.mp4`；换算不出来时退回按文件名匹配。
- 配置查找顺序：`--config` → `$PLEX_BADGE_CONFIG` → skill 目录的 `config.json` → 当前目录的 `.plex-badge.json`。

## 结果状态

| state | 含义 |
|---|---|
| `ok` | 已经是这张封面 |
| `switched` | 刚切过去 |
| `poster-missing` | 日志里的 URL 在候选里找不到 → 该条目没刮完 |
| `switch-failed` | PUT 返回了但没选中 |
| `no-log-line` | 插件没提交过海报 → 刷新失败 |
| `BADGE-OFF` | 封面选对了，但角标和文件名后缀不符（多版本条目共用一个 id 时会发生） |
| `NOT-SELECTED` | 选中项不是最新提交 → 重跑 `covers --apply` |

## 自测

```bash
python scripts/selftest.py
```

20 项离线用例，不碰 Plex、不碰 NAS：角标正则（含 `FSDSS983C` 不算角标的负例）、路径换算、日志解析（含轮转优先级）、刷新跟踪的完成与超时判定。

## 注意

- **刷新请求会排队**。PMS 串行处理插件更新，一条可能等几分钟才开始。等待期间每 30 秒报一次进度，超时就调大 `--timeout`。
- **角标必须用分隔符隔开**。`FSDSS983C.mp4` 不会被认为带 `C` 角标，要写成 `FSDSS-983-C.mp4`。
- **一个 movie 只有一张海报**。同一条目里混装 `X.mp4` 和 `X-UC.mp4` 时，角标只能表示其中一个版本 —— 这是 Plex 的数据模型，不是本工具的缺陷。

## 许可

MIT
