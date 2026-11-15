# 验收清单与实测记录

> 本文件记录 2026-11-15 在本机跑的验收结果。脚本 `scripts/verify.py` 每次都会打印实际输出，
> 期望值与判定逻辑写在脚本里，改代码后直接重跑即可，不必回来改这份文档的断言。

## 1. 怎么跑

```bash
# ① 全量单测（99 例，含引擎实例与 PowerContext 真实进程）
/home/claude/mmvenv/bin/python -m pytest tests -q

# ② 端到端验收：进程内起 app，复用 .runtime 已导入的知识库 + 自动拉起 PowerContext
/home/claude/mmvenv/bin/python scripts/verify.py

# ③ 额外验证 PowerContext 重启后档案仍在（+1 项，约 +2s；只有进程内模式能重启它）
/home/claude/mmvenv/bin/python scripts/verify.py --restart

# ④ 对已部署实例验收（容器/创空间上跑起来之后；--restart 在远端记 SKIP，不计入失败）
/home/claude/mmvenv/bin/python scripts/verify.py --base-url http://127.0.0.1:7860
```

验收脚本会写入一条队伍档案（队名 `验收队-勿删`，可用 `VERIFY_TEAM` 覆盖），可重复执行：
每一步先置回基线状态再断言，不依赖上一次运行的残留。
远端模式（`--base-url`）走 `urllib`，路径里的中文队名会先 percent-encoding——
以前没转义时 `/api/teams/验收队-勿删/tracks` 直接抛 `UnicodeEncodeError`，进程内的 TestClient 却不复现（见 DESIGN §16）。
运行前的计数基线同样问"被验收的那个实例"要（`/api/health`、`/api/stats`、`/api/logs/{channel}`），不看本机 `logs/`，
所以本机和远端两种模式的判据一致。

## 2. 验收项与期望

| # | 验收项 | 期望 |
|---|--------|------|
| 1 | 页面可达 | `/ui/` 返回单页，`/ui/app.js`、`/ui/styles.css`、`/stats` 均 200 且含关键字 |
| 2 | 健康检查 | `/api/health` 200，块数 > 0，`status` 为 `ok` |
| 3 | PowerContext 在线 | `powercontext.ok=true`（档案读写的前提） |
| 4 | 倒计时下发 | `days_left` 为整数，`deadline` 来自 `DEADLINE` 配置 |
| 5 | 赛题候选 | `/api/tracks` 每个赛道带 `topics`（前端勾选题目用的文档标题） |
| 6 | 检索限定赛道 | `POST /api/search` 查「提交材料」，命中全部属于指定赛道 |
| 7 | 噪声抑制 | 无意义查询（`zzz-不存在的词-qqq`）返回 0 条，而不是弱向量顶来的片段 |
| 8 | 建档 | `POST /api/teams` 返回 `scope_id` |
| 9 | 选题 | `PUT /tracks` 后 `tracks=[赛道]`、`result.topics` 与提交清单一致 |
| 10 | 进度 | `PUT /progress` 同赛题只保留一条有效记录（`replaced` 记旧值），状态生效 |
| 11 | 卡点 | `POST /blockers` 返回 `id`；`.../resolve` 后 `resolved=true` |
| 12 | 复读档案 | `GET /api/teams/{name}` 的 `tracks/topics/progress/blockers` 形状符合前端用法 |
| 13 | 问答 | `POST /api/ask` 字段齐全、有答案、来源带编号与片段原文 |
| 14 | 下一步 | `GET /next-steps` 只列未完成赛题，且能从文档抽出待办 |
| 15 | 已完成不出现在待办 | 标记 `done` 后该赛题从 `items` 消失 |
| 16 | 统计 | `/api/stats` 的 `ask_count` 相比运行前增加，队伍数 ≥ 1 |
| 17 | 三份日志 | `seekdb`/`powercontext` 有新增行；未配 LLM 时 `llm` 通道允许为空 |
| 18 | 重启持久化（`--restart`） | 重启**应用自己拉起的**那个 PowerContext 子进程后，档案仍可读且赛道不变；远端实例记 SKIP |

## 3. 实测记录（本机，2026-11-15）

```
$ /home/claude/mmvenv/bin/python -m pytest tests -q
99 passed in 34.95s     # test_kb 51 / test_team 17 / test_api 10 / test_fetch_docs 11 / test_llm_extractive 5 / test_verify 5

$ /home/claude/mmvenv/bin/python scripts/verify.py --restart
→ 进程内验收：DATA_DIR=.runtime 队伍=验收队-勿删
PASS  页面 GET /ui/ 返回单页            status=200 len=3093
PASS  页面 GET /ui/app.js               status=200
PASS  页面 GET /ui/styles.css           status=200
PASS  页面 GET /stats                   status=200
PASS  健康检查 /api/health              status=200 status字段=ok 块数=429 模式=hybrid
PASS  PowerContext 在线（队伍档案可用）  {"ok": true, "url": "http://127.0.0.1:8765", "spawned": true}
PASS  距提交天数已下发                  days_left=4 deadline=2026-11-19
PASS  赛道统计含赛题候选（前端勾选题目用） seekdb: 10 个赛题候选
PASS  检索「提交材料」限定赛道          status=200 mode=hybrid 命中=2 赛道=['seekdb']
PASS  无意义查询返回空而不是噪声        命中=0
PASS  建档 POST /api/teams              {"name": "验收队-勿删", "scope_id": "scp_761x…", "created": false}
PASS  选题 PUT /tracks（赛道 + 赛题清单） tracks=['seekdb'] topics=["赛题一：部署一个 seekdb", "赛题二：GitHub bug修复"]
PASS  进度 PUT /progress（状态 + 备注覆盖旧值） status=doing 该赛题记录数=1 replaced=1
PASS  卡点 POST /blockers               {"id": "art_09aee5…", "kind": "blocker", "state": "active", "resolved": false, …}
PASS  解决卡点 POST /blockers/{id}/resolve  resolved=True state=active
PASS  复读档案 GET /api/teams/{name}    {"tracks": ["seekdb"], "topics": {…}, "progress": ["done", "doing"], …}
PASS  问答 /api/ask 字段齐全且来源可核对  mode=extractive basis=question 来源=5 耗时=158.5ms
      答案节选：根据知识库检索结果整理（抽取式答案，共 5 条相关片段）：
      提示：未配置 LLM（LLM_BASE_URL / LLM_API_KEY / LLM_MODEL），答案为抽取式
PASS  下一步 GET /next-steps            items=[('赛题一：部署一个 seekd', 'doing', 6), ('赛题二：GitHub bug', 'doing', 6)]
      待办示例：{"text": "项目：seekdb", "doc_title": "赛题一：部署一个 seekdb", "track": "seekdb", "source_url": "https://oceanbase.yuque.com/…/lz6ixow11mar233g", "chunk_id": "05_lz6ixow11mar233g#000"}
PASS  倒计时随档案下发                  days_left=4 overdue=False
PASS  标记已完成后从待办里消失          剩余 items=1
PASS  统计 GET /api/stats               ask=10 search=14 teams=2 模式分布={'extractive': 10}
PASS  日志通道 seekdb 有新增可核对行      total=87 最近={"event": "hybrid_search", "query": "赛题一：部署一个 seekdb 提交材料", …}
PASS  日志通道 powercontext 有新增可核对行 total=1164 最近={"method": "POST", "path": "/v1/atomic-memory/list", …}
PASS  日志通道 llm（未配置 LLM，允许为空） total=0 path=/home/claude/saitong/logs/llm_calls.jsonl
PASS  PowerContext 重启后档案仍可读（持久化在 POWERCONTEXT_HOME） pid=6544 url=http://127.0.0.1:8765 tracks=['seekdb']

25/25 项通过，耗时 9.6s（日志目录 /home/claude/saitong/logs）
```

`created=false` 是因为该队已在上一轮建档（幂等），`复读档案` 里 `progress` 有两条是历史遗留的另一个赛题记录，
判据看的是形状而非条数。重启那行的 `pid` 是重启后的新进程号，档案是从磁盘（`POWERCONTEXT_HOME`）读回来的。

`块数=429` 是本机 `data/docs` 那份 59 篇旧快照的导入结果；语料改由脚本抓取后同一套代码得到
**60 篇 → 430 块**（见 §3.2）。所以验收不写死块数，只看它 > 0，实际数字随语雀文档更新变化。

未配置 LLM，所以答案走抽取式（`mode=extractive`），问答里 `warning` 字段会明说原因；
配上 `LLM_BASE_URL/LLM_API_KEY/LLM_MODEL` 后 `mode` 变 `llm`，`logs/llm_calls.jsonl` 也会开始有行。
抽取式答案已不再吐出 Markdown 表格分隔行/表头（`| :--- | :--- |` 这类），见 `tests/test_llm_extractive.py`。

### 3.1 语料抓取（`scripts/fetch_docs.py`）

```
$ /home/claude/mmvenv/bin/python scripts/fetch_docs.py --out-dir .runtime/fetch-check/docs --manifest .runtime/fetch-check/manifest.json
目录「2026年OceanBase数据库大赛」下有 63 篇文档，开始抓取（间隔 0.4s）
写入 60 篇到 .runtime/fetch-check/docs，清单 …（跳过 3 篇）
  未收录：加赛部分 / 初赛部分 / OceanBase seekdb基础事件 —— 文档没有正文
```

与仓库里那份 59 篇快照比对：同一批 slug，front matter 逐字节一致，正文去掉尾部空行后 59/59 一致，
序号 00–52 相同；多出的 1 篇是上游新增的「分享」目录页（24 字），它插到 53 后，`分享/*` 六个文件序号整体 +1。

### 3.2 空语料时的自动抓取（本轮改动，`data/docs` 已不随仓库分发）

在一个空的语料目录上跑 `scripts/ingest.py`（`--docs` 指向不存在的路径），它自己把抓取这段补齐了：

```
$ /home/claude/mmvenv/bin/python scripts/ingest.py --docs .tmp/nodocs/docs --data-dir .tmp/nodocs/run
[ingest] .tmp/nodocs/docs 里没有文档（语料快照不入库，按需抓取），尝试从语雀抓取…
目录「2026年OceanBase数据库大赛」下有 63 篇文档，开始抓取（间隔 0.4s）
写入 60 篇到 .tmp/nodocs/docs，清单 .tmp/nodocs/manifest.json（跳过 3 篇）
  未收录：加赛部分 / 初赛部分 / OceanBase seekdb基础事件 —— 文档没有正文
导入 .tmp/nodocs/docs → .tmp/nodocs/run/seekdb (collection=saitong_kb)
文档 60 篇 → 块 430 个（新增 430、更新 0、未变 0、删除 0，无正文文档 2 篇），耗时 45.6s
赛道分布：seekdb:141, PowerContext:96, 通用:64, AMD:64, Dify:14, 阶跃星辰:13, Qoder:12, MiniMax:9, 高德地图:9, 魔搭社区:7, 原力灵机:1
现存块数：430
```

即 **60 篇 → 430 块**，README / 本文档里的「约 430 块」就是这一次的结果；旧的 59 篇快照是 429 块，
差 1 块来自新增的「分享」目录页。抓取失败（例如容器没有外网）时不会静默留空库：

```
$ /home/claude/mmvenv/bin/python scripts/fetch_docs.py --if-missing --book-url https://ob.invalid/nope --out-dir .tmp/nodocs/docs --manifest .tmp/nodocs/manifest.json
[fetch_docs] .tmp/nodocs/docs 里没有文档，从语雀抓取：https://ob.invalid/nope
[fetch_docs] 抓取失败：URLError: <urlopen error Tunnel connection failed: 502 Bad Gateway> https://ob.invalid/nope/
（退出码 1；ingest.py 走同一判断，返回码 2 并打印挂卷 / --docs / 换机器抓 三条办法）
```

已有语料时 `--if-missing` 什么都不做：`[fetch_docs] /home/claude/saitong/data/docs 已有文档，跳过抓取`。

> 待办：`data/docs/` 已进 `.gitignore`，但仓库里那 59 篇还在索引中，需要执行者本机跑一次
> `git rm -r --cached data/docs`（文件留在磁盘上）再提交，才算真正不分发。

## 4. 前端人工点验清单

`verify.py` 只核对静态资源可达与 API 契约，浏览器里的交互还要点一遍（本机 `bash scripts/start.sh` 后打开 <http://127.0.0.1:7860/ui/>）：

1. 顶栏：健康条显示块数/检索模式/剩余天数/LLM 与 PowerContext 状态，`.runtime` 为空时应提示「离线全文检索」。
2. 建个队伍 → 顶部出现赛道 chip 为空、页面记住队名（localStorage，刷新后仍在）。
3. 「赛题进度」：展开 seekdb，勾两个赛题 → 保存选题 → 下方出现进度表；改状态/填备注/记卡点 → 保存 → 刷新页面仍在。
4. 「知识库检索」：搜「提交材料」，勾选赛道后结果里的赛道标签应只有该赛道；清空按钮复位。
5. 「问答」：勾「带上当前队伍」，提问后答案下方来源可点开、原文跳转是新标签页；未配 LLM 时提示「抽取式」。
6. 「下一步」：显示倒计时与待办；把赛题标成「已完成」后它不再出现在待办里。
7. `/stats`：提问/检索计数随操作增长，赛道条形图与三份日志尾部有内容。

## 5. 这份验收没覆盖的

- 浏览器内的 JS 执行：无浏览器环境，未做点验；上面的第 4 节是给使用时的手动清单。
- `verify.py --base-url` 的完整远端复跑：中文队名的 URL 构造与基线来源都有单测（`tests/test_verify.py`），
  但整份 25 项对容器实例跑一遍要在有服务的机器上执行（容器侧 `/api/health` 此前实测 hybrid / PowerContext 在线，
  块数约 430，随语雀文档更新略有变化）。
- `deploy/modelscope/`：创空间侧未实跑，`ms_deploy.json` 字段名以平台文档为准（见该目录 README §5）。
- 真实语料的时效：语料不随仓库分发，首次运行由脚本现抓（§3.2）；比赛期间上游改了文档，
  跑 `python scripts/fetch_docs.py` 重抓 + `POST /api/ingest`（或重跑 `scripts/ingest.py`）即可，块数会变。
- `scripts/start.sh` 与 Dockerfile 里那段「语料为空就先抓」的判断在有 daemon 的机器上复跑过才算闭环；
  本机没有 docker，只验证了它调用的 `fetch_docs.py --if-missing` / `ingest.py` 两条路径（§3.2）。
