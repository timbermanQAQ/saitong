# Qoder 开发记录 · 赛题通

这份文档记录 **Qoder（AI 编码助手）在本项目里具体做了什么**：需求怎么拆、代码怎么写、坑怎么踩、bug 怎么定位与验证。
按阶段组织，每段固定三块：**做了什么 / 关键决策与发现的问题 / 验证结果**。

只写事实与可复现命令；凡是没实跑的（浏览器交互、创空间部署）都明确标注，不写"大幅提升""完美通过"这类评价。
时间线（`git log`）：

| 时间（+08:00） | 事件 |
| --- | --- |
| 2026-11-15 15:37 | `80e17ec` 语雀 59 篇文档导出（使用者完成，作为原始语料） |
| 2026-11-15 15:58 | `4743777` M1：设计文档 + seekdb 知识库 |
| 2026-11-15 16:23 | `4efa572` M2+M3：PowerContext 队伍档案 + 问答/下一步 |
| 2026-11-15 17:21 | `4214e20` M4–M8：前端、Dockerfile、验收脚本与文档 |
| 2026-11-15 18:05 | `76f08c6` 本轮：远端验收 URL 编码修复、`fetch_docs.py`、创空间部署目录、本开发记录 |
| 2026-11-15 18:29 | `fe13ace` 语料不再随仓库分发、抽取式答案清洗表格噪声、`site/` 展示站（含使用者执行的 `git rm -r --cached data/docs`） |
| 之后 | 本轮（未提交）：`site/` 用 Qoder Sites 公开发布、本记录与 DESIGN 的状态回填 |

运行环境：`/home/claude/mmvenv/bin/python`（Python 3.13.18）、Linux x86_64、无 docker daemon、无浏览器；
依赖只允许用已装好的包，不联网安装。**PowerContext 的请求/响应字段是先真机调用确认的，不是猜的**（使用者的硬要求）。

---

## 阶段 1 · M0/M1：设计 + seekdb 知识库

### 做了什么

- 先把使用者的口述需求拆成 M0–M8 八个里程碑写进 `docs/DESIGN.md`，每个里程碑带"验收"列，
  后续每完成一个就回填状态（现在只有 `deploy/modelscope/` 与浏览器点验是 🔶/待办）。
- 写代码之前先跑了一批**环境实测**，结论直接决定设计（DESIGN §14）：
  默认 embedding `all-MiniLM-L6-v2`（ONNX 86MB，384 维）本机已缓存、离线可用、首次加载约 3.4s；
  嵌入式 `Client(path=...)` 传 `database=` 会报 `Unknown database`；同一 seekdb 目录可被两个进程同时读写；
  `where_document` 支持 `$contains`/`$or`，`get()` 必须带 `ids`/`where`/`where_document` 之一；
  `hybrid_search` 返回多包一层 `{"ids": [[...]], ...}`，`distances` 是 RRF 融合分。
- 实现 `app/kb.py`：front matter 解析 → 赛道推断 → 按 H2/H3 切块（块首加"文档标题 / 章节"面包屑）→
  内容 sha 增量 upsert → `hybrid_search` 检索 → 每次检索写一行 JSONL；`scripts/ingest.py` CLI、
  `scripts/prep_embedding.py` 构建期预热；`app/config.py`（env → 类型化配置单例）、`app/audit.py`（三通道 JSONL）。
- 写 `tests/test_kb.py`（51 例，全部离线，用合成语料不依赖 `data/docs`），然后真机导入 `data/docs` 跑示例检索。

### 关键决策与发现的问题

| 问题 | 处理 |
| --- | --- |
| **语雀卡片占位块抢第一名**：卡片转出来的块正文几乎是空壳，却排到 Top-1 | 清洗阶段丢弃卡片占位与空块；`test_clean_body_drops_yuque_card_placeholder`、`test_card_only_doc_yields_no_chunks` 两条单测锁住 |
| 代码围栏里的 `#` 注释被当成标题，切出碎块 | `split_sections()` 只认围栏外的 H2/H3（`test_split_sections_ignores_heading_like_lines_inside_code_fence`） |
| 中文没有天然分词，全文腿（`$contains`）不知道该搜什么 | jieba + 领域词典构造检索词，并且**最长匹配优先**（「提交材料」不再被切成「提交」「材料」） |
| 默认向量模型是英文模型，中文区分度差：相似 0.335 vs 无关 0.306 | 排序主要靠全文腿 + 服务端 RRF，向量腿只补召回；这条限制原文写进 README §7，换模型只需改 `default_embedding_function()` |
| 嵌入式 client 重复加载会重复吃 3.4s 与内存 | 按 path 做模块级缓存 → 引出"只能单进程"的限制（`--workers` 必须为 1） |
| 增量导入依赖 id/哈希稳定 | 块 id = `文件名#序号`，内容 sha 不变则 `unchanged`；`FallbackEmbeddingFunction` 也必须是确定性的 |

### 验证结果

```
$ /home/claude/mmvenv/bin/python -m pytest tests/test_kb.py -q      # 51 passed
$ /home/claude/mmvenv/bin/python scripts/ingest.py                   # 59 篇 → 429 块
seekdb 141 / PowerContext 96 / AMD 64 / 通用 63 / Dify 14 / 阶跃星辰 13 / Qoder 12 / MiniMax 9 / 高德地图 9 / 魔搭社区 7 / 原力灵机 1
$ /home/claude/mmvenv/bin/python scripts/ingest.py --query "MiniMax 题要提交什么" --tracks MiniMax
```

示例检索命中 MiniMax「三、提交材料」块（`mode=hybrid`、`elapsed_ms=142.6`、`n_hits=5`），每条带 `source_url`；
`logs/seekdb_queries.jsonl` 里能对出 `ft_terms=["minimax","提交"]`、`where={"track":{"$in":["MiniMax"]}}`。

---

## 阶段 2 · M2/M3：PowerContext 队伍档案 + 问答（顺带 M4 下一步、M5 日志）

### 做了什么

- **先真机打一遍 PowerContext 再写客户端**：起 `powercontext server run`，逐个调
  `/v1/scopes`、`/v1/memory/remember`、`/v1/atomic-memory/list`、`/v1/atomic-memory/lifecycle`，
  把真实字段名（`scope_id`、`kind`、`content`、`state`、分页游标）抄进 `app/team.py`。
- `app/team.py`：scope 幂等创建（`idempotency_key=team:<队名>`）、四类记忆 `track-choice|progress|blocker|note`
  （正文是 JSON）、原子记忆分页读取、`lifecycle` 把旧 progress 置 `forgotten`、子进程管理与"就绪后不重复拉起"。
- `app/llm.py` + `POST /api/ask`：配了 `LLM_BASE_URL/LLM_API_KEY/LLM_MODEL` 就走 OpenAI 兼容接口并要求带 `[n]` 引用；
  **缺任何一个或调用失败自动落抽取式答案**，并在 `warning` 里说明原因。
- `app/api.py`：`/api/tracks`、队伍 CRUD、`/api/next-steps`（从文档抽「要交什么」+ 倒计时）、`/api/stats`、`/api/logs/{channel}`。

### 关键决策与发现的问题

| 问题 | 处理 |
| --- | --- |
| 同一赛题反复改状态后，档案里堆着历史值，读回来分不清哪条有效 | 写新 progress 前把旧的 `lifecycle → forgotten`；API 断言"该赛题只剩 1 条有效记录 + `replaced` 计数" |
| **next-steps 赛道过滤串味**：MiniMax 赛题的待办引用了 AMD 的文档 | 待办检索按"该赛题所属赛道"限定 `where`，加回归 `test_next_steps_todos_stay_within_their_own_track`（先失败、修完转绿） |
| 问「赛题一要做什么」时该用哪个赛道？ | 优先级：问题里点名的赛道 > 请求显式 `tracks` > 队伍档案；两条单测分别覆盖"泛问题用档案"与"点名覆盖档案" |
| PowerContext 挂了不能让知识库一起 500 | 档案类接口 `503`、`/healthz` 暴露 `powercontext.ok`，检索/问答照常；`PowerContextError` 必须显式抛到 API 层，不能被吞成空结果 |
| 每次档案调用要可核对 | 三通道 JSONL（`seekdb`/`powercontext`/`llm`）只记摘要不记全文，日志器自身异常不外抛 |
| 队名可以含中文，路径参数直接拼 URL 有编码风险 | 当时只在 TestClient 下跑，没暴露；本轮补修（见阶段 4） |

### 验证结果

```
$ /home/claude/mmvenv/bin/python -m pytest tests/test_team.py tests/test_api.py -q   # 17 + 10 passed
```

`test_team.py` 用 `httpx.MockTransport` 断言报文形状，最后一条**起真实子进程**：建队 → 勾题 → 改状态两次 →
杀掉重启 → `scope_id` 不变、progress/blocker 读回一致（没装 `powercontext` 的机器上该条 skip 而非假通过）。

---

## 阶段 3 · M6 前端 / M7 部署 / M8 验收

### 做了什么

- `app/static/`（无构建的原生 JS 单页）：队伍条 + 四个页签（检索 / 问答 / 赛题进度 / 下一步）+ `/stats` 页；
  选题候选来自 `/api/tracks` 的 `topics`（后端把赛道下的文档标题一并下发）。
- `Dockerfile` + `scripts/start.sh`：`python:3.13-slim-bookworm`，`ENV HOME=/app` 让 ONNX 缓存落在镜像里，
  构建期预热 embedding 并烘入索引（`--build-arg SAITONG_BUILD_INGEST=0` 可跳过），入口脚本建目录 → 增量导入 → 前台 uvicorn。
- `scripts/verify.py`：24 项端到端验收（页面、健康、赛道、检索、建档、选题、进度、卡点、复读、问答、待办、统计、三份日志），
  `--restart` 多一项；`samples/VERIFY.md` 记录实测输出与人工点验清单。

### 关键决策与发现的问题

| 问题 | 处理 |
| --- | --- |
| 前端把后端返回的文本直接 `innerHTML` 会 XSS | 所有插值过 `esc()`，链接过 `safeUrl()`（只允许 `^https?://`，挡住 `javascript:`） |
| 「保存选题」把全部赛道重推一遍，PowerContext 调用爆量 | 前端 diff「勾选 vs 已存」，只对变化的赛道发 `PUT` |
| 列表页显示的状态与后端聚合口径不一致 | 前端 `statusOf()` 复用后端 `_STATUS_RANK` 的同一套优先级 |
| `stats.html` 里 `const $` 与后面 `function $(id)` 重复声明 → 整段脚本 SyntaxError | 删掉重复声明，只留顶部一个 |
| `styles.css` 的 `:root` 里写了无效属性 `radius: 10px` | 删除（CSS 不报错但会让人以为设了圆角） |
| 企业赛道（MiniMax/Qoder/AMD 等）没有「赛题一：…」篇目，选题列表为空 | 前端仍渲染该赛道并提示「该赛道没有独立的『赛题…』文档，请自建赛题名」，输入框允许手填 |
| 验收断言本身写错：`TOPIC in data["tracks"]`（`tracks` 装的是赛道名） | 改成 `tracks==[赛道] and result.topics==[赛题清单]` |
| 弱向量把无关片段顶进结果 | 混合检索丢弃"一个查询词都不含"的纯向量命中（`dropped_no_overlap`），过滤词表用比检索词更宽的 `overlap_terms()`——否则只写「提交方式」的文档会被误杀（这条过滤第一版误伤过两条问答单测，改宽之后转绿） |

### 验证结果

```
$ /home/claude/mmvenv/bin/python -m pytest tests -q            # 78 passed in 34.16s
$ /home/claude/mmvenv/bin/python scripts/verify.py             # 24/24
$ /home/claude/mmvenv/bin/python scripts/verify.py --restart   # 25/25
```

部署侧：使用者在 Docker 里实测——镜像构建成功，容器启动后 `/api/health` 为 429 块 / `hybrid` / PowerContext 在线。
开发机没有浏览器，页面交互**未**自动验证，`samples/VERIFY.md` §4 给人工清单。

---

## 阶段 4 · 本轮：远端验收修复 + 语料可脚本同步 + 创空间部署

### 4.1 修 bug：`--base-url` 模式下中文队名抛 `UnicodeEncodeError`

- 现象：`verify.py --base-url http://127.0.0.1:7861` 在 `check_team_flow` 里请求 `/api/teams/样例队/tracks` 直接抛错。
- 定位：`urllib` 最终按 ascii 编码 URL；`RemoteClient` 把队名原样拼进路径。进程内模式用的是 TestClient（httpx），
  它会自动做 IRI 编码，**所以本地 24 项全绿也照不出来**——这是典型的"验收脚本自身只在目标环境失效"的 bug。
- 修法：编码放在传输层单点。新增 `encode_path()`（`quote(path, safe="/=?&%#")`，保留结构性字符、不二次转义 `%`），
  `RemoteClient.request()` / `get_text()` 统一走它，各检查函数不动。
- 测试：`tests/test_verify.py` 5 例——中文队名转义后可 `unquote` 还原、ASCII 路径原样不动、
  `%` 不被二次转义、未编码 URL 确实不可 ascii 编码（bug 成因），
  以及用假 `urlopen` 跑一遍 `RemoteClient` 四类请求，断言发出的 URL 全 ASCII 且 `?top_k=3` 未被破坏。
- 顺着这条路还发现**远端模式的判据本身是错的**：`main()` 原先直接读本机 `logs/*.jsonl` 和 `.runtime/qa_log.jsonl`
  算"运行前基线"，`--base-url` 下等于拿容器的 total 比本机的历史行数。改成 `read_baseline(client)`，
  基线全部走被验收实例的 HTTP 接口（`/api/health` / `/api/stats` / `/api/logs/{channel}?limit=1`），
  两种模式同一口径；附带好处是脚本不再依赖 `app` 包，可以只拷 `verify.py` 一个文件到目标机器上跑。

### 4.2 顺手抓到验收脚本自己的两个假信号

- `--restart` **原来是假阳性**：它 `import` 后 new 了一个新的 `PowerContextService`，新对象的 `stop()` 因
  `process is None` 空转，`start()` 又因为服务已在跑直接返回 True——什么都没重启还差点留下孤儿子进程。
  改成重启应用自己持有的 `app.state.ctx.service`（`restart()` = stop + 等端口释放 + `start(force=True)`），
  并让 `Client` 抽象出 `restart_service()`：远端实例明确记 **SKIP** 而不是 PASS。
- `next-steps` 一项**依赖上一次运行的残留**：上一轮把赛题标成 `done` 后第二次跑就失败。
  现在先压回 `doing` 再断言，脚本可重复执行。

### 4.3 `scripts/fetch_docs.py`：把语料变成"可脚本再生"

- 抓取方式（公开页面，无需 Token）：首页 HTML 里 `window.appData = JSON.parse(decodeURIComponent("..."))` 拿到 `book.toc`
  （扁平列表：`uuid/parent_uuid/type/title/url/doc_id/level`）；每篇 `<doc_url>/markdown?plain=true&linebreak=false&anchor=false` 取正文；
  只取第一个顶层节点之下、下一个 level 0 之前的 DOC，空正文跳过，请求间隔 0.4s。
- 两个必须实测才知道的细节：
  1. 顶层节点自身也是一篇有正文的文档，现有快照的 `00_ix20uoszxhifnp4b.md` 就是它 —— 不选它整体序号会错位；
  2. **不能"清洗"返回内容**：初版把字面 `\n` 还原成换行 + 剥 `<card>` 标签，实测改坏两篇
     （PowerContext 赛题四的 `` `DOCUMENT_ID\nPAGE_TEXT` ``、AMD 修复方案里的 `tr '\0' '\n'`，都是代码里的字面量）。
     实测 `/markdown` 返回的已是真换行、正文里没有 `<card>`，目录性空页返回 0 字节 → 整个 `normalize_body()` 删掉。
- 输出与 `data/docs` 对齐：文件名 `{序号:02d}_{slug}.md`、`title/path/source_url` front matter、
  `data/manifest.json` 保持数组 + 1 空格缩进 + `file/title/path/source_url/chars` 字段。
  `tests/test_fetch_docs.py` 里直接拿真实的 `data/docs/00_*.md` 反推比对，格式跑偏会立刻红。
- 重抓实测（写进 `.runtime/fetch-check/`，**没有覆盖仓库快照**）：TOC 共 71 个节点，根节点之下选出 63 篇 DOC，
  其中 60 篇有正文（跳过「加赛部分 / 初赛部分 / OceanBase seekdb基础事件」三个空目录页）。与现有 59 篇比：
  slug 同一批、front matter 逐字节一致、正文去掉尾部空行后 **59/59 一致**、序号 00–52 相同；
  唯一多出来的是上游新增的「分享」目录页（24 字，插到 53，`分享/*` 六个文件序号整体 +1）。
  也就是说旧快照少这一篇，是因为导出那天那一页还没有正文。

### 4.4 `deploy/modelscope/`：魔搭创空间 Docker 部署

- `Dockerfile`：与主 Dockerfile 同构（同样的 apt 依赖、`ENV HOME=/app`、预热 embedding、烘入索引、`CMD scripts/start.sh`、
  端口 7860），差别是代码不在构建上下文里而是 `git clone --depth 1 --branch "$REPO_REF" "$REPO_URL" /app`
  （`ARG REPO_URL` 可覆盖，换 fork 只改 build-arg）；仓库里没有 `data/docs` 时构建期改跑 `scripts/fetch_docs.py`。
- `README.md`：头部 YAML 声明 `sdk: docker` / `app_port: 7860`，另给 `ms_deploy.json`（`sdk.type=docker`、`port=7860`
  + 非敏感环境变量）。LLM 三个变量**只写占位符**，Key 由使用者填在创空间「环境变量 / Secrets」；
  写清"缺任一变量 → `mode=extractive` 抽取式回答，演示照常可用"，以及队伍档案随实例回收丢失（索引在镜像里所以检索不受影响）。
- 状态：**未在创空间实跑**（开发机无 docker daemon）。这条以及字段名以平台文档为准的提醒，都写在该目录 README §5。

### 4.5 本轮验证结果

```
$ /home/claude/mmvenv/bin/python -m pytest tests -q
89 passed in 36.22s          # test_kb 51 / test_team 17 / test_api 10 / test_fetch_docs 6 / test_verify 5

$ /home/claude/mmvenv/bin/python scripts/verify.py --restart
25/25 项通过，耗时 10.9s       # 重启项打印的是新 pid(4016) 与读回的 tracks=['seekdb']

$ /home/claude/mmvenv/bin/python scripts/fetch_docs.py --out-dir .runtime/fetch-check/docs --manifest .runtime/fetch-check/manifest.json
写入 60 篇（跳过 3 篇）
```

文档同步：README（目录、89 例、部署与语料再生、`--base-url` 复跑）、`docs/DESIGN.md`（v0.5：模块表加语料同步/创空间、
§11 测试策略补两个新测试文件、§12 M7 回填为已实测并新增 M9、§16 本轮实测记录）、`samples/VERIFY.md`
（实测输出换成最新一次运行、重启项语义澄清、未覆盖清单更新）。

---

## 阶段 5 · 本轮：语料不再随仓库分发 + 抽取式答案清洗 + 展示站发布

### 做了什么

- **A1 语料改为按需抓取**：`data/docs/` 进 `.gitignore`（`data/manifest.json` 保留），抓取入口收敛成
  `fetch_docs.ensure_docs()` 一个函数，`ingest.py::ensure_corpus()`、`start.sh` 的 `--if-missing`、
  两个 Dockerfile 构建期的 `if ! ls data/docs/*.md` 都调它；`README` / `samples/VERIFY.md` 的块数改成
  「约 430 块，随语雀文档更新略有变化」并写明数据来源（哪个知识库、哪个顶层节点）与抓取方式。
- **A2 抽取式答案清洗**：`app/llm.py` 新增 `clean_markdown_noise()`（`extractive_answer` 取文处调用），
  丢表格分隔行、把数据行摊成「表头：值；表头：值」、去掉未闭合的 `**`；新增 `tests/test_llm_extractive.py` 5 例。
- **B 展示站**：`site/`（纯 HTML/CSS，无构建）首页含简介、解决的问题、核心功能（配 `site/assets/` 五张截图）、
  技术架构（seekdb 与 PowerContext 各自负责什么 + 数据流）、用 Qoder 开发的四个阶段与踩坑清单、验证方式、
  仓库/在线体验链接；「使用效果」区块写「收集中」并留出访问统计与反馈位，**不放编造数据**。
  本机静态服务已验：`/`、`/styles.css`、五张 PNG 全部 200（首页 17KB），HTML 标签闭合与锚点用解析器核过。
  **公开发布（Qoder Sites）已跑通**（使用者放行 `qoderclicn sites` 命令并明确授权公开发布之后）：
  `get_context` 显示项目还没绑定站点 → `createNewSite=true`；`prepare_site`（`webDirectory=site`、
  `displayName=赛题通`、`slug=subdomain=saitong`、`spa=false`）第一次返回 `descriptorStatus=pending`，
  `get_publish_status` 报 `canPublish=true` 后用**同一 actionId + 同一份入参**重试，才写出描述符文件 `.赛题通.qoder.site`；
  平台给新站点的是「前缀 + 随机后缀」，所以 `saitong` 并没有被占用、只是拿不到裸前缀，
  最终地址是 <https://saitong-8ibzejymr71.qoder.zone>。
  `update_access_policy`（`mode=public` + `confirmPublic=true`，用完读取返回的 `accessRef` 并配一个固定 `requestId`）
  先落公开（返回 `auth_integration_status=not_required`），再 `publish_site`，
  `get_publish_status` 为 `published=true`、`operation.state=succeeded`、`committed=true`、
  `readiness=publication_completed`，`get_site` 里 `access_mode=public`。
  未登录 `curl` 复验：首页 `200 / 74294 B`（0 次重定向）、`styles.css` `200 / 7339 B`（与本地同尺寸）、
  五张 PNG `200`（425537 / 437622 / 340159 / 670206 / 275205 B）。
  线上首页比本地 17252 B 大是平台在末尾注入了 `<script data-qoder-watermark="v1">`，
  我们自己的七个 `<section id=…>` 一个没少。

### 关键决策与发现的问题

| 问题 | 处理 |
| --- | --- |
| 三处各写一份「要不要抓」的判断，日后必然漂 | 判据只留 `has_docs()`（目录里有任意 `.md`），返回码/报错文案留给调用方：ingest 返回 2 并打印三条办法，start.sh 只 `fail` 告警不让容器进崩溃循环，Dockerfile 构建期失败就 `exit 1`（构建失败比镜像里带个空库好发现） |
| 快照不入库后，那条「与仓库快照逐字节比对」的单测在干净克隆上会 KeyError | 改成缺快照时 `pytest.skip` 并写明原因；同时补 6 例纯 monkeypatch 的抓取/短路/报错测试，这些不依赖语料 |
| 「约 430」是估的还是测的 | 现测：空目录跑 `ingest.py --docs .tmp/nodocs/docs` → 自动抓 60 篇 → **430 块**（旧快照 59 篇是 429），README/VERIFY 里的数字来自这次运行 |
| 抽取式答案里的残缺表格行 | 分隔行按「每格都匹配 `^:?-{2,}:?$`」判定；表头只当标签不重复进正文；`**` 只看单双（成对是原文强调，保留）。清洗只作用于兜底答案，LLM 路径仍喂原始 Markdown |
| `.gitignore` 不会自动取消已跟踪文件 | `git rm -r --cached data/docs` 属于会改索引的动作，留给使用者执行（本文档 §遗留 1）；文件仍在磁盘上，本机行为不变 |
| 站点的「使用效果」没有真实数据 | 只放「收集中」区块与两个待接入位（访问量 / 反馈），不写假数字；截图是 `site/assets/` 里现成的五张真实页面图 |
| 新站点没法预先验域名 | `check_subdomain` 的入参要求已有 `siteId`，对全新站点直接 `sites_invalid_request`；只能在 `prepare_site` 之后看返回的 `host`。且平台给的是「前缀+随机后缀」，`saitong` 没被占用也拿不到裸前缀 |
| `prepare_site` 返回 `descriptorStatus=pending` 时描述符还没落盘 | 按返回里的提示**保留同一 `actionId` 与完全相同的入参**重试一次，才写出 `.赛题通.qoder.site`；换 `actionId` 会另起一份草稿 |

### 验证结果

```
$ /home/claude/mmvenv/bin/python -m pytest tests -q
99 passed in 34.95s          # test_kb 51 / test_team 17 / test_api 10 / test_fetch_docs 11 / test_llm_extractive 5 / test_verify 5

$ /home/claude/mmvenv/bin/python scripts/verify.py --restart
25/25 项通过，耗时 9.6s

$ /home/claude/mmvenv/bin/python scripts/ingest.py --docs .tmp/nodocs/docs --data-dir .tmp/nodocs/run   # 空语料
写入 60 篇到 .tmp/nodocs/docs（跳过 3 篇） → 文档 60 篇 → 块 430 个

$ /home/claude/mmvenv/bin/python scripts/fetch_docs.py --if-missing
[fetch_docs] /home/claude/saitong/data/docs 已有文档，跳过抓取

$ /home/claude/mmvenv/bin/python scripts/fetch_docs.py --if-missing --book-url https://ob.invalid/nope --out-dir .tmp/nodocs/docs …
[fetch_docs] 抓取失败：URLError: …（退出码 1，ingest 侧返回 2 并打印三条办法）
```

真实检索答案复跑：`extractive_answer("提交材料有哪些", …)` 输出里 `:---` 计数 0、未闭合 `**` 行数 0。

公开发布后的未登录复验（本地没做任何登录态，`curl` 直接打线上域名）：

```
$ curl -s -o /dev/null -w "%{http_code} %{size_download} %{content_type}\n" https://saitong-8ibzejymr71.qoder.zone/
200 74294 text/html; charset=utf-8
$ curl -s -o /dev/null -w "%{http_code} %{size_download} %{content_type}\n" https://saitong-8ibzejymr71.qoder.zone/styles.css
200 7339 text/css; charset=utf-8
$ curl … https://saitong-8ibzejymr71.qoder.zone/assets/01_search.png
200 425537 image/png          # 另外四张 02_ask / 03_progress / 04_next / 05_stats 分别 437622 / 340159 / 670206 / 275205 B
```

`get_publish_status` 收尾状态：`published=true`、`canPublish=false`（已发布，没有待发的草稿）、
`operation.state=succeeded`、`committed=true`、`readiness=publication_completed`、`access_mode=public`。

---

## 遗留事项（照实列）

1. `--base-url` 的**整份远端复跑**没在这台机器上做过（本机没能起服务）：修的是 `RemoteClient` 的 URL 编码与基线来源，
   两者都有单测（假 `urlopen` 断言发出的 URL），但 24 项对容器实例跑一遍仍需在容器侧执行一次。
2. `deploy/modelscope/` 未在创空间实跑；`ms_deploy.json` 字段名待与平台文档核对。
3. 浏览器内的前端交互未自动化验证（无浏览器环境），只有 API 契约与静态资源可达的单测/验收。
4. `fetch_docs.py` 每次全量重抓（63 篇逐篇请求 + 0.4s 间隔，实测约 2 分钟），没做增量。
   本轮起 `data/docs/` 不再随仓库分发，所以**干净克隆 + 无外网 = 空知识库**（报错会说清楚，但确实需要网络或手工放快照）；
   本机磁盘上那份 59 篇快照还在（`git rm --cached` 不动工作区，现在只靠 `.gitignore` 挡住），现抓的是 60 篇 / 430 块。
5. 中文向量模型的根因还在：换模型是改 `app/kb.py` 的一行，但需要重新导入并复跑验收。
6. 队伍数据无鉴权，队名即隔离边界，不适合放敏感信息。
7. 主 `Dockerfile` 里新增的「构建期抓语雀」和 `start.sh` 的 `--if-missing` 前置检查，**没在有 docker daemon 的机器上复跑**
   （开发机没有 daemon）；它调用的两条 Python 路径本身在本机实测过（阶段 5 的验证块）。
8. 展示站页面上的「使用效果」仍是「收集中」占位：平台侧有 `get_analytics`，但站点是纯静态、
   没有把访问量/反馈接进去；线上域名也是平台给的随机后缀（`saitong-8ibzejymr71`），换正式域名要再动一次访问与命名配置。
9. 工作区 `.tmp/` 里留着本轮 publish 用的两个入参文件与一份线上首页副本（`sites_prepare.json`、
   `sites_access.json`、`live_index.html`）；`.tmp` 未跟踪，随手删掉不影响仓库。

上一轮列的两条已闭环：**「不再分发」差的 git 一步**（`git rm -r --cached data/docs` 由使用者执行并随 `fe13ace` 提交，
`git ls-files data/docs` 现为 0）和**展示站的公开发布**（见阶段 5 的 B 与验证块：站点「赛题通」已公开，
地址 <https://saitong-8ibzejymr71.qoder.zone>，未登录 `curl` 已复验）。
一次性目录 `.tmp/nodocs/`（约 192MB）已由使用者删除。
