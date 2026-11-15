#!/usr/bin/env python
"""端到端验收：按 README 的流程跑一遍，逐项打印 PASS/FAIL 与实际输出。

默认在进程内起 app（复用 .runtime 里已导入的知识库，并自动拉起 PowerContext）；
加 --base-url 则对着已部署的实例跑，不改任何代码路径。

用法：
    python scripts/verify.py                 # 本机验收
    python scripts/verify.py --restart       # 额外验证 PowerContext 重启后档案仍在
    python scripts/verify.py --base-url http://127.0.0.1:7860
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("DATA_DIR", ".runtime")
os.environ.setdefault("LOG_DIR", "logs")

TEAM = os.environ.get("VERIFY_TEAM", "验收队-勿删")
TRACK = "seekdb"
TOPICS = ["赛题一：部署一个 seekdb", "赛题二：GitHub bug修复"]
TOPIC = TOPICS[0]
# 写死而不 import app.audit.CHANNELS：--base-url 模式下这台机器上可能根本没有 app 包
LOG_CHANNELS = ("seekdb", "powercontext", "llm")


class Result:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str]] = []  # (名称, PASS|FAIL|SKIP, 详情)

    def _emit(self, status: str, name: str, detail: str) -> bool:
        self.rows.append((name, status, detail))
        print(f"{status}  {name}" + (f"\n      {detail}" if detail else ""))
        return status == "PASS"

    def add(self, name: str, ok: bool, detail: str = "") -> bool:
        return self._emit("PASS" if ok else "FAIL", name, detail)

    def skip(self, name: str, detail: str = "") -> bool:
        return self._emit("SKIP", name, detail)

    @property
    def failed(self) -> int:
        return sum(1 for _, status, _ in self.rows if status == "FAIL")

    @property
    def skipped(self) -> int:
        return sum(1 for _, status, _ in self.rows if status == "SKIP")


class Client:
    """统一的最小请求接口：返回 (status, json)。"""

    def request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> tuple[int, Any]:
        raise NotImplementedError

    def restart_service(self) -> tuple[str, str]:
        """重启本脚本能控制的 PowerContext 子进程：返回 ("ok"|"failed"|"unsupported", 详情)。"""
        return "unsupported", "该客户端不能重启服务"


class LocalClient(Client):
    def __init__(self) -> None:
        from fastapi.testclient import TestClient

        from app.main import create_app

        self.app = create_app()
        self.http = TestClient(self.app)
        self.http.__enter__()

    def restart_service(self) -> tuple[str, str]:
        service = getattr(self.app.state.ctx, "service", None)
        if service is None:
            return "unsupported", "应用没有自己拉起 PowerContext（POWERCONTEXT_AUTOSTART=0）"
        return ("ok" if service.restart() else "failed"), \
            f"pid={service.process.pid if service.process else None} url={service.base_url}"

    def request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> tuple[int, Any]:
        res = self.http.request(method, path, json=payload)
        try:
            return res.status_code, res.json()
        except ValueError:
            return res.status_code, res.text

    def get_text(self, path: str) -> tuple[int, str]:
        res = self.http.get(path)
        return res.status_code, res.text

    def close(self) -> None:
        self.http.__exit__(None, None, None)


class RemoteClient(Client):
    def __init__(self, base_url: str) -> None:
        self.base = base_url.rstrip("/")

    def restart_service(self) -> tuple[str, str]:
        return "unsupported", f"远端实例（{self.base}）的 PowerContext 由容器/服务自己持有，脚本不重启它"

    def request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> tuple[int, Any]:
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(self.base + encode_path(path), data=data, method=method,
                                     headers={"Content-Type": "application/json"} if data else {})
        try:
            with urllib.request.urlopen(req, timeout=60) as res:
                body = res.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", "replace")
        try:
            return res.status, json.loads(body)
        except json.JSONDecodeError:
            return res.status, body

    def get_text(self, path: str) -> tuple[int, str]:
        with urllib.request.urlopen(self.base + encode_path(path), timeout=60) as res:
            return res.status, res.read().decode("utf-8", "replace")

    def close(self) -> None:
        return None


def dump(value: Any, limit: int = 200) -> str:
    text = json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[:limit] + "…"


def encode_path(path: str) -> str:
    """路径里允许中文队名：非 ASCII 段做 percent-encoding，结构性字符与已编码的 % 不动。

    urllib 会把 URL 按 ascii 编码，未转义的「/api/teams/样例队/tracks」直接抛 UnicodeEncodeError；
    TestClient（httpx）会自动做 IRI 编码，所以这个 bug 只在 --base-url 远端验收时暴露。
    """
    return urllib.parse.quote(path, safe="/=?&%#")


# --------------------------------------------------------------------------- #
# 各验收步骤
# --------------------------------------------------------------------------- #

def check_pages(client: Client, out: Result) -> None:
    status, body = client.get_text("/ui/")
    out.add("页面 GET /ui/ 返回单页", status == 200 and "赛题通" in body,
            f"status={status} len={len(body)}")
    for path, needle in (("/ui/app.js", "api/ask"), ("/ui/styles.css", "--brand"), ("/stats", "运行指标")):
        status, body = client.get_text(path)
        out.add(f"页面 GET {path}", status == 200 and needle in body, f"status={status}")


def check_health(client: Client, out: Result) -> dict[str, Any]:
    status, health = client.request("GET", "/api/health")
    chunks = (health.get("seekdb") or {}).get("chunks", 0) if isinstance(health, dict) else 0
    ok = out.add("健康检查 /api/health", status == 200 and chunks > 0,
                 f"status={status} status字段={health.get('status')} 块数={chunks} 模式={health.get('search_mode')}")
    if ok:
        pc = health.get("powercontext") or {}
        out.add("PowerContext 在线（队伍档案可用）", bool(pc.get("ok")), dump(pc))
        out.add("距提交天数已下发", isinstance(health.get("days_left"), int),
                f"days_left={health.get('days_left')} deadline={str(health.get('deadline'))[:10]}")
    return health if isinstance(health, dict) else {}


def check_tracks(client: Client, out: Result) -> None:
    status, data = client.request("GET", "/api/tracks")
    tracks = {row["name"]: row for row in data.get("tracks", [])} if isinstance(data, dict) else {}
    candidates = tracks.get(TRACK, {}).get("topics", [])
    out.add("赛道统计含赛题候选（前端勾选题目用）",
            status == 200 and TRACK in tracks and TOPIC in candidates,
            f"{TRACK}: {len(candidates)} 个赛题候选，如 {candidates[:2]}")


def check_search(client: Client, out: Result) -> None:
    status, data = client.request("POST", "/api/search", {"q": "提交材料", "tracks": [TRACK], "top_k": 3})
    hits = data.get("hits", []) if isinstance(data, dict) else []
    out.add("检索「提交材料」限定赛道",
            status == 200 and len(hits) > 0 and all(h["track"] == TRACK for h in hits),
            f"status={status} mode={data.get('search_mode')} 命中={len(hits)} 赛道={sorted({h['track'] for h in hits})}")
    status, data = client.request("POST", "/api/search", {"q": "zzz-不存在的词-qqq", "top_k": 3})
    out.add("无意义查询返回空而不是噪声",
            status == 200 and len(data.get("hits", [])) == 0, f"命中={len(data.get('hits', []))}")


def check_ask(client: Client, out: Result) -> None:
    status, data = client.request("POST", "/api/ask", {"question": f"{TRACK} 赛题一要做什么？", "team": TEAM})
    if status != 200:
        out.add("问答 /api/ask", False, f"status={status} {dump(data)}")
        return
    sources = data.get("sources", [])
    fields_ok = all(key in data for key in ("answer", "mode", "track_basis", "used_tracks", "search_mode", "elapsed_ms"))
    cited = all(("n" in s and "snippet" in s) for s in sources)
    out.add("问答 /api/ask 字段齐全且来源可核对",
            bool(data.get("answer")) and fields_ok and cited,
            f"mode={data.get('mode')} basis={data.get('track_basis')} 来源={len(sources)} "
            f"耗时={data.get('elapsed_ms')}ms")
    print(f"      答案节选：{str(data.get('answer')).splitlines()[0][:120] if data.get('answer') else '—'}")
    if data.get("warning"):
        print(f"      提示：{data['warning']}")


def check_team_flow(client: Client, out: Result) -> None:
    status, created = client.request("POST", "/api/teams", {"name": TEAM})
    out.add("建档 POST /api/teams", status == 200 and bool(created.get("scope_id")), dump(created))

    status, data = client.request("PUT", f"/api/teams/{TEAM}/tracks", {"track": TRACK, "topics": TOPICS})
    out.add("选题 PUT /tracks（赛道 + 赛题清单）",
            status == 200 and data.get("tracks") == [TRACK] and data.get("result", {}).get("topics") == TOPICS,
            f"status={status} tracks={data.get('tracks')} topics={dump(data.get('result', {}).get('topics'))}")

    status, data = client.request("PUT", f"/api/teams/{TEAM}/progress",
                                  {"track": TRACK, "topic": TOPIC, "status": "doing", "note": "容器内跑通部署"})
    saved = [row for row in data.get("progress", []) if row.get("topic") == TOPIC]
    out.add("进度 PUT /progress（状态 + 备注覆盖旧值）",
            status == 200 and data.get("status") == "doing" and len(saved) == 1,
            f"status={data.get('status')} 该赛题记录数={len(saved)} replaced={data.get('replaced')}")

    status, data = client.request("POST", f"/api/teams/{TEAM}/blockers",
                                  {"track": TRACK, "topic": TOPIC, "text": "向量索引在容器里初始化慢"})
    blocker = data.get("blocker") or {}
    out.add("卡点 POST /blockers", status == 200 and bool(blocker.get("id")), dump(blocker))

    status, data = client.request("POST",
                                  f"/api/teams/{TEAM}/blockers/{blocker.get('id')}/resolve")
    resolved = data.get("blocker") or {}
    out.add("解决卡点 POST /blockers/{id}/resolve",
            status == 200 and resolved.get("resolved") is True,
            f"resolved={resolved.get('resolved')} state={resolved.get('state')}")

    status, profile = client.request("GET", f"/api/teams/{TEAM}")
    shape_ok = (profile.get("tracks") == [TRACK]
                and set(profile.get("topics", {}).get(TRACK, [])) == set(TOPICS)
                and any(row.get("status") == "doing" for row in profile.get("progress", []))
                and any(row.get("resolved") for row in profile.get("blockers", [])))
    out.add("复读档案 GET /api/teams/{name}", status == 200 and shape_ok, dump({
        "tracks": profile.get("tracks"), "topics": profile.get("topics"),
        "progress": [row.get("status") for row in profile.get("progress", [])],
        "blockers_resolved": [row.get("resolved") for row in profile.get("blockers", [])],
    }))


def check_next_steps(client: Client, out: Result) -> None:
    # 待办只覆盖未完成的赛题；先把两个赛题都压回 doing，避免上一次验收留下的 done 影响断言
    for topic in TOPICS:
        client.request("PUT", f"/api/teams/{TEAM}/progress",
                       {"track": TRACK, "topic": topic, "status": "doing", "note": "验收进行中"})
    status, data = client.request("GET", f"/api/teams/{TEAM}/next-steps?top_k=3")
    items = data.get("items", []) if isinstance(data, dict) else []
    with_todos = [item for item in items if item.get("todos")]
    out.add("下一步 GET /next-steps（未完成赛题 + 文档待办）",
            status == 200 and len(items) == 2 and len(with_todos) >= 1
            and all(item["status"] != "done" for item in items),
            f"items={[(i['topic'][:14], i['status'], len(i['todos'])) for i in items]}")
    if items:
        first = items[0]["todos"][0] if items[0]["todos"] else None
        print(f"      待办示例：{dump(first, 240) if first else '（该赛题暂无待办）'}")
    out.add("倒计时随档案下发", isinstance(data.get("days_left"), int) and "overdue" in data,
            f"days_left={data.get('days_left')} overdue={data.get('overdue')}")

    client.request("PUT", f"/api/teams/{TEAM}/tracks", {"track": TRACK, "topics": [TOPICS[1]]})
    client.request("PUT", f"/api/teams/{TEAM}/progress",
                   {"track": TRACK, "topic": TOPICS[1], "status": "done", "note": "已完成"})
    status, data = client.request("GET", f"/api/teams/{TEAM}/next-steps?top_k=3")
    out.add("标记已完成后从待办里消失",
            status == 200 and all(item["topic"] != TOPICS[1] for item in data.get("items", [])),
            f"剩余 items={len(data.get('items', []))}")
    client.request("PUT", f"/api/teams/{TEAM}/tracks", {"track": TRACK, "topics": TOPICS})


def read_baseline(client: Client) -> dict[str, Any]:
    """运行前的计数一律从"被验收的那个实例"读：本地与远端同一口径。

    以前这里直接读本机 `logs/*.jsonl` 和本机 `.runtime/qa_log.jsonl`，对 `--base-url` 是错的：
    比的是容器里的 total 和本机的历史行数。
    """
    status, health = client.request("GET", "/api/health")
    status, stats = client.request("GET", "/api/stats")
    before: dict[str, Any] = {
        "ask": stats.get("ask_count", 0) if isinstance(stats, dict) else 0,
        "search": stats.get("search_count", 0) if isinstance(stats, dict) else 0,
        "llm_configured": bool(isinstance(health, dict) and (health.get("llm") or {}).get("enabled")),
        "log_dir": "",
    }
    for channel in LOG_CHANNELS:
        status, log = client.request("GET", f"/api/logs/{channel}?limit=1")
        before[channel] = log.get("total", 0) if isinstance(log, dict) else 0
        if channel == "seekdb" and isinstance(log, dict) and log.get("path"):
            before["log_dir"] = str(Path(log["path"]).parent)
    return before


def check_logs(client: Client, out: Result, before: dict[str, Any]) -> None:
    status, data = client.request("GET", "/api/stats")
    out.add("统计 GET /api/stats（提问/检索/队伍/赛道分布）",
            status == 200 and data.get("ask_count", 0) > before["ask"] and data.get("team_count", 0) >= 1,
            f"ask={data.get('ask_count')} search={data.get('search_count')} teams={data.get('team_count')}")
    print(f"      模式分布={data.get('modes')} 热门问题={dump(data.get('hot_questions', [])[:2])}")
    for channel in LOG_CHANNELS:
        status, log = client.request("GET", f"/api/logs/{channel}?limit=2")
        grew = status == 200 and log.get("total", 0) > before[channel]
        if channel == "llm" and not before.get("llm_configured"):
            out.add(f"日志通道 {channel}（未配置 LLM，允许为空）", status == 200,
                    f"total={log.get('total')} path={log.get('path')}")
        else:
            out.add(f"日志通道 {channel} 有新增可核对行", grew,
                    f"total={log.get('total')} 最近={dump((log.get('items') or [{}])[-1], 160)}")


def check_restart(client: Client, out: Result) -> None:
    """真重启应用自己拉起的 PowerContext 子进程，再从 HTTP 读回档案（数据在磁盘上）。"""
    state, detail = client.restart_service()
    if state == "unsupported":
        out.skip("PowerContext 重启后档案仍可读", detail)
        return
    if state != "ok":
        out.add("PowerContext 重启后档案仍可读", False, f"重启失败：{detail}")
        return
    status, profile = client.request("GET", f"/api/teams/{TEAM}")
    tracks = profile.get("tracks") if isinstance(profile, dict) else None
    out.add("PowerContext 重启后档案仍可读（持久化在 POWERCONTEXT_HOME）",
            status == 200 and tracks == [TRACK], f"{detail} tracks={tracks}")


# --------------------------------------------------------------------------- #

def main() -> int:
    parser = argparse.ArgumentParser(description="赛题通端到端验收")
    parser.add_argument("--base-url", default="", help="对已部署实例验收，例如 http://127.0.0.1:7860")
    parser.add_argument("--restart", action="store_true", help="额外验证 PowerContext 重启后档案仍在")
    args = parser.parse_args()

    if args.base_url:
        client: Client = RemoteClient(args.base_url)
        print(f"→ 对远端实例验收：{args.base_url}")
    else:
        os.environ.setdefault("POWERCONTEXT_AUTOSTART", "1")
        client = LocalClient()
        print(f"→ 进程内验收：DATA_DIR={os.environ['DATA_DIR']} 队伍={TEAM}")

    before = read_baseline(client)

    out = Result()
    started = time.perf_counter()
    try:
        check_pages(client, out)
        check_health(client, out)
        check_tracks(client, out)
        check_search(client, out)
        check_team_flow(client, out)
        check_ask(client, out)
        check_next_steps(client, out)
        check_logs(client, out, before)
        if args.restart:
            check_restart(client, out)
    finally:
        client.close()

    total = len(out.rows)
    passed = total - out.failed - out.skipped
    tail = f"，{out.skipped} 项跳过" if out.skipped else ""
    where = f"（日志目录 {before['log_dir']}）" if before["log_dir"] else ""
    print(f"\n{passed}/{total} 项通过{tail}，耗时 {time.perf_counter() - started:.1f}s{where}")
    return 1 if out.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
