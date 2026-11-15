"""HTTP 层：把 kb（知识库）、team（队伍档案）、llm（生成答案）组合成 /api/*。

路由只在这里出现；三个能力模块互不依赖，方便单测各自覆盖。
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field, field_validator

from . import audit, kb as kb_module
from . import llm as llm_module
from . import team as team_module
from .config import Settings
from .kb import ALL_TRACKS, KnowledgeBase, SearchHit
from .llm import ContextPiece
from .team import PowerContextService, TeamStore, TeamProfile

DEFAULT_TOP_K = 5
MAX_TOP_K = 20
NEXT_STEP_QUERIES: tuple[str, ...] = ("{topic} 提交材料", "{topic} 验收", "{track} 提交方式")
NEXT_STEP_TODO_LIMIT = 6

QA_LOG_FILE = "qa_log.jsonl"
_HOT_QUESTION_STOP = re.compile(r"[\s，。！？、；：,.!?;:\"'“”‘’()（）【】\[\]]+")


@dataclass
class AppContext:
    """挂在 `app.state.ctx` 上的依赖集合。"""

    settings: Settings
    kb: KnowledgeBase
    teams: TeamStore
    service: PowerContextService | None = None
    qa: "QaLog" = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.qa is None:
            self.qa = QaLog(self.settings.data_dir / QA_LOG_FILE)


class QaLog:
    """问答与检索计数落盘（DATA_DIR/qa_log.jsonl）：只存问题文本，不存队名等个人信息。"""

    def __init__(self, path: Path) -> None:
        self.path = path

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    @staticmethod
    def normalize(question: str) -> str:
        return _HOT_QUESTION_STOP.sub("", (question or "").strip().lower())[:80]

    def record(self, event: str, *, question: str = "", **fields: Any) -> dict[str, Any]:
        entry = {"ts": self._now(), "event": event, **fields}
        if question:
            entry["q"] = self.normalize(question)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
        except OSError:
            pass
        return entry

    def rows(self, limit: int | None = None) -> list[dict[str, Any]]:
        if not self.path.is_file():
            return []
        lines = [line for line in self.path.read_text(encoding="utf-8").splitlines() if line.strip()]
        if limit is not None:
            lines = lines[-limit:]
        rows: list[dict[str, Any]] = []
        for line in lines:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return rows

    def aggregate(self, *, top_n: int = 10) -> dict[str, Any]:
        asks = [row for row in self.rows() if row.get("event") == "ask"]
        searches = [row for row in self.rows() if row.get("event") == "search"]
        counter: dict[str, int] = {}
        for row in asks:
            key = row.get("q") or ""
            if key:
                counter[key] = counter.get(key, 0) + 1
        modes: dict[str, int] = {}
        for row in asks:
            mode = str(row.get("mode") or "unknown")
            modes[mode] = modes.get(mode, 0) + 1
        hot = sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))[:top_n]
        return {
            "ask_count": len(asks),
            "search_count": len(searches),
            "modes": modes,
            "hot_questions": [{"question": text, "count": count} for text, count in hot],
            "path": str(self.path),
        }


# --------------------------------------------------------------------------- #
# 请求模型
# --------------------------------------------------------------------------- #

def _clean(value: str) -> str:
    return (value or "").strip()


class TeamIn(BaseModel):
    name: str = Field(min_length=1, max_length=60, pattern=r"\S")

    @field_validator("name")
    @classmethod
    def _strip(cls, value: str) -> str:
        return _clean(value)


class TracksIn(BaseModel):
    track: str = Field(min_length=1, max_length=40, pattern=r"\S")
    topics: list[str] = Field(default_factory=list)

    @field_validator("track")
    @classmethod
    def _strip(cls, value: str) -> str:
        return _clean(value)

    @field_validator("topics")
    @classmethod
    def _strip_topics(cls, value: list[str]) -> list[str]:
        return [_clean(item)[:120] for item in value if _clean(item)][:30]


class ProgressIn(BaseModel):
    topic: str = Field(min_length=1, max_length=120, pattern=r"\S")
    track: str = Field(default="", max_length=40)
    status: str = Field(default="doing", min_length=1, max_length=20)
    note: str = Field(default="", max_length=500)

    @field_validator("topic", "track", "status")
    @classmethod
    def _strip(cls, value: str) -> str:
        return _clean(value)


class BlockerIn(BaseModel):
    text: str = Field(min_length=1, max_length=500, pattern=r"\S")
    track: str = Field(default="", max_length=40)
    topic: str = Field(default="", max_length=120)

    @field_validator("text", "track", "topic")
    @classmethod
    def _strip(cls, value: str) -> str:
        return _clean(value)


class NoteIn(BaseModel):
    text: str = Field(min_length=1, max_length=500, pattern=r"\S")
    track: str = Field(default="", max_length=40)
    topic: str = Field(default="", max_length=120)


class SearchIn(BaseModel):
    q: str = Field(min_length=1, max_length=300, pattern=r"\S")
    tracks: list[str] = Field(default_factory=list)
    top_k: int = Field(default=DEFAULT_TOP_K, ge=1, le=MAX_TOP_K)

    @field_validator("q")
    @classmethod
    def _strip(cls, value: str) -> str:
        return _clean(value)

    @field_validator("tracks")
    @classmethod
    def _known(cls, value: list[str]) -> list[str]:
        return [item for item in (_clean(v) for v in value) if item in ALL_TRACKS]


class AskIn(BaseModel):
    question: str = Field(min_length=1, max_length=300, pattern=r"\S")
    team: str = Field(default="", max_length=60)
    tracks: list[str] = Field(default_factory=list)
    top_k: int = Field(default=DEFAULT_TOP_K, ge=1, le=MAX_TOP_K)

    @field_validator("question", "team")
    @classmethod
    def _strip(cls, value: str) -> str:
        return _clean(value)

    @field_validator("tracks")
    @classmethod
    def _known(cls, value: list[str]) -> list[str]:
        return [item for item in (_clean(v) for v in value) if item in ALL_TRACKS]


class IngestIn(BaseModel):
    rebuild: bool = False


# --------------------------------------------------------------------------- #
# 纯函数：赛道判定与待办抽取
# --------------------------------------------------------------------------- #

def mentioned_tracks(question: str) -> list[str]:
    """问题文本里点名了哪些赛道（关键词或赛道名本身）。"""
    text = (question or "").lower()
    found: list[str] = []
    for keyword, track in kb_module.TRACK_RULES:
        if keyword in text and track not in found:
            found.append(track)
    for track in ALL_TRACKS:
        if track.lower() in text and track not in found:
            found.append(track)
    return found


def resolve_track_filter(
    question: str,
    requested: Sequence[str] | None,
    profile: TeamProfile | None,
) -> tuple[list[str], str]:
    """显式选择 > 问题点名 > 队伍档案 > 全库。"""
    explicit = [track for track in (requested or []) if track]
    if explicit:
        return explicit, "request"
    named = mentioned_tracks(question)
    if named:
        return named, "question"
    if profile and profile.tracks:
        return list(profile.tracks), "team"
    return [], "all"


_TODO_HINT_RE = re.compile(
    r"提交|材料|验收|运行说明|验证样例|测试|报告|记录|部署|仓库|分支|commit|pr|fork|算分|评分|要求"
    r"|submission|acceptance|deploy|repo",
    re.I,
)
_HEADING_RE = re.compile(r"^\s*#{1,6}\s*\S")
_LIST_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)、])\s+(?P<text>.+?)\s*$")


def _clean_line(line: str) -> str:
    text = re.sub(r"[|`*>#]+", " ", line)
    text = re.sub(r"\s+", " ", text).strip(" ，,、.。;；:：-")
    return text


_SEPARATOR_ROW_RE = re.compile(r"^\s*\|?[\s:|-]+\|?\s*$")


def _row_cells(row: str) -> list[str]:
    return [cell for cell in (_clean_line(part) for part in row.strip().strip("|").split("|")) if cell]


def extract_todos(hits: Iterable[SearchHit], *, limit: int = NEXT_STEP_TODO_LIMIT) -> list[dict[str, Any]]:
    """从检索片段里抽取「要交什么 / 要做什么」条目（纯函数，便于单测）。

    规则：正文/列表行必须命中提交类关键词；「提交材料」章节里的表格行本身就是要交的东西，
    直接取「材料：要求」，表头行与分隔行丢弃。
    """
    items: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(text: str, hit: SearchHit, *, require_hint: bool = True) -> bool:
        cleaned = _clean_line(text)[:110].strip()
        key = re.sub(r"\s+", "", cleaned.lower())
        if len(cleaned) < 4 or key in seen:
            return False
        if require_hint and not _TODO_HINT_RE.search(cleaned):
            return False
        seen.add(key)
        items.append({
            "text": cleaned,
            "doc_title": hit.doc_title,
            "heading": hit.heading,
            "track": hit.track,
            "source_url": hit.source_url,
            "chunk_id": hit.id,
        })
        return True

    for hit in hits:
        lines = (hit.text or "").splitlines()
        submission_section = bool(_TODO_HINT_RE.search(
            f"{hit.heading} {' '.join(lines[:2])}"))
        for index, raw in enumerate(lines):
            raw = raw.strip()
            if not raw or raw.startswith("【") or _HEADING_RE.match(raw):
                continue
            if len(items) >= limit:
                return items[:limit]
            if raw.startswith("|"):
                if _SEPARATOR_ROW_RE.match(raw):
                    continue
                following = lines[index + 1].strip() if index + 1 < len(lines) else ""
                if _SEPARATOR_ROW_RE.match(following):
                    continue  # 表头行
                cells = _row_cells(raw)
                if len(cells) < 2:
                    continue
                add(f"{cells[0]}：{cells[1]}", hit, require_hint=not submission_section)
                continue
            listed = _LIST_RE.match(raw)
            add(listed.group("text") if listed else raw, hit,
                require_hint=not (submission_section and listed is not None))
    return items[:limit]


def next_step_queries(track: str, topic: str) -> list[str]:
    return [template.format(track=track, topic=topic).strip() for template in NEXT_STEP_QUERIES]


# --------------------------------------------------------------------------- #
# 路由
# --------------------------------------------------------------------------- #

router = APIRouter()


def ctx_of(request: Request) -> AppContext:
    context: AppContext | None = getattr(request.app.state, "ctx", None)
    if context is None:  # pragma: no cover - 组装错误
        raise HTTPException(500, detail={"error": "app_context_missing"})
    return context


def _store(request: Request) -> TeamStore:
    store = ctx_of(request).teams
    if not store.client.probe()["ok"]:
        raise HTTPException(503, detail={"error": "powercontext_unavailable",
                                         "url": store.client.base_url,
                                         "hint": "POWERCONTEXT_AUTOSTART=1 或先启动 powercontext server"})
    return store


def _profile_or_404(store: TeamStore, name: str) -> TeamProfile:
    profile = store.profile_or_none(name)
    if profile is None:
        raise HTTPException(404, detail={"error": "team_not_found", "team": _clean(name)})
    return profile


def _pieces(hits: Sequence[SearchHit]) -> list[ContextPiece]:
    return [
        ContextPiece(doc_title=hit.doc_title, heading=hit.heading, source_url=hit.source_url,
                     text=hit.text, track=hit.track, chunk_id=hit.id)
        for hit in hits
    ]


def _sources(hits: Sequence[SearchHit]) -> list[dict[str, Any]]:
    return [
        {"n": index, **hit.to_dict()}
        for index, hit in enumerate(hits, start=1)
    ]


# ------------------------------------------------------------------ 运维
@router.get("/api/health")
def health(request: Request) -> dict[str, Any]:
    ctx = ctx_of(request)
    seekdb = ctx.kb.health()
    probe = ctx.teams.client.probe()
    llm_ready = llm_module.is_configured(ctx.settings)
    degraded = not seekdb["ok"] or not probe["ok"]
    return {
        "status": "degraded" if degraded else "ok",
        "seekdb": seekdb,
        "search_mode": seekdb.get("search_mode"),
        "embedding": seekdb.get("embedding"),
        "powercontext": {"ok": probe["ok"], "url": probe["url"],
                         "error": probe.get("error"),
                         "spawned": bool(ctx.service and ctx.service.process is not None)},
        "llm": {"enabled": llm_ready, "model": ctx.settings.llm_model if llm_ready else None},
        "days_left": ctx.settings.days_left(),
        "deadline": ctx.settings.deadline.isoformat(),
    }


@router.get("/api/tracks")
def tracks(request: Request) -> dict[str, Any]:
    ctx = ctx_of(request)
    stats = ctx.kb.stats()
    counts = stats["tracks"]
    docs = stats.get("track_docs", {})
    return {
        "tracks": [{"name": name, "chunks": counts.get(name, 0),
                    "docs": len(docs.get(name, [])),
                    "topics": docs.get(name, [])} for name in ALL_TRACKS],
        "chunks": stats["chunks"],
        "docs": stats["docs"],
        "deadline": ctx.settings.deadline.isoformat(),
        "days_left": ctx.settings.days_left(),
    }


@router.post("/api/ingest")
def ingest(payload: IngestIn, request: Request) -> dict[str, Any]:
    report = ctx_of(request).kb.ingest(rebuild=payload.rebuild)
    return {**report.as_dict(), "summary": report.summary()}


@router.get("/api/logs/{channel}")
def logs(channel: str, request: Request, limit: int = Query(20, ge=1, le=500)) -> dict[str, Any]:
    if channel not in audit.CHANNELS:
        raise HTTPException(422, detail={"error": "unknown_channel", "allowed": list(audit.CHANNELS)})
    cfg = ctx_of(request).settings
    return {
        "channel": channel,
        "path": str(audit.log_path(channel, cfg)),
        "total": audit.count_lines(channel, cfg),
        "items": audit.read_tail(channel, limit=limit, settings=cfg),
    }


# ------------------------------------------------------------------ 队伍
@router.post("/api/teams")
def create_team(payload: TeamIn, request: Request) -> dict[str, Any]:
    return _store(request).ensure_team(payload.name)


@router.get("/api/teams")
def list_teams(request: Request) -> dict[str, Any]:
    return {"teams": _store(request).teams()}


@router.get("/api/teams/{name}")
def get_team(name: str, request: Request) -> dict[str, Any]:
    profile = _profile_or_404(_store(request), name)
    data = profile.as_dict()
    data["next_steps_preview"] = ctx_of(request).teams.unfinished(profile)[:3]
    return data


@router.put("/api/teams/{name}/tracks")
def set_tracks(name: str, payload: TracksIn, request: Request) -> dict[str, Any]:
    store = _store(request)
    _profile_or_404(store, name)
    result = store.set_track_choices(name, payload.track, payload.topics)
    return {"team": name, "result": result, "tracks": store.profile(name).tracks}


@router.put("/api/teams/{name}/progress")
def set_progress(name: str, payload: ProgressIn, request: Request) -> dict[str, Any]:
    store = _store(request)
    _profile_or_404(store, name)
    result = store.set_progress(name, payload.track, payload.topic, payload.status, payload.note)
    profile = store.profile(name)
    saved = result["progress"]
    return {"team": name, "track": saved["track"], "topic": saved["topic"],
            "status": profile.status_of(saved["track"], saved["topic"]),
            "progress": profile.as_dict()["progress"], "replaced": result["replaced"]}


@router.post("/api/teams/{name}/blockers")
def add_blocker(name: str, payload: BlockerIn, request: Request) -> dict[str, Any]:
    store = _store(request)
    _profile_or_404(store, name)
    return {"team": name, "blocker": store.add_blocker(
        name, payload.text, track=payload.track, topic=payload.topic)}


@router.post("/api/teams/{name}/blockers/{blocker_id}/resolve")
def resolve_blocker(name: str, blocker_id: str, request: Request) -> dict[str, Any]:
    store = _store(request)
    _profile_or_404(store, name)
    try:
        resolved = store.resolve_blocker(name, blocker_id)
    except KeyError as exc:
        raise HTTPException(404, detail={"error": "blocker_not_found", "id": blocker_id}) from exc
    return {"team": name, "blocker": resolved}


@router.post("/api/teams/{name}/notes")
def add_note(name: str, payload: NoteIn, request: Request) -> dict[str, Any]:
    store = _store(request)
    _profile_or_404(store, name)
    return {"team": name, "note": store.add_note(name, payload.text, track=payload.track,
                                                 topic=payload.topic)}


@router.get("/api/teams/{name}/next-steps")
def next_steps(name: str, request: Request, top_k: int = Query(3, ge=1, le=8)) -> dict[str, Any]:
    ctx = ctx_of(request)
    profile = _profile_or_404(_store(request), name)
    items: list[dict[str, Any]] = []
    for entry in ctx.teams.unfinished(profile):
        todos: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        track = entry["track"]
        queries = next_step_queries(track, entry["topic"])
        for query in queries:
            # 必须限定在该赛题所属赛道，否则「提交材料」这类通用词会把别的赛道文档补位进来
            hits = ctx.kb.search(query, tracks=[track] if track else None, top_k=top_k)
            for todo in extract_todos(hits):
                if track and todo["track"] != track:
                    continue  # 检索侧过滤失效时的兜底：待办来源赛道必须一致
                key = (todo["chunk_id"], todo["text"])
                if key in seen:
                    continue
                seen.add(key)
                todos.append(todo)
                if len(todos) >= NEXT_STEP_TODO_LIMIT:
                    break
            if len(todos) >= NEXT_STEP_TODO_LIMIT:
                break
        items.append({**entry, "todos": todos[:NEXT_STEP_TODO_LIMIT], "queries": queries,
                      "search_tracks": [track] if track else []})
    missing = [track for track in profile.tracks if not profile.topics_for(track)]
    return {
        "team": profile.name,
        "days_left": ctx.settings.days_left(),
        "overdue": ctx.settings.days_left() < 0,
        "deadline": ctx.settings.deadline.isoformat(),
        "items": items,
        "no_track_selected": not profile.tracks,
        "tracks_without_topics": missing,
    }


# ------------------------------------------------------------------ 检索与问答
@router.post("/api/search")
def search(payload: SearchIn, request: Request) -> dict[str, Any]:
    ctx = ctx_of(request)
    started = time.perf_counter()
    hits = ctx.kb.search(payload.q, tracks=payload.tracks or None, top_k=payload.top_k)
    ctx.qa.record("search", question=payload.q, n_hits=len(hits),
                  tracks=payload.tracks or None)
    return {
        "query": payload.q,
        "tracks": payload.tracks or None,
        "top_k": payload.top_k,
        "search_mode": ctx.kb.search_mode,
        "hits": [hit.to_dict() for hit in hits],
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
    }


@router.post("/api/ask")
def ask(payload: AskIn, request: Request) -> dict[str, Any]:
    ctx = ctx_of(request)
    started = time.perf_counter()
    profile = None
    if payload.team:
        profile = ctx.teams.profile_or_none(payload.team)
        if profile is None:
            raise HTTPException(404, detail={"error": "team_not_found", "team": payload.team})
    tracks, basis = resolve_track_filter(payload.question, payload.tracks, profile)

    hits = ctx.kb.search(payload.question, tracks=tracks or None, top_k=payload.top_k)
    pieces = _pieces(hits)
    notes: list[str] = []
    mode = "extractive"
    model: str | None = None

    if pieces and llm_module.is_configured(ctx.settings):
        try:
            result = llm_module.answer(payload.question, pieces, settings=ctx.settings)
            answer, mode, model = result.answer, "llm", result.model
        except llm_module.LLMUnavailable as exc:
            notes.append(f"LLM 未返回可用答案（{exc}），已改用抽取式答案")
            answer = llm_module.extractive_answer(payload.question, pieces)
    else:
        if not pieces:
            answer = ("没有检索到相关内容"
                      + ("：换个说法，或在问题里带上赛道名（如 MiniMax、seekdb、高德）。"
                         if not tracks else f"：当前限定赛道 {tracks}，可去掉赛道限制再试一次。"))
        else:
            answer = llm_module.extractive_answer(payload.question, pieces)
        if not llm_module.is_configured(ctx.settings):
            notes.append("未配置 LLM（LLM_BASE_URL / LLM_API_KEY / LLM_MODEL），答案为抽取式")

    if ctx.kb.search_mode != "hybrid":
        notes.append("离线模式：默认 embedding 模型不可用，检索退化为纯全文匹配，"
                     "问题里带上「提交材料」「验收」这类词更容易命中")
    warning = "；".join(notes) or None

    used = sorted({hit.track for hit in hits if hit.track})
    ctx.qa.record(
        "ask",
        question=payload.question,
        mode=mode,
        model=model,
        n_hits=len(hits),
        used_tracks=used,
        track_basis=basis,
        requested_tracks=payload.tracks or None,
        has_team=bool(payload.team),
        answer_chars=len(answer),
        elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
    )
    return {
        "question": payload.question,
        "answer": answer,
        "mode": mode,
        "model": model,
        "track_basis": basis,
        "filter_tracks": tracks,
        "used_tracks": used,
        "search_mode": ctx.kb.search_mode,
        "sources": _sources(hits),
        "warning": warning,
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
    }


# ------------------------------------------------------------------ 统计
@router.get("/api/stats")
def stats(request: Request, top_n: int = Query(10, ge=1, le=50)) -> dict[str, Any]:
    ctx = ctx_of(request)
    aggregate = ctx.qa.aggregate(top_n=top_n)
    teams = []
    probe = ctx.teams.client.probe()
    if probe["ok"]:
        try:
            teams = ctx.teams.teams()
        except team_module.PowerContextError:
            teams = []
    kb_stats = ctx.kb.stats()
    return {
        "ask_count": aggregate["ask_count"],
        "search_count": aggregate["search_count"],
        "team_count": len(teams),
        "teams": [row["name"] for row in teams],
        "modes": aggregate["modes"],
        "hot_questions": aggregate["hot_questions"],
        "chunks": kb_stats["chunks"],
        "docs": kb_stats["docs"],
        "track_chunks": kb_stats["tracks"],
        "days_left": ctx.settings.days_left(),
        "deadline": ctx.settings.deadline.isoformat(),
        "llm_enabled": llm_module.is_configured(ctx.settings),
    }
