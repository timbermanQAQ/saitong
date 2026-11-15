# 赛题通（SaiTong）设计文档

> 面向 2026 OceanBase 数据库大赛参赛队伍的资料检索 + 进度跟踪单页应用。
> 语雀赛题文档（约 60 篇，由 `scripts/fetch_docs.py` 按需抓取）进 seekdb 做混合检索，
> 队伍档案进 PowerContext 做长期记忆，问答与「下一步待办」都带上可核对的原文链接与调用日志。

- 版本：v0.6（M0–M6、M8、M9、M10 已实现并实测；M7 主镜像已由使用者在 Docker 实测，`deploy/modelscope/` 待创空间实跑；
  M11 `site/` 展示站已用 Qoder Sites 公开发布（<https://saitong-8ibzejymr71.qoder.zone>，未登录 curl 已验）；
  M9 在 v0.6 里把语料改成「不随仓库分发、缺失时自动抓取」，见 §17）
- 运行环境：Python 3.11+（本仓库开发验证于 3.13），依赖见 `requirements.txt`
- 关键外部依赖：`pyseekdb==1.4.0.post1`（嵌入式）、`powercontext[cli,server]==1.5.0`（本地 HTTP）、FastAPI + uvicorn、httpx、jieba、openai（可选 LLM）
- 快速上手见 `README.md`；本文档只讲设计与取舍

---

## 1. 设计目标与非目标

**目标**

1. 一次提问跨全部语雀文档（约 60 篇）拿答案，答案下方给原文链接，评委可点开核对。
2. 队伍「选了哪些题、每题做到哪、卡在哪」有地方记，服务重启后还在。
3. 不配置任何 LLM Key 也能完整体验（抽取式答案兜底）。
4. 每次 seekdb / PowerContext 调用留一行 JSONL 日志，行为可复盘。
5. 单容器可部署到魔搭创空间（Docker），一个 `docker run` 起全栈。

**非目标**

- 不做用户体系、不做队伍鉴权（创空间公开演示场景，靠队名隔离数据）。
- 不做查询时的实时抓取语雀：语料是 `data/docs/*.md` 快照，只是**不随仓库分发**——目录空着时
  `ingest.py` / `start.sh` / 两个 Dockerfile 先跑 `scripts/fetch_docs.py` 抓一次，之后仍以快照为准。
- 不替队伍跑评测、不判分；只回答「文档里怎么写的」与「你还差什么」。

---

## 2. 模块划分

```
                         ┌──────────────────────────────────┐
  浏览器 (app/static) ──▶ │ app/api.py  FastAPI 路由/模型校验 │
                         └───┬───────────┬──────────┬────────┘
                             │           │          │
                 ┌───────────▼──┐  ┌─────▼──────┐  ┌▼─────────────┐
                 │ app/kb.py    │  │ app/team.py│  │ app/llm.py   │
                 │ 知识库 M1    │  │ 队伍档案 M2│  │ 生成答案 M3  │
                 └───────┬──────┘  └─────┬──────┘  └──────┬───────┘
                         │               │                │
              pyseekdb 嵌入式      httpx → PowerContext  OpenAI 兼容
              (DATA_DIR/seekdb)    HTTP 127.0.0.1:8765   HTTP
                         │               │                │
                         └──────┬────────┴────────┬───────┘
                                ▼                 ▼
                        app/audit.py        app/config.py
                        logs/*.jsonl        环境变量集中读取
```

| 模块 | 文件 | 职责 | 状态 |
| --- | --- | --- | --- |
| 配置 | `app/config.py` | 读环境变量（`DATA_DIR`/`PORT`/`LLM_*`/`KB_*`/`POWERCONTEXT_*`/截止日），进程内单例；可选加载 `.env` | M0 ✅ |
| 审计日志 | `app/audit.py` | 线程安全 JSONL 追加器，三个通道：`seekdb` / `powercontext` / `llm` | M0 ✅ |
| 知识库 | `app/kb.py` | 文档解析 → 赛道推断 → 标题切块 → 哈希增量 upsert → `hybrid_search` 检索 → 检索日志；向量模型不可用时降级纯全文 | M1 ✅ |
| 导入脚本 | `scripts/ingest.py` | CLI 入口，打印块数/赛道分布/耗时，`--rebuild` 全量重建、`--query` 顺带验证检索 | M1 ✅ |
| 模型预热 | `scripts/prep_embedding.py` | Docker 构建期把 all-MiniLM-L6-v2 烘进镜像缓存；`--check` 只探测 | M1 ✅ |
| 队伍档案 | `app/team.py` | PowerContext HTTP 客户端 + 子进程管理：scope 幂等创建、remember、atomic-memory/list、lifecycle forget、重启读回 | M2 ✅ |
| LLM 封装 | `app/llm.py` | 判断是否配置 LLM；带片段上下文的受约束问答；未配置/失败时抽取式答案 | M3 ✅ |
| API | `app/api.py` | 全部 `/api/*` 路由 + pydantic 校验 + 待办抽取 + `/stats` 聚合 | M3–M5 ✅ |
| 应用入口 | `app/main.py` | app 工厂、启动期拉起 PowerContext 子进程、静态页挂载、优雅退出（无 `app/static` 时 `/` 跳 `/docs`） | M6 ✅ |
| 前端 | `app/static/` | 单页：队伍 → 赛题状态 → 提问 → 下一步；`/stats` 页 | M6 ✅ |
| 部署 | `Dockerfile` / `scripts/start.sh` | 单容器：镜像内预热模型 + 烘入索引，入口脚本「语料为空先抓取」再增量导入，最后前台跑 uvicorn | M7 ✅ 已在 Docker 里实测（约 430 块 / hybrid / PowerContext 在线） |
| 创空间部署 | `deploy/modelscope/` | 魔搭 Docker 部署：构建期 `git clone` 仓库（`ARG REPO_URL`），无 `data/docs` 时改跑 `fetch_docs.py` | M7 🔶 文件已写，未在创空间实跑 |
| 语料同步 | `scripts/fetch_docs.py` | 从语雀公开页面抓 `data/docs` + `manifest.json`（`book.toc` 定位 + `/markdown` 取正文）；`ensure_docs()` / `--if-missing` 供 ingest、start.sh、Dockerfile 在语料缺失时调用 | M9 ✅ 实测可复现快照；空目录跑通 60 篇 → 430 块 |
| 验收 | `scripts/verify.py`、`samples/VERIFY.md` | 端到端跑一遍并留证据；`--base-url` 对已部署实例复跑 | M8 ✅ |

设计约束：

- `app/kb.py`、`app/team.py`、`app/llm.py` 互不依赖，只被 `app/api.py` 组合，便于单测。
- seekdb 嵌入式 client **每进程一个**（模块级按 path 缓存），避免重复加载模型与 libseekdb 实例；
  同理 PowerContext 子进程由应用持有，因此**只支持单进程**（`uvicorn --workers` 必须为 1）。
- 所有对外调用（seekdb / PowerContext / LLM）失败时降级不抛 500：检索失败返回明确错误 + 空结果，
  PowerContext 不可用时档案读写返回 `503` 并在 `/healthz` 暴露，LLM 不可用时落抽取式。

---

## 3. 目录结构

```
saitong/
├── app/
│   ├── __init__.py          # 包与版本号
│   ├── config.py            # Settings：env → 类型化配置
│   ├── audit.py             # JsonlLogger + seekdb/powercontext/llm 三个通道
│   ├── kb.py                # [M1] 解析/切块/赛道/导入/检索/向量降级
│   ├── team.py              # [M2] PowerContext 客户端 + 子进程 + 档案语义
│   ├── llm.py               # [M3] LLM 可用性判断 + 生成/抽取式答案
│   ├── api.py               # [M3-M5] 路由、pydantic 模型、待办抽取、stats 聚合
│   ├── main.py              # [M6] create_app()、生命周期、静态挂载
│   └── static/              # [M6] index.html / app.js / styles.css / stats.html
├── scripts/
│   ├── ingest.py            # [M1] python scripts/ingest.py [--rebuild|--dry-run|--query]
│   ├── prep_embedding.py    # [M1] 构建期预热 ONNX 模型（Dockerfile 调用）
│   ├── fetch_docs.py        # [M9] 抓语雀生成 data/docs 与 manifest.json；ensure_docs()/--if-missing 供缺失时调用
│   ├── start.sh             # [M7] 容器入口：建目录 →（语料为空先抓取）→ 增量导入 → 前台 uvicorn
│   └── verify.py            # [M8] 端到端验收（本地 / --base-url 远端 / --restart）
├── deploy/modelscope/       # [M7] 魔搭创空间 Docker 部署（Dockerfile + ms_deploy.json + README）
├── site/                    # [M11] 项目展示站：index.html + styles.css + assets/ 五张截图（纯静态，无构建）
├── .赛题通.qoder.site        # [M11] Qoder Sites 站点描述符：prepare_site 写出，指向已发布站点（勿手改）
├── tests/
│   ├── conftest.py          # 临时 DATA_DIR / 合成语料 fixture（不依赖网络）
│   ├── test_kb.py           # [M1] 51 例
│   ├── test_team.py         # [M2] 17 例（含真实子进程重启读回）
│   ├── test_api.py          # [M3-M5] 10 例（TestClient + 真知识库 + 假 PowerContext）
│   ├── test_verify.py       # [M8] 5 例：中文队名的 URL 构造 + 远端基线只走 HTTP
│   ├── test_llm_extractive.py  # [M10] 5 例：抽取式答案的 Markdown 清洗（表格分隔行、未闭合加粗）
│   └── test_fetch_docs.py   # [M9] 11 例：TOC 选取、路径拼接、快照格式对齐、语料缺失时的抓取与报错
├── docs/DESIGN.md           # 本文档
├── docs/QODER_DEV_LOG.md    # 开发记录：各阶段做了什么、踩了什么坑
├── samples/VERIFY.md        # [M8] 验收步骤与实测输出
├── data/
│   ├── docs/*.md            # 语雀文档快照（约 60 篇，带 YAML front matter）——不随仓库分发，缺失时自动抓取
│   └── manifest.json        # 导出清单（file/title/path/source_url/chars），保留在仓库里
├── logs/                    # 审计日志：seekdb_queries.jsonl / powercontext_calls.jsonl / llm_calls.jsonl
├── .runtime/                # DATA_DIR 默认值：seekdb 数据 + powercontext 数据（不入库）
├── requirements.txt
├── .env.example             # 只有占位符，无真实凭据
├── Dockerfile               # [M7] 单容器
├── .dockerignore            # [M7] .env / .runtime / logs 不进镜像
└── README.md                # 上手与部署
```

运行时产物（`.runtime/`、`logs/`、`__pycache__/`）全部 gitignore。

---

## 4. 数据设计

### 4.1 切块（chunking）

语雀导出数据实际结构（2026-11-15 那批 59 篇快照核对；脚本重抓得到 60 篇，结构一致）：
均有 front matter；正文标题只出现 `##`/`###`/`####`（188/224/8 处）；代码围栏内有 12 行以 `#` 开头的伪标题，**切块必须围栏感知**，否则会把 shell 注释当章节。

切块规则：

1. 剥离 front matter，取 `title` / `path` / `source_url`；正文做轻量清洗（去 `<font>`、`</font>`、`<br>`、图片语法与「此处为语雀卡片」占位行，保留表格、列表与附件链接——表格里有「提交产物」「验收项」这类高价值内容）。
2. 遇到 `##` 开新块并重置三级面包屑；遇到 `###` 开新块并保留其 `##` 父级；`####` 不切块（内容并入所属 `###`，避免碎片化）。首个标题之前的前言也算一块，面包屑为空。
3. 每块文本 = `【文档标题 > 二级标题 > 三级标题】\n正文`。把面包屑写进正文，全文检索「提交材料」这类章节词才召得回。
4. 超过 `KB_MAX_CHUNK_CHARS`（默认 1200 字符）的块按空行分段贪心打包成子块，`part_index` 递增，不重叠（重叠会让 Top-5 里塞满近重复片段）。
5. 正文为空的标题块丢弃；只有语雀卡片占位的文档（本次语料 2 篇：「第二讲：向量数据库与 RAG」「企业第八讲：MiniMax」）清洗后没有正文 → 不产生块，计入 `IngestReport.skipped_docs`。这类块曾在示例检索里抢走 MiniMax 提问的第一名，去掉后「三、提交材料」回到首位。

`chunk_id = "{文件主干名}#{序号:03d}"`，例如 `25_qlqnoryzrs7gtqbw#004`。序号在标题结构不变时稳定，配合 `content_sha` 实现「重跑只写变化的块」。

### 4.2 seekdb collection

嵌入式模式：`pyseekdb.Client(path=<DATA_DIR>/seekdb)`，使用默认库（**不要传 `database=`**：嵌入式下库不存在会直接 `Unknown database` 报错，已实测）。collection 名 `KB_COLLECTION`（默认 `saitong_kb`），默认 `DefaultEmbeddingFunction`（all-MiniLM-L6-v2 ONNX，384 维，本机已缓存，可离线）。

| 字段 | 位置 | 说明 |
| --- | --- | --- |
| `id` | 主键 | `{stem}#{seq:03d}` |
| `document` | 正文 | 面包屑 + 章节正文（也是全文索引列） |
| `doc_title` | metadata | 文档标题 |
| `path` | metadata | 语雀层级路径 |
| `source_url` | metadata | 原文链接，答案里直接给 |
| `track` | metadata | 推断赛道，检索过滤用 |
| `heading` / `heading_path` | metadata | 最近一级标题 / 面包屑 |
| `doc_file` | metadata | 文件主干名，按文档增量读写用 |
| `chunk_index` / `part_index` / `chars` | metadata | 定位与统计 |
| `content_sha` | metadata | `sha1(chunk_text)[:16]`，增量判定依据 |

### 4.3 赛道推断

按路径**从左到右**取第一个命中的分段；同一分段内按优先级取第一个命中的关键词（`AMD … seekdb …` 归 AMD，`… / PowerContext / 赛题十一：seekdb …` 归 PowerContext）：

| 关键词（小写匹配） | 赛道 |
| --- | --- |
| `minimax` | MiniMax |
| `qoder` | Qoder |
| `amd` | AMD |
| `dify` | Dify |
| `高德` | 高德地图 |
| `阶跃星辰` | 阶跃星辰 |
| `魔搭` | 魔搭社区 |
| `powercontext` | PowerContext |
| `原力灵机` | 原力灵机 |
| `seekdb` | seekdb |
| 无命中 | 通用 |

`通用` 覆盖主页、FAQ、赛题目录、加赛算分、测试/判题/仓库地址、GitHub 使用说明与通识课程。赛道常量 `ALL_TRACKS` 供 `/api/tracks` 与前端复选框使用。

### 4.4 队伍档案（PowerContext 1.5.0）

PowerContext 以本地 HTTP 服务运行：`powercontext server run --no-env-file --host 127.0.0.1 --port <POWERCONTEXT_URL 端口>`，
数据目录由环境变量 `POWERCONTEXT_HOME` 指定（`app/team.py` 把它设成 `POWERCONTEXT_DATA_DIR`，默认 `{DATA_DIR}/powercontext`），
SQLite 落盘 → 进程重启后按 `idempotency_key` 仍能读回同一 `scope_id`。

- `server run` 只认 `--host/--port/--uds/--env-file/--no-env-file/--role`，**没有 `--data-dir`**：
  持久化只能靠 `POWERCONTEXT_HOME`，所以单容器部署时把 `POWERCONTEXT_DATA_DIR` 放在 `DATA_DIR` 下面一起挂卷。
- 未设 `POWERCONTEXT_SERVER_AUTH_TOKEN` 且 `--no-env-file` 时服务不启用 Bearer 鉴权，直接匿名调用即可
  （真实子进程用例已验证：`tests/test_team.py::test_live_profile_survives_service_restart`，
  写档案 → 杀进程 → 重启 → `scope_id` 不变、progress/blocker 读回一致）。
- 上层只认 `TeamStore` 的 `ensure_team / profile / set_track_choices / set_progress / add_blocker /
  resolve_blocker / add_note / teams / unfinished`，不感知 PowerContext 报文格式。

| 语义 | 端点 | 关键字段 |
| --- | --- | --- |
| 队伍 scope | `POST /v1/scopes` | `idempotency_key = "team:<队名>"` → `scope_id` |
| 写记忆 | `POST /v1/memory/remember` | `kind ∈ track-choice / progress / blocker / note` |
| 读档案 | `POST /v1/atomic-memory/list` | 按 scope + kind 过滤 |
| 作废旧进度 | `POST /v1/atomic-memory/lifecycle` | `state = forgotten`（同赛题旧 progress） |

记录约定（`content` 为文本，`metadata` 为结构化字段）：

- `track-choice`：`{track, topics:[...], active:true}` —— 勾选赛题。
- `progress`：`{track, topic, status(todo\|doing\|blocked\|done), note}` —— 某赛题状态；同 `topic` 新记录写入后，旧 `progress` 置 `forgotten`。
- `blocker`：`{track, topic, text, resolved:false}` —— 卡点，可标解决而不删（保留历史）。
- `note`：自由备注。

### 4.5 截止时间

`DEADLINE = 2026-11-19T00:00:00+08:00`（即 2026-11-18 24:00 北京时间），常量在 `config.py`，剩余天数向上取整、逾期为负并在响应里给 `overdue: true`。

---

## 5. 接口设计

### 5.1 HTTP API

| 方法 | 路径 | 入参 | 出参（核心） |
| --- | --- | --- | --- |
| GET | `/` | — | 302 → `/ui/`（无静态页时 → `/docs`） |
| GET | `/ui/`、`/stats` | — | 单页 HTML / 统计页 HTML（`app/static`） |
| GET | `/healthz`、`/api/health` | — | `{status, seekdb:{ok,chunks,search_mode,embedding}, powercontext:{ok,url,spawned}, llm:{enabled,model}, days_left}` |
| GET | `/api/tracks` | — | `{tracks:[{name,chunks,docs,topics:[文档标题]}], chunks, docs, deadline, days_left}`，`topics` 供前端勾选题目 |
| POST | `/api/ingest` | `{rebuild?:bool}` | `IngestReport`（同 CLI 输出，供容器内热更新语料） |
| POST | `/api/teams` | `{name}` | `{name, scope_id, created}` |
| GET | `/api/teams` | — | `{teams:[{name, tracks, updated_at}]}` |
| GET | `/api/teams/{name}` | — | `TeamProfile.as_dict()`（`tracks/topics/progress/blockers/notes`）+ `next_steps_preview` |
| PUT | `/api/teams/{name}/tracks` | `{track, topics?}` | `{team, result, tracks}` |
| PUT | `/api/teams/{name}/progress` | `{track, topic, status, note?}` | `{team, track, topic, status, progress, replaced}` |
| POST | `/api/teams/{name}/blockers` | `{text, track?, topic?}` | `{blocker}` |
| POST | `/api/teams/{name}/blockers/{id}/resolve` | — | `{blocker}`（幂等；未知 id → 404） |
| POST | `/api/teams/{name}/notes` | `{text, track?, topic?}` | `{note}` |
| GET | `/api/teams/{name}/next-steps?top_k=3` | — | `{team, days_left, overdue, items:[{track, topic, status, queries, search_tracks, todos:[{text, source_url, chunk_id}]}], tracks_without_topics}` |
| POST | `/api/search` | `{q, tracks?:[...], top_k?:5}` | `{query, tracks, search_mode, hits:[SearchHit.to_dict()], elapsed_ms}` |
| POST | `/api/ask` | `{question, team?, tracks?:[...], top_k?:5}` | `{answer, mode:"llm"\|"extractive", model, track_basis, filter_tracks, used_tracks, sources:[{n,...}], warning, elapsed_ms}` |
| GET | `/api/stats?top_n=10` | — | `{ask_count, search_count, team_count, teams, modes, hot_questions, chunks, docs, track_chunks, days_left, llm_enabled}` |
| GET | `/api/logs/{seekdb\|powercontext\|llm}?limit=20` | — | 最近 N 行调用日志（`{channel,path,total,items}`；非法 channel → 422，limit 1–500） |

错误约定：入参为空/超长 → `422`；队伍不存在（PowerContext 无 scope）→ `404`；PowerContext 未就绪 → `503 {error:"powercontext_unavailable", hint}`；seekdb 检索异常 → 返回空 `hits` 且日志记 `error`，不打挂请求。

### 5.2 `/api/ask` 流程（M3）

1. 解析 `team` → 有档案则取已选赛道；`tracks` 显式传入优先。
2. 问题文本里点名赛道（`MiniMax`、`高德`…）时，该赛道胜出且不再受档案限制。
3. 无赛道可用 → 全库检索。`kb.search()` 取 Top-5（`KB_TOP_K`）。
4. `llm.enabled()` 为真 → 片段编号后喂给模型，system 限定「只用给定片段、中文、逐条标注 `[n]`、无依据答『文档里没写』」；失败或超时 → 落到抽取式。
5. 抽取式答案 = 首个命中片段全文 + 其余片段首句摘要，逐条带 `source_url`。
6. 无论哪条路径，`sources` 必返且与 `logs/seekdb_queries.jsonl` 的 `matched_ids` 一致。

### 5.3 `/api/next-steps` 流程（M4）

对档案里 `status != done` 的每个 `(track, topic)`：

1. 用固定模板拼检索词：`"<topic> 提交材料"`、`"<track> 提交方式"`、`"验收"`（`NEXT_STEP_QUERIES` 可配）。
2. 每个查询取 Top-3 片段，从正文行里抽「提交材料 / 运行说明 / 验证样例 / 代码差异 / 测试 / 记录」等条目作为待办（正则 + 标题命中；抽取函数在 M4 作为纯函数落地，便于单测）。
3. 输出 `days_left`、`overdue`，并把「档案里缺哪些赛道」也列成一条，提示未选题队伍。

### 5.4 内部接口（模块边界）

```python
# app/kb.py —— 纯函数（可单测、不碰引擎）
parse_markdown(text, *, file_name) -> ParsedDoc
clean_body(text) -> str                              # 去 <font>/<br>/图片/语雀卡片占位
infer_track(path, *, title="") -> str
split_sections(body) -> list[(breadcrumb, text)]     # 围栏感知：代码块里的 # 不当标题
pack_text(text, max_chars) -> list[str]              # 长章节按空行贪心打包，必要时硬切
build_chunks(doc, *, max_chars=1200) -> list[Chunk]
build_ft_terms(query) -> list[str]                   # ASCII 词 + 中文领域词典
embedding_state(settings) -> {"mode","model","detail",...}   # 进程内只探测一次

class KnowledgeBase:
    def __init__(self, *, settings=None, docs_dir=None, data_dir=None, collection=None, max_chunk_chars=None)
    @property collection -> pyseekdb Collection      # 懒建，client 每进程一个
    @property search_mode -> str                     # hybrid | fulltext_only
    def ingest(self, *, rebuild=False, progress=None) -> IngestReport
    def search(self, query, *, tracks=None, top_k=None, log=True) -> list[SearchHit]
    def chunks_for(docs=None) -> list[Chunk]         # 只切块不写库
    def track_counts(docs=None) -> dict[str, int]
    def count() / stats() / health()                 # 运维：块数 / 赛道分布 / 就绪状态

# scripts/ingest.py: main(argv) -> IngestReport（打印人读摘要）
```

```python
# app/team.py —— 上层只认这些，不感知 PowerContext 报文
PowerContextClient:  call / probe / ensure_scope / remember / list_records / forget / records_for
PowerContextService: command / start / wait_ready / restart / stop / client / base_url
TeamStore:           ensure_team / teams / profile / profile_or_none / set_track_choices
                     / set_progress / add_blocker / resolve_blocker / add_note / unfinished
TeamProfile:         tracks / topics_for / status_of / progress_for / open_blockers / latest_note
```

`SearchHit.to_dict()` 即 `/api/search` 的 `hits[]` 元素，API 层不做二次拼装。

---

## 6. 可核对日志

`app/audit.py` 提供 `log_event(channel, **fields)` / `read_tail(channel, limit)` / `count_lines(channel)`：`threading.Lock` 按通道加锁、`ensure_ascii=False`、自动补 `ts`（UTC ISO8601）、目录不可写时只往返回值里塞 `log_error` 而不抛异常（日志不能打挂请求）。目录 `LOG_DIR`（默认 `./logs`）。三个通道：

| 通道 | 文件 | 事件 |
| --- | --- | --- |
| `seekdb` | `seekdb_queries.jsonl` | `ingest`（每次导入）、`hybrid_search`（每次检索）、`embedding_probe`（每进程一次） |
| `powercontext` | `powercontext_calls.jsonl` | `powercontext`（每次 HTTP 调用）、`server_spawn`（拉起子进程） |
| `llm` | `llm_calls.jsonl` | `chat`（每次生成，含 `ok/error/elapsed_ms/cited` 与否） |

`logs/seekdb_queries.jsonl` —— 每次检索一行（`mode` 见 §7.1：`hybrid` / `knn_only` / `fulltext_only` / `fulltext_only_no_terms`）：

```json
{"ts":"2026-11-15T02:11:03.481+00:00","event":"hybrid_search","collection":"saitong_kb",
 "query":"MiniMax 题要提交什么","ft_terms":["minimax","提交"],"guard_terms":["minimax","提交"],
 "top_k":5,"where":{"track":{"$in":["MiniMax"]}},
 "where_document":{"$or":[{"$contains":"minimax"},{"$contains":"提交"}]},
 "tracks":["MiniMax"],"mode":"hybrid","elapsed_ms":142.6,"n_hits":5,
 "matched_ids":["25_qlqnoryzrs7gtqbw#004","..."],"scores":[0.0325,0.0161],
 "dropped_no_overlap":0,"error":null}
```

`logs/powercontext_calls.jsonl` —— 每次 HTTP 调用一行：

```json
{"ts":"...","event":"powercontext","method":"POST","path":"/v1/memory/remember",
 "request_summary":{"scope_id":"sc_1","kind":"progress","content_preview":"赛题二 done…"},
 "status_code":200,"elapsed_ms":38.2,"ok":true,"error":null,"scope_id":"sc_1","kind":"progress"}
```

规则：只记摘要不记全文（`content_preview` 截断 200 字符），**不写任何凭据**（LLM Key 只存在于环境变量，日志里不出现）。
`/stats` 页的累计问答次数、独立队伍数、热门问题来自 `QaLog`（`DATA_DIR/qa_log.jsonl`，`event ∈ {ask, search}`），
每次请求现算不落计数器；问题文本先 `normalize()`（去空白标点、转小写、截 80 字符）再聚合，**不写队名等个人信息**。

---

## 7. 检索实现要点

`collection.hybrid_search(query={...}, knn={...}, rank={"rrf":{}}, n_results=k, include=["documents","metadatas"])`：

- `query.where_document = {"$or": [{"$contains": term}, ...]}`，词来自 `build_ft_terms()`：ASCII 词（`minimax`/`pdf`/`http`）+ 大赛领域词典命中（提交材料、提交方式、算分、验收、截止、分支、commit、PR、fork、部署、仓库、测试、评分、要求…）。一个都没抽到就退回纯 `knn`（`mode="knn_only"`），不塞整句 —— 中文整句 `$contains` 会当短语匹配，召回直接归零。
- `where = {"track": {"$in": [...]}}` 同时挂到 `query` 与 `knn` 两侧（只挂一侧会被另一侧的结果污染）。
- `rank={"rrf": {"rank_window_size": 60, "rank_constant": 60}}`（默认值显式写出，便于调参）。
- 返回体是 `{"ids":[[...]], "documents":[[...]], ...}` 的嵌套一层结构，统一在 `_unwrap()` 里拉平；引擎顺序即 RRF 融合顺序，`score` 原样透传不换算。
- **纯向量命中要过词表检查**：`mode=hybrid` 且查询能抽到词时，片段（含首行面包屑里的标题与章节）必须至少含
  `overlap_terms(query)` 中的一个词才保留，被剔掉的条数记在日志 `dropped_no_overlap`。理由见 §7.1：默认英文 MiniLM
  在中文上「向量最近」近似随机，不给检查就会把无关块端成来源。`overlap_terms()` 故意比 `build_ft_terms()` 宽
  （同族短词不被最长匹配吞掉，如「提交材料」仍带「提交」「材料」），否则只写「提交方式」的文档会被误杀。
- **导入后必须 `refresh_index()`**，否则新块检索不到（增量写入与索引可见性解耦）。
- 嵌入式 seekdb 同目录跨进程读写在本机实测可用（A 进程持有连接时 B 进程可 count/upsert），因此 `scripts/ingest.py` 可与服务并行；但服务内仍走 `/api/ingest` 复用同一 client，避免两个 embedding 模型常驻内存。

### 7.1 向量后端与离线降级

`KB_EMBEDDING=auto|onnx|fallback`，判定在 `embedding_state()` 里做且**进程内只探测一次**（模型加载要 3s+）：

| 取值 | 行为 |
| --- | --- |
| `auto`（默认） | 试跑一次 `DefaultEmbeddingFunction(["赛题通自检"])`；成功 → `hybrid`，抛异常 → `fulltext_only`，异常摘要进日志与 `/healthz` |
| `onnx` | 强制向量化，模型不可用直接抛错（Docker 构建期自检用） |
| `fallback` | 不加载模型，直接用 `FallbackEmbeddingFunction`（sha256 伪向量，确定性，保证增量导入不受影响） |

降级后的检索语义（关键取舍）：

- 只走全文腿，`hybrid_search` 不再带 `knn`/`rank` 参数 —— 伪向量没有任何语义，拿它补位只会把无关块塞进 Top-5。
- 连全文词都没抽到时（`怎么办啊`）**直接返回空**，并在 `/api/ask` 里提示「换个说法、带上赛道名」，而不是给噪声。
- 日志 `mode` 相应变成 `fulltext_only` / `fulltext_only_no_terms`，`/healthz` 的 `embedding.model` 会显示 `saitong-hash-fallback`，前端据此提示当前是离线模式。
- 即使本机模型可用，也要保留这条降级路径：创空间的出网策略不可控，首次加载模型也可能被掐断。
- 默认模型 `all-MiniLM-L6-v2` 是英文模型，中文语义区分度有限（实测相似句 0.335 vs 无关句 0.306），
  排序实际主要靠全文腿 + RRF；向量腿只用来调整已被词表认可的片段的先后（§7 的 `dropped_no_overlap` 检查），
  换中文模型只需替换 `default_embedding_function()`，这条过滤对强模型同样只是保守项。

---

## 8. 配置

`app/config.py` 的 `Settings(env=..., root=...)` 直接把环境读成类型化字段；`get_settings()` 单例，测试用 `reset_settings(Settings(env={...}))` 注入。存在 `.env` 时加载（`os.environ.setdefault`，已导出的变量优先）。

| 变量 | 默认 | 用途 |
| --- | --- | --- |
| `PORT` | `7860` | FastAPI 端口（创空间约定） |
| `HOST` | `0.0.0.0` | 绑定地址 |
| `DATA_DIR` | `./.runtime` | seekdb / powercontext 持久化根目录 |
| `DOCS_DIR` | `./data/docs` | 语雀导出 Markdown |
| `LOG_DIR` | `./logs` | JSONL 审计日志 |
| `KB_COLLECTION` | `saitong_kb` | collection 名 |
| `KB_MAX_CHUNK_CHARS` | `1200` | 切块上限 |
| `KB_TOP_K` | `5` | 默认检索条数 |
| `KB_EMBEDDING` | `auto` | `auto` 探测 / `onnx` 强制向量 / `fallback` 纯全文（见 §7.1） |
| `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` | 空 | OpenAI 兼容接口；缺任一 → 抽取式答案 |
| `LLM_TIMEOUT` | `30` | 秒 |
| `POWERCONTEXT_URL` | `http://127.0.0.1:8765` | 档案服务地址（兼容旧名 `POWERCONTEXT_BASE_URL`）；指向回环才会自己拉起子进程 |
| `POWERCONTEXT_AUTOSTART` | `1` | `app/main.py` 是否拉起子进程 |
| `POWERCONTEXT_BIN` | 空 | 可执行文件路径；留空则 PATH → 当前解释器同目录 → `python -c` 兜底 |
| `POWERCONTEXT_READY_TIMEOUT` | `30` | 等待子进程就绪的秒数 |
| `POWERCONTEXT_DATA_DIR` | `{DATA_DIR}/powercontext` | 服务 SQLite 目录（传给子进程的 `POWERCONTEXT_HOME`） |
| `DEADLINE` | `2026-11-19T00:00:00+08:00` | 截止（北京时间 11-18 24:00） |

只有 `scripts/start.sh` 认的开关：`PYTHON`（解释器，默认 `python`）、`SKIP_INGEST=1`（跳过启动期导入）。

`.env.example` 只放占位符（`LLM_API_KEY=sk-xxxx`），真实凭据走部署平台环境变量；容器与文档里都强调「不要提交真实 Key」（与大赛「配置示例使用占位值」要求一致）。

---

## 9. 前端（M6）

单页无构建（纯 HTML + 原生 JS + 一份 CSS，FastAPI `StaticFiles` 托管，`/` → `index.html`）：

1. **队伍条**：下拉选已有队伍 / 输入新建；显示当前赛道 chip。
2. **知识库**：搜索框 + 赛道多选 → 结果卡片（标题、面包屑、片段高亮、原文跳转）。
3. **赛题进度**：赛道复选框 → 展开该赛道赛题，状态四选一（待办 / 进行中 / 阻塞 / 已完成）+ 备注；卡点列表可标记已解决。
4. **问答**：提问框（可带当前队伍上下文），答案下方「来源片段」可折叠，逐条 `source_url`；LLM 未启用时明确标注「抽取式答案」。
5. **下一步**：按未完成赛题列待办 + 剩余天数倒计时条（逾期红色）。
6. **`/stats`**：累计问答次数、独立队伍数、热门问题 Top10、知识库块数与赛道分布、最近检索日志尾部（可核对）。

无框架，请求集中在 `app.js` 的 `api()` 里，统一错误提示条；移动端（创空间预览常见）单列布局。

实现要点（`app/static/index.html` + `app.js` + `styles.css` + `stats.html`，共 4 个文件、无构建）：

- 赛题候选直接来自 `/api/tracks` 的 `topics`（= 该赛道下以「赛题」开头的文档标题），勾选题目与自定义赛题名
  合成一张勾选表；「保存选题」只对**变化的赛道**发 `PUT /tracks`，取消全部勾选即等于放弃该赛道（后端的 `removed` 语义）。
- 当前队名记在 `localStorage`（键 `saitong.team`），刷新后回到同一支队伍；档案本身在 PowerContext 里，跨设备可读回。
- 状态/备注/卡点共用一行 `PUT /progress`（+ 可选 `POST /blockers`），写完重新拉档案，页面不自己维护副本 —— 避免与
  后端「同赛题取离完成最远的状态」的口径打架。
- 所有插入 DOM 的文本走 `esc()`；`source_url` 来自语雀 front matter（外部数据），只放行 `http(s)` 才渲染成链接。
- `/stats` 是第二份 HTML（`stats.html`），复用同一套 CSS；日志表按通道取 `/api/logs/{channel}?limit=10` 的尾部倒序显示。

---

## 10. 部署（M7）

单容器：镜像里同时跑 FastAPI 与它自己拉起的 PowerContext 子进程，seekdb 用嵌入式模式（无独立服务）。

### 10.1 `Dockerfile`

```
FROM python:3.13-slim-bookworm
apt-get install ca-certificates libstdc++6 libgcc-s1 tzdata
ENV HOME=/app PORT=7860 HOST=0.0.0.0 DATA_DIR=/app/.runtime LOG_DIR=/app/logs
COPY requirements.txt → pip install --no-cache-dir
COPY app scripts tests data docs README.md .env.example
RUN if ! ls data/docs/*.md; then python scripts/fetch_docs.py; fi   # 语料没随仓库走就现抓
RUN python scripts/prep_embedding.py || echo "[warn] 未预热，运行期降级"      # ①
ARG SAITONG_BUILD_INGEST=1 → RUN python scripts/ingest.py --quiet             # ②
EXPOSE 7860  HEALTHCHECK /healthz  CMD ["bash","scripts/start.sh"]
```

- **基础镜像选 3.13-slim**：与开发验证用的解释器（3.13.18）一致，`pylibseekdb`/`onnxruntime` 都有现成 manylinux wheel，不需要编译工具链。
- **系统库**：`libseekdb.so` 与 onnxruntime 需要 `libstdc++6`/`libgcc-s1`（`libaio.so.1` 由 `pylibseekdb` 自带，实测无需 apt 包）；`ca-certificates` 给 LLM 出网；`tzdata` 给 `+08:00` 截止时间。
- **① 模型预热**：默认 ONNX 模型 86MB，落在 `$HOME/.cache/pyseekdb/onnx_models`（实测跟 `HOME` 走、**不认 `XDG_CACHE_HOME`**），所以把 `HOME` 设成 `/app`，缓存随镜像走、与运行用户无关。构建机没有外网时这一步失败也无所谓（`|| echo`），运行期 `KB_EMBEDDING=auto` 会再探一次，探不到就降级全文检索。
- **② 索引烘进镜像**：约 60 篇 → 约 430 块（`.runtime` 约 268MB，随语雀文档更新略有变化）。好处是容器一起来就能提问、`HEALTHCHECK` 不会因为首次导入的 45s 卡住；不想烘就 `--build-arg SAITONG_BUILD_INGEST=0`，交给 `start.sh` 现场导。
- **构建机需要能访问语雀**：`data/docs/` 已 gitignore，从干净仓库克隆出来的构建上下文里只有 `data/manifest.json`，
  构建期那句 `if ! ls data/docs/*.md` 就会去抓语雀（约 2 分钟）。不想让构建依赖网络，就把快照手动放进上下文——脚本检测到已有文档会直接跳过抓取。
- **不设 `VOLUME`**：避免匿名卷把烘进镜像的索引「盖」掉却不告警；持久化靠文档里明确写的 `-v`。
- **`.dockerignore` 排除 `.env`**：真实凭据只走部署平台的环境变量，绝不进镜像层。

### 10.2 `scripts/start.sh`

```
建目录（DATA_DIR / LOG_DIR / POWERCONTEXT_DATA_DIR）
→ 语料为空则 python scripts/fetch_docs.py --if-missing   # 抓不到只告警，不让容器崩循环
→ python scripts/ingest.py --quiet         # 增量：索引已存在时几秒；SKIP_INGEST=1 跳过
→ python scripts/prep_embedding.py --check # 只为把 hybrid/fulltext_only 写进启动日志，失败不拦停
→ exec python -m uvicorn app.main:app --host $HOST --port $PORT
```

- **PowerContext 不在这里起**：`app/main.py` 的 `build_context()` 已经负责 spawn（`POWERCONTEXT_AUTOSTART=1`，日志 `logs/powercontext_server.log`）、等待就绪、`lifespan` 退出时 `stop()`。两处都起会抢同一个端口。想连远端实例：`POWERCONTEXT_URL` 指向非回环地址，应用只连接不自启。
- `exec` 让 uvicorn 变成 1 号进程，SIGTERM 直达 → FastAPI 走 lifespan 关闭 → 子进程被回收，容器 `docker stop` 干净退出。
- **只能单进程**：seekdb 嵌入式 client 和 PowerContext 子进程都按进程持有，`--workers` 恒为 1。

### 10.3 持久化与部署位置

| 内容 | 路径 | 挂卷 |
| --- | --- | --- |
| seekdb 索引 + PowerContext SQLite | `DATA_DIR`（镜像内 `/app/.runtime`） | **必须**，否则实例回收后队伍档案与索引一起丢 |
| 审计日志 | `LOG_DIR`（`/app/logs`） | 建议，便于事后核对 |
| 语料快照 | `DOCS_DIR`（`/app/data/docs`，镜像里的烘索引那一份） | 不用；更新走 `scripts/fetch_docs.py` 重抓 + `POST /api/ingest`，离线环境可 `-v $PWD/data/docs:/app/data/docs` 覆盖 |

魔搭创空间：把 `DATA_DIR`/`LOG_DIR` 指到平台提供的可写持久目录（如 `/mnt/workspace/...`）再挂卷；
端口沿用平台约定的 `PORT`（默认 7860）。挂空卷首次启动会现场导入（约 45s），`/healthz` 期间不可用属正常。

---

## 11. 测试策略

- `tests/conftest.py`：把仓库根加入 `sys.path`；`isolated_settings` fixture 用 `Settings(env=...)` 把 `DATA_DIR`/`LOG_DIR`/`DOCS_DIR` 指到 `tmp_path`，autouse fixture 在每个用例后复位全局配置单例 —— 测试不会写进仓库的 `.runtime/` 与 `logs/`。
- `test_kb.py`（M1，51 例）：合成 5 篇语料（含代码块围栏内的伪标题、长表格、纯语雀卡片文档），**不依赖 `data/docs`**；覆盖 front matter 解析 / 21 条真实路径的赛道推断表 / 切块（H2-H3 面包屑、围栏内 `#` 不误切、超长分片、空块与卡片块丢弃、id 与 sha 稳定）/ 首次导入计数 / 二次导入 `unchanged == total`（增量）/ 追加章节只 `added=1`、改章节只 `updated=1`、回滚 → `deleted=1` / 删文件 → 该文档块清零、`--rebuild` 回到全量 / 赛道 `$in` 过滤（MiniMax 与 seekdb 结果不相交）/ 无有效词时降级 `knn_only` / 空查询记 `error` / 检索日志字段逐条核对（query、ft_terms、where、where_document、matched_ids、elapsed_ms）。另有三例专打向量降级：`FallbackEmbeddingFunction` 确定性（增量导入依赖它）、`KB_EMBEDDING=fallback` 下的建库 + 纯全文命中 + `fulltext_only_no_terms` 不给噪声、`auto` 探测进程内只跑一次并写 `embedding_probe` 日志。噪声过滤两例（§7）：`zzz-不存在的词-qqq` 返回空且 `dropped_no_overlap > 0`；`overlap_terms()` 比 `build_ft_terms()` 宽（「提交材料」仍带「提交」「材料」），保证只写「提交方式」的文档不被误杀。
- `test_team.py`（M2，17 例）：`httpx.MockTransport` 假 PowerContext 断言报文（幂等键 `team:<队名>`、`kind`、JSON 文本 content、lifecycle 置 `forgotten`、分页游标、每次调用落日志）；**最后一条用真实子进程**：`powercontext server run` 起来 → 建队 → 勾题 → 改状态两次 → 杀掉重启 → `scope_id` 不变、progress/blocker 读回一致（本机装了 `powercontext` 就会跑，没装则 skip）。
- `test_api.py`（M3–M5，10 例）：真知识库 + 假 PowerContext + 不配 LLM；覆盖 `/healthz`、`/api/tracks`、队伍 CRUD、`/api/search`、`/api/ask` 的赛道来源（问题点名 > 请求显式 > 队伍档案）、`/api/teams/{name}/next-steps` 的待办赛道一致性（回归：MiniMax 的待办不能引用 AMD 文档）、`/api/stats` 与两个日志通道。
- `test_verify.py`（M8，5 例）：按文件路径加载 `scripts/verify.py`，断言中文队名的路径经 `encode_path()` 后是纯 ASCII、
  能被 `unquote` 还原、已编码的 `%` 不被二次转义；再用假的 `urlopen` 替身跑一遍 `RemoteClient` 的四类请求
  （PUT tracks / POST resolve / GET 带 query / 取文本），断言发出的 URL 全是 ASCII 且 `?top_k=3` 没被破坏；
  最后一例断言 `read_baseline()` 的运行前计数全部来自被验收实例的 HTTP 响应（本机文件系统不参与）。
  起因见 §16：`--base-url` 远端验收时 urllib 对未转义的中文路径抛 `UnicodeEncodeError`。
- `test_fetch_docs.py`（M9，11 例）：合成 TOC 断言「只取第一个顶层节点之下、下一个顶层节点之前」（顶层文档自身也收），
  `build_path()` 按 `parent_uuid` 回溯；`render_doc()` 的输出与真实 `data/docs/00_*.md` 对比（文件名与 front matter 逐字节一致，
  正文允许尾部空行差异；**干净克隆里没有快照时这一例 skip**，不假装通过）；`fetch_toc()` 用 monkeypatch 喂页面 HTML，
  覆盖 toc 为对象、为二次编码的 JSON 字符串、页面结构变化时报错三种情况；后 5 例锁住语料缺失时的行为：
  `has_docs()` 判空、`ensure_docs()` 有语料时不发请求、抓取失败返回 False、成功时写出快照 + 清单、
  `ingest.ensure_corpus()` 的返回码 0/2（对应「可继续导入」与「给出可操作办法后退出」）。
- `test_llm_extractive.py`（M10，5 例）：抽取式答案的 Markdown 清洗——`| 材料 | 要求 |` + `| :--- | :--- |` 变成
  「材料：要求」这类可读文本且分隔行消失、表头只当标签不重复出正文、未闭合的 `**` 去掉而成对的保留、
  普通散文原样不动、`extractive_answer()` 整体输出里检索不到 `:---` 且引用编号 `[1]/[2]` 仍正确。
- 全部离线可跑：`/home/claude/mmvenv/bin/python -m pytest tests -q` → **99 passed**（约 35s；嵌入式引擎与一次真实 PowerContext 子进程重启占大头；`test_verify.py`/`test_fetch_docs.py`/`test_llm_extractive.py` 是纯函数测试，毫秒级）。端到端另有一条命令：`python scripts/verify.py`（24 项，`--restart` 25 项，见 §15）。真实语料导入是一次显式的验收动作（见 §14 与 §17），不进单测，避免 CI 变慢。

---

## 12. 开发任务清单

| # | 任务 | 产出 | 验收 | 状态 |
| --- | --- | --- | --- | --- |
| M0 | 设计 + 骨架 | `docs/DESIGN.md`、`app/{__init__,config,audit}.py`、`requirements.txt`、`.env.example`、`.gitignore`、`tests/conftest.py` | 包可导入，`get_settings()` 正常 | ✅ |
| M1 | 知识库 | `app/kb.py`、`scripts/{ingest,prep_embedding}.py`、`tests/test_kb.py` | `pytest tests/test_kb.py` 全绿；真实导入全部语料打印块数；「MiniMax 题要提交什么」命中 MiniMax「三、提交材料」块并带 `source_url`；`logs/seekdb_queries.jsonl` 有记录 | ✅ |
| M2 | 队伍档案 | `app/team.py`、`tests/test_team.py` | mock 报文正确；真机 `remember → 重启 server → list` 读回一致（`scope_id` 不变）；旧 `progress` 变 `forgotten`；`logs/powercontext_calls.jsonl` 有记录 | ✅ |
| M3 | 问答 | `app/llm.py`、`POST /api/ask`、`GET /healthz` | 无 Key 走抽取式且 `sources` 非空；有 Key 时答案含 `[n]` 引用且只基于片段；赛道未点名时用档案过滤，点名时用点名赛道 | ✅ |
| M4 | 下一步 | `GET /api/teams/{name}/next-steps` | 未完成赛题各出一条以上待办 + 链接，且待办只引用本赛道文档；`days_left` 相对 2026-11-18 24:00(+08) 正确，逾期为负且 `overdue` | ✅ |
| M5 | 审计日志 | `GET /api/logs/{channel}`、`/api/stats` 数据源 | 每次检索/档案调用各多一行；字段含查询、过滤条件、命中 id、耗时 / 方法、路径、摘要、状态码 | ✅ |
| M6 | 前端 | `app/static/*`、`app/main.py`、`/stats` 页 | 浏览器完成「建队 → 勾题 → 改状态 → 提问看来源 → 看下一步」全链路；`/stats` 显示三项指标 | ✅ 代码与 API 契约已核（§15），页面交互需按 `samples/VERIFY.md` §4 点一遍 |
| M7 | 部署 | `Dockerfile`、`.dockerignore`、`scripts/start.sh`、`deploy/modelscope/` | 单容器内 FastAPI + PowerContext 同时就绪；`docker run -p 7860:7860 -v ...` 打开页面可提问；重启容器档案仍在 | ✅ 使用者在 Docker 里实测：镜像构建成功，容器起来后 `/api/health` 为约 430 块 / `hybrid` / PowerContext 在线（当时 429）。创空间那份 `deploy/modelscope/` 未实跑；v0.6 之后构建期多了「语料为空先抓取」一步，未在有 daemon 的机器上复跑 |
| M8 | 验收与打磨 | `scripts/verify.py`、`samples/VERIFY.md`、README | 一条命令跑通端到端并打印关键输出；评审无 Key 也能复现 | ✅（远端 `--base-url` 模式修了中文队名 URL 编码，见 §16） |
| M9 | 语料可脚本同步 | `scripts/fetch_docs.py`、`scripts/ingest.py`、`scripts/start.sh`、两个 Dockerfile、`tests/test_fetch_docs.py` | 重抓一次能与语料对上：同一批文档、同样的文件名与 front matter 格式；**语料缺失时各处自动抓取、失败给出可操作报错** | ✅ 实测复现 59/59（上游新增 1 篇「分享」，见 §16）；空目录跑通 60 篇 → 430 块（见 §17、`samples/VERIFY.md` §3.2） |
| M10 | 抽取式答案可读性 | `app/llm.py`（`clean_markdown_noise`）、`tests/test_llm_extractive.py` | 无 LLM 的答案里不出现 `| :--- | :--- |`、单独的 `**`；表格行摊成「表头：值」 | ✅ 5 例全绿，本机答案样例已复跑 |
| M11 | 项目展示站 | `site/index.html`、`site/styles.css`、`site/assets/*.png`、`.赛题通.qoder.site` | 纯静态、中文、手机可读；内容含问题背景、五个功能截图、seekdb/PowerContext 分工、Qoder 开发四阶段、验证方式、仓库与在线体验链接；效果栏只写「收集中」不放编造数据 | ✅ 本机静态服务与 Qoder Sites 公开发布都已验：<https://saitong-8ibzejymr71.qoder.zone>（`access_mode=public`，未登录 `curl` 首页 200 / 74294 B、五张 PNG 与 `styles.css` 全 200），见 §17 |

后续可选项（不进本次里程碑）：语雀抓取的增量同步（`fetch_docs.py` 目前每次全量重抓）、赛题→文档自动对齐表（当前 `topics` 由 `kb` 里赛题文档解析给出候选）、把检索日志接到 `/stats` 图表、多队伍导出为 Markdown 周报。

---

## 13. 已知风险与对策

| 风险 | 对策 |
| --- | --- |
| 默认 embedding 首次需下载 86MB ONNX，部署环境可能出不去 | `KB_EMBEDDING=auto` 探测失败即降级纯全文检索（§7.1），服务不因此不可用；Dockerfile 构建期预热把模型烘进 `/app/.cache`；`/healthz` 与 `/api/ask` 的 `warning` 明确告知当前模式 |
| 英文向量模型对中文区分度弱 | 排序主要靠中文领域词典 + `$contains` 全文腿；纯向量命中必须含查询词才返回（`dropped_no_overlap`），换中文模型只改 `default_embedding_function()` |
| 嵌入式 seekdb 同目录多进程并发 | 已实测跨进程读写可行；但服务内仍走 `/api/ingest` 单 client，部署固定单进程（`--workers 1`） |
| `data/docs` 更新后旧块残留 | 按 `doc_file` 比对 id 集合，多余 id 显式 `delete`；`--rebuild` 直接清 collection 重建 |
| PowerContext 未起或端口占用 | `/healthz` 明确报 `powercontext.ok=false`；档案接口 503，知识库与检索仍可用（前端分区降级）；`POWERCONTEXT_URL` 可改指远端 |
| LLM 幻觉 / 无 Key | system 限定「只用片段」+ 强制 `[n]` 引用；无 Key 直接抽取式，页面标注答案模式 |
| 中文 `$contains` 召回差 | 领域词典 + ASCII 词切分，抽不到就纯向量检索（hybrid 模式），纯全文模式下宁可不给结果 |
| 企业赛道（MiniMax/Qoder/AMD…）没有独立「赛题一：…」文档 | 选题候选只覆盖社区赛道，企业赛道在前端输入框自建赛题名；名称会拼进待办查询串（`"{赛题} 提交材料"`），写准才有结果。自动对齐表列为后续项（§12 末） |
| 前端 JS 未在浏览器里点验 | 无浏览器环境；`verify.py` 覆盖静态资源可达 + 它依赖的每个响应字段，交互按 `samples/VERIFY.md` §4 手工清单走一遍 |
| 创空间实例回收导致档案丢失 | `DATA_DIR` 必须挂持久卷（README §1.2 与 §10.3 都标了）；`logs/` 一并挂卷便于事后核对 |
| 语雀导出含 `<font>`/图片等噪声 | 解析阶段轻量清洗，表格保留（提交材料、验收项多在表格里） |
| Dockerfile 未在有 daemon 的机器上实跑 | 本机只有 docker CLI 没有 daemon（`docker version` 连不上），构建/运行验证列为交付前必做项；镜像内的命令与路径已按 `python:3.13-slim-bookworm` + `requirements.txt` 逐项核对，依赖的 `.so` 需求（libstdc++/libgcc/libaio）也用 `objdump` 查过 |

---

## 14. M1 实测记录（2026-11-15，`/home/claude/mmvenv`，Python 3.13.18）

命令与结果：

```
$ python scripts/ingest.py --dry-run                # 只切块
59 篇文档 → 431 块

$ python scripts/ingest.py                          # 首次全量
文档 59 篇 → 块 431 个（新增 431），耗时 43.4s

$ python scripts/ingest.py                          # 清洗规则调整后重跑（增量）
文档 59 篇 → 块 429 个（新增 0、更新 15、未变 414、删除 2，无正文文档 2 篇），耗时 5.5s
赛道分布：seekdb:141, PowerContext:96, AMD:64, 通用:63, Dify:14, 阶跃星辰:13,
          Qoder:12, MiniMax:9, 高德地图:9, 魔搭社区:7, 原力灵机:1

$ python scripts/ingest.py --query "MiniMax 题要提交什么" --tracks MiniMax
1. [0.0320] MiniMax · MiniMax › 三、提交材料   ← 完整源码/运行说明/验证样例/技术使用记录 表格
2. [0.0320] MiniMax · MiniMax › 4.2 验收方式
3. [0.0315] MiniMax · MiniMax › 2.4 扩展能力
4. [0.0311] MiniMax · MiniMax › MiniMax（顶部判题分支注意 blockquote）
5. [0.0310] MiniMax · MiniMax › 2.2 技术结合要求
   每条均带 https://oceanbase.yuque.com/.../qlqnoryzrs7gtqbw 原文链接
```

`logs/seekdb_queries.jsonl` 对应行：`ft_terms=["minimax","提交"]`、`where={"track":{"$in":["MiniMax"]}}`、`mode=hybrid`、`elapsed_ms=142.6`、`n_hits=5`、`matched_ids=[…#006,#008,#005,#000,#003]`。

环境实测结论（写代码前已逐条验证，后续模块据此设计）：

1. 默认 embedding `all-MiniLM-L6-v2`（ONNX，384 维）在本机已缓存，**离线可用**，首次加载约 3.4s。
2. 嵌入式 `Client(path=...)` **不要传 `database=`**：库不存在时报 `Unknown database`，用默认库即可（`app/kb.py` 因此不传该参数）。
3. 同一 seekdb 目录可被两个进程同时读写（实测 A 持连接时 B 可 `count`/`upsert`），`scripts/ingest.py` 可与服务并行；新块须 `refresh_index()` 才立即可检索。
4. `where_document` 支持 `$contains` / `$or`；metadata 支持 `$in` / `$ne`；`get()` 必须带 `ids`/`where`/`where_document` 之一 → 全量扫描借封闭的 `track` 取值集合做 `$in`。
5. `hybrid_search` 返回 `{"ids": [[...]], ...}` 嵌套一层，`distances` 即 RRF 融合分（本语料下量级 0.031–0.033，区分度小，故 Top-5 一起交给 LLM 而非只取第一条）。

---

## 15. M2–M8 实测记录（2026-11-15，同一台机器）

```
$ /home/claude/mmvenv/bin/python -m pytest tests -q
78 passed in 34.16s          # test_kb 51 / test_team 17 / test_api 10

$ /home/claude/mmvenv/bin/python scripts/verify.py --restart
25/25 项通过，耗时 3.9s（日志目录 logs）
```

关键实测输出（原文见 `samples/VERIFY.md` §3）：

- **档案持久化**：`PowerContext 重启后档案仍可读 restarted=True tracks=['seekdb']`；`scope_id` 不变，
  `progress` 同赛题只剩一条有效记录（`replaced` 计旧值），旧记录在 PowerContext 里是 `forgotten`。
- **问答在无 Key 环境**：`mode=extractive basis=question 来源=5 耗时=151ms`，`warning` 明说「未配置 LLM」，
  来源逐条带 `snippet` 与 `source_url`；`/api/stats` 的 `modes={"extractive": 6}` 与之互相印证。
- **待办赛道一致性**：`items=[('赛题一：部署一个 seekd','doing',6),('赛题二：GitHub bug','doing',6)]`，
  标成 `done` 后 `剩余 items=1`；每条待办都带 `chunk_id` + `source_url`。
- **三份日志都在写**：`seekdb total=41`、`powercontext total=453`（一次验收 ~160 次档案调用，主要是分页 list），
  `llm total=0`（未配置 Key，通道存在但没调用，符合预期）。
- **噪声过滤生效**：`zzz-不存在的词-qqq` → `n_hits=0 dropped_no_overlap=3`（此前会给 3 条 0.016 分的无关块）；
  代价可见于「提交材料」在 seekdb 赛道的命中从 3 条收窄到 2 条（第 3 条是一个词都不含的纯向量结果）。
- **前端**：`/ui/`、`/ui/app.js`、`/ui/styles.css`、`/stats` 均 200，页面所用的每个响应字段都在 `verify.py` 里断言过；
  开发机没有浏览器，交互按 `samples/VERIFY.md` §4 的清单人工点。
- **部署**：`Dockerfile` / `scripts/start.sh` 只做静态核对与 `bash -n`；随后由使用者在 Docker 里实测——
  镜像构建成功，容器启动后 `/api/health` 为 429 块（即约 430，随语料变化）/ `search_mode=hybrid` / PowerContext 在线。

---

## 16. M8 修复 + M9 语料同步实测（2026-11-15，同一台机器）

```
$ /home/claude/mmvenv/bin/python -m pytest tests -q
89 passed                      # test_kb 51 / test_team 17 / test_api 10 / test_fetch_docs 6 / test_verify 5

$ /home/claude/mmvenv/bin/python scripts/fetch_docs.py --out-dir .runtime/fetch-check/docs --manifest .runtime/fetch-check/manifest.json
目录「2026年OceanBase数据库大赛」下有 63 篇文档，开始抓取（间隔 0.4s）
写入 60 篇到 .runtime/fetch-check/docs，清单 …（跳过 3 篇）
  未收录：加赛部分 / 初赛部分 / OceanBase seekdb基础事件 —— 文档没有正文
```

- **语料可复现**：重抓的 60 篇与仓库里的 59 篇逐一对齐——同一批 slug，front matter（title/path/source_url）
  逐字节一致，正文 `rstrip` 后 59/59 一致；序号 00–52 完全相同。唯一差异是上游新增了一篇「分享」目录页
  （`xen6hatoerd7iggg`，24 字），它落在序号 53，于是 `分享/*` 的六个文件整体后移一位。
  旧快照里没有它，是因为导出时那一页还没有正文（脚本对空正文的处置就是跳过）。
  这一轮只把结果写进 `.runtime/fetch-check/`，仓库里的 `data/docs` 保持不动；v0.6 起这份快照改为**不随仓库分发**，见 §17。
- **抓回的内容不能随手"清洗"**：初版脚本把响应里的字面 `\n` 还原成换行、并剥掉 `<card>` 标签，
  实测改坏两篇（PowerContext 赛题四的 `` `DOCUMENT_ID\nPAGE_TEXT` ``、AMD 部署修复方案里的 `tr '\0' '\n'`）——
  它们是代码里的字面量。实测确认 `/markdown` 返回的已是真换行、正文里没有 `<card>`，目录性空页返回 0 字节，
  于是删掉整个 `normalize_body()`，只留 `.strip()`。教训：以现有快照为准做逐字节 diff，才知道"清洗"该不该存在。
- **中文队名的 URL 编码（远端验收崩溃）**：`urllib` 最终按 ascii 编码 URL，`/api/teams/样例队/tracks`
  在发出前就 `UnicodeEncodeError`；进程内 `TestClient`（httpx）会自动做 IRI 编码，所以这个 bug 只在
  `--base-url` 模式暴露。修在传输层单点：`RemoteClient` 的 `request()`/`get_text()` 统一过
  `encode_path()`（`quote(path, safe="/=?&%#")`，已编码的 `%` 不二次转义），各检查函数不用操心编码。
- **验收脚本自己也有假阳性**：`--restart` 原先在 `LocalClient` 之外又 new 了一个 `PowerContextService`，
  它的 `stop()` 因 `process is None` 空转、`start()` 又因为服务已在跑直接返回 True——**什么都没重启**，
  还差点留下孤儿子进程。改成重启应用自己持有的 `app.state.ctx.service`（`restart()` = stop + 等端口释放 +
  `start(force=True)`），远端实例则明确记 SKIP 而不是 PASS。
- **验收断言不能依赖上一次运行的残留**：`next-steps` 一项在第二次跑时因为上一轮把赛题标成 `done` 而失败；
  现在先把两个赛题压回 `doing` 再断言，脚本可重复执行。
- **运行前基线也要问实例要**：原先 `main()` 直接读本机 `logs/*.jsonl` 与 `.runtime/qa_log.jsonl` 算基线，
  `--base-url` 下变成"容器的 total 对比本机的历史行数"，日志通道那三项断言随之失真。
  改为 `read_baseline(client)`：`/api/health`（是否配了 LLM）、`/api/stats`（ask/search）、
  `/api/logs/{channel}?limit=1`（各通道 total 与日志目录）全走 HTTP，本地与远端同一口径，
  顺带让 `verify.py` 在只有这一个文件的机器上也能跑远端验收。
- **`deploy/modelscope/`**：`Dockerfile` 构建期 `git clone --depth 1 --branch "$REPO_REF" "$REPO_URL" /app`
  （fork 换地址只改 build-arg），依赖安装 / embedding 预热 / 烘入索引与主 Dockerfile 一致；
  仓库里没有 `data/docs` 时改跑 `scripts/fetch_docs.py`。**未在创空间实跑**，端口 7860 在
  README YAML（`app_port`）、`ms_deploy.json`（`port`）与 `EXPOSE` 三处对齐，LLM 三个变量只写占位符。

---

## 17. v0.6 实测：语料不随仓库分发 + 抽取式答案清洗（2026-11-15，同一台机器）

```
$ /home/claude/mmvenv/bin/python -m pytest tests -q
99 passed in 34.95s      # test_kb 51 / test_team 17 / test_api 10 / test_fetch_docs 11 / test_llm_extractive 5 / test_verify 5

$ /home/claude/mmvenv/bin/python scripts/verify.py --restart
25/25 项通过，耗时 9.6s（日志目录 /home/claude/saitong/logs）

$ /home/claude/mmvenv/bin/python scripts/ingest.py --docs .tmp/nodocs/docs --data-dir .tmp/nodocs/run   # 空语料目录
[ingest] .tmp/nodocs/docs 里没有文档（语料快照不入库，按需抓取），尝试从语雀抓取…
写入 60 篇到 .tmp/nodocs/docs，清单 .tmp/nodocs/manifest.json（跳过 3 篇）
文档 60 篇 → 块 430 个（新增 430、更新 0、未变 0、删除 0，无正文文档 2 篇），耗时 45.6s
```

- **抓取点是 `scripts/fetch_docs.py` 的 `ensure_docs()`**，三处调用它而不是各写一份判断：
  `ingest.py::ensure_corpus()`（返回码 0 继续 / 2 退出并打印挂卷、`--docs`、换机器抓三条办法）、
  `start.sh` 的 `fetch_docs.py --if-missing`（失败只告警，不让容器进崩溃循环——空库至少能开页面看接口，
  比反复重启更容易看出来）、两个 Dockerfile 构建期的 `if ! ls data/docs/*.md`。
  `has_docs()` 的判据是「目录里有任意 `.md`」，所以手工放快照、挂卷、重抓三种来源都认。
- **`data/docs/` 进 `.gitignore`，`data/manifest.json` 保留**：`git rm -r --cached data/docs` 已由使用者执行并随 `fe13ace` 提交，
  `git ls-files data/docs` 现在是 0（本机磁盘上那份 59 篇快照还在，靠 ignore 挡住）。因此 README/VERIFY 里的块数不再是定值：
  **约 430 块，随语雀文档更新略有变化**（本机旧快照 429、现抓 60 篇 430，验收只断言 `chunks > 0`）。
- **单测跟着环境走，不假装通过**：`test_render_doc_matches_committed_snapshot` 在没有快照时 `pytest.skip`
  （它比的是仓库文件与脚本输出，干净克隆里两者都缺就没有可比对象）；新增的 6 例是纯 monkeypatch，
  有网没网都能跑，把「有语料不发请求 / 抓取失败返回 False / 成功时写出快照与清单 / ingest 返回码」四种情形钉住。
- **抽取式答案的 Markdown 噪声**（`app/llm.py::clean_markdown_noise`）：现象是答案里出现
  `| 材料 | 要求 | | :--- | :--- |` 和 `1. **网站可访问。` 这类残缺行。做法是逐行判定：
  分隔行（每格都匹配 `^:?-{2,}:?$`）丢掉；表头行不当正文、只作为下一行数据行的标签，
  于是表格行摊成「材料：部署脚本；要求：可一键运行」；一行里 `**` 落单就去掉、成对保留（那是原文强调）。
  只在 `extractive_answer` 的取文处调用一次，LLM 路径的片段原文不动（模型需要看到原始 Markdown）。
  本机复跑真实检索的答案：无 `:---`、无未闭合 `**`。
- **M11 展示站的公开发布已跑通**（`qoderclicn sites ...`，使用者放行命令并授权 `confirmPublic=true` 之后）：
  `get_context` 显示项目没有绑定站点 → `createNewSite=true`；`prepare_site`（`webDirectory=site`、
  `displayName=赛题通`、`slug/subdomain=saitong`、`spa=false`）第一次返回 `descriptorStatus=pending`，
  `get_publish_status` 报 `canPublish=true` 后用同一 `actionId` 重试即写出描述符文件 `.赛题通.qoder.site`；
  平台给的新二级域名是前缀加随机后缀，所以地址是 `saitong-8ibzejymr71.qoder.zone` 而不是裸 `saitong`。
  `update_access_policy`（`mode=public`、`confirmPublic=true`）先落下 `access_mode=public`，再 `publish_site`，
  最终 `get_publish_status` 为 `published=true`、`operation.state=succeeded`、`committed=true`、`readiness=publication_completed`。
  未登录 `curl` 复验：首页 `200 / 74294 B / text/html`（0 次重定向）、`styles.css` `200 / 7339 B`（与本地同尺寸）、
  五张 `assets/*.png` 全 `200`；线上 HTML 比本地 17252 B 大，是因为平台在末尾注入了一段
  `<script data-qoder-watermark="v1">`，我们自己的七个 `<section id=…>` 位置与内容不变。


