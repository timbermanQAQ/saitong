---
title: 赛题通 · OceanBase 大赛赛题问答与进度跟踪
emoji: 🏆
colorFrom: red
colorTo: indigo
sdk: docker
app_port: 7860
pinned: false
license: apache-2.0
short_description: 约 60 篇赛题文档进 seekdb，跨文档问答 + 队伍进度跟踪
---

# 赛题通 · 魔搭创空间（Docker）部署

这个目录只放部署用的东西，**不含应用代码**：镜像在构建时从 Git 仓库克隆代码。

| 文件 | 作用 |
| --- | --- |
| `Dockerfile` | `python:3.13-slim-bookworm` + `git clone` + 装依赖 + 预热 embedding + 烘入索引，`CMD` 是 `scripts/start.sh` |
| `ms_deploy.json` | 创空间部署元数据：`sdk.type=docker`、`port=7860`、非敏感环境变量 |
| `README.md` | 本文件；头部 YAML 同样声明 `sdk: docker` / `app_port: 7860` |

## 1. 部署步骤

1. 在[创空间](https://modelscope.cn/studios)新建空间：**SDK 选 Docker**，硬件选「平台免费资源」即可（本应用不需要 GPU）。
2. 把本目录的三个文件放到空间仓库根目录后 `git push`；
   若希望保留目录结构，则在空间的「部署配置」里把 Dockerfile 路径指到 `deploy/modelscope/Dockerfile`。
   两种放法都一样，因为 Dockerfile 不 `COPY` 构建上下文里的任何文件——代码来自 `git clone`。
3. 端口三处必须一致，都是 **7860**：README 头部 `app_port` / `ms_deploy.json` 的 `port` / Dockerfile 的 `EXPOSE`+`PORT`。
4. 等构建完成（首次约 5–8 分钟：装依赖 + 下载 86MB embedding 模型 + 从语雀抓约 60 篇文档建索引；
   仓库里不带 `data/docs` 快照，构建期会跑 `scripts/fetch_docs.py`，所以**构建环境要能访问 oceanbase.yuque.com**）。
5. 起来后先核对 <https://<空间域名>/api/health>：`seekdb.chunks` 约 430（随语雀文档更新略有变化，只要不是 0 就说明语料进来了）、
   `search_mode` 为 `hybrid`、`powercontext.ok` 为 `true`。

## 2. 环境变量

镜像里已经写死 `HOST=0.0.0.0`、`PORT=7860`、`DATA_DIR=/app/.runtime`、`LOG_DIR=/app/logs`、
`POWERCONTEXT_AUTOSTART=1`，**不配任何变量也能跑完整个功能**。需要在创空间「环境变量 / Secrets」里动的只有 LLM：

| 变量 | 示例值（占位符，勿提交真实值） | 说明 |
| --- | --- | --- |
| `LLM_BASE_URL` | `https://<你的 OpenAI 兼容网关>/v1` | OpenAI 兼容 `chat/completions` 的地址 |
| `LLM_API_KEY` | `<在创空间 Secrets 面板填写>` | **只填在平台的面板里**，不要写进 `README.md`/`ms_deploy.json` 再提交 |
| `LLM_MODEL` | `<该网关上的模型名>` | 例如网关提供的对话模型 |

三个变量**缺任何一个**（含只填 Key 不填 Base URL）都会走**抽取式回答**：`/api/ask` 的
`mode` 返回 `extractive`，答案是从命中文档片段里抽出来的句子，来源链接照常给出，
前端会标注「未配置 LLM，以下为文档抽取结果」。也就是说：**不填 Key 的创空间演示是完全可用的**，
填了 Key 只是把抽取式换成生成式。

其余可选变量（完整列表见仓库根 [`.env.example`](../../.env.example)）：

- `DEADLINE`：倒计时截止时间，默认 `2026-11-19T00:00:00+08:00`（北京时间 11-18 24:00）。
- `KB_EMBEDDING`：`auto`（默认，探测不到向量模型就降级纯全文）/ `onnx` / `fallback`。
- `POWERCONTEXT_URL`：默认指向回环 `http://127.0.0.1:8765`，此时应用自己拉起子进程；
  改成外部地址则只连接不启动。
- `REPO_URL` / `REPO_REF`（仅构建参数）：换用自己的 fork 或分支，
  例如 `--build-arg REPO_URL=https://github.com/<你>/saitong.git --build-arg REPO_REF=dev`。

## 3. 数据存放

- seekdb 索引与 PowerContext 的 SQLite 都在容器内 `$DATA_DIR=/app/.runtime`。
- **知识库索引已经烘在镜像里**，实例回收/重启不影响检索与问答。
- **队伍档案（选题/进度/卡点）是运行期写入的**：创空间若没有给你持久卷，实例回收后档案会丢。
  要长期保留就把 `DATA_DIR` 指到平台提供的持久目录，或定期用 `/api/teams/{name}` 导出。
- 自建/本地跑请挂卷：`docker run -p 7860:7860 -v $PWD/.ms-runtime:/app/.runtime <image>`。

## 4. 本地先验一遍再推

```bash
docker build -f deploy/modelscope/Dockerfile -t saitong-ms .
docker run -d --name saitong-ms -p 7860:7860 -v "$PWD/.ms-runtime:/app/.runtime" saitong-ms
python scripts/verify.py --base-url http://127.0.0.1:7860   # 24 项端到端验收（远端模式下重启项跳过）
docker rm -f saitong-ms
```

上面这条 `docker build -f deploy/modelscope/Dockerfile` 是在**构建机本地**跑的：`ARG REPO_URL` 默认指向 GitHub 上的仓库，
所以要先 `git push`，构建出来的才是最新代码；想验未推送的分支就 `--build-arg REPO_URL=<你的 fork> --build-arg REPO_REF=<分支>`。

抓语雀这条路径现在是**主路径**（`data/docs` 不随仓库分发，构建时仓库里就没有快照），建议在有外网的机器上确认它跑通了：
构建日志里应有 `目录「2026年OceanBase数据库大赛」下有 63 篇文档，开始抓取（间隔 0.4s）` 和 `写入 60 篇 …`，
之后 ingest 报 `文档 60 篇 → 块 430 个`（数字随语雀更新变化）。

## 5. 已知不确定项（照实说）

- 本目录的 Dockerfile 与元数据**尚未在魔搭创空间上实跑过**：开发机没有 docker daemon，创空间账号侧的
  构建日志也没看过。镜像逻辑与仓库根 `Dockerfile` 一致（后者已由使用者在 Docker 里实测通过）。
- `ms_deploy.json` 的字段名以创空间控制台文档为准；平台实际只读 README 头部 YAML 时，
  把 `sdk` / `app_port` 这两行对齐即可，`ms_deploy.json` 里另外的非敏感变量可以照抄进控制台。
- 构建期 `git clone` 与 `pip install` 需要平台构建机有外网；若构建机无外网，
  请把 `data/docs` 提交进空间仓库并改用「从上下文 COPY」的方式（此时参考仓库根 `Dockerfile`）。
