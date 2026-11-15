"""[M1] 知识库：把语雀导出文档切块导入 seekdb，并提供混合检索与检索日志。

对外主要入口：
    kb = KnowledgeBase()
    kb.ingest()                        # 按内容哈希增量导入
    hits = kb.search("MiniMax 题要提交什么", tracks=["MiniMax"])
"""

from __future__ import annotations

import hashlib
import math
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from .audit import log_event
from .config import DEFAULT_MAX_CHUNK_CHARS, Settings, get_settings

# --------------------------------------------------------------------------- #
# 赛道
# --------------------------------------------------------------------------- #

TRACK_GENERAL = "通用"

# 同一分段内按此优先级匹配：生态名优先于通用技术名（"AMD … seekdb …" 归 AMD）。
TRACK_RULES: tuple[tuple[str, str], ...] = (
    ("minimax", "MiniMax"),
    ("qoder", "Qoder"),
    ("amd", "AMD"),
    ("dify", "Dify"),
    ("高德", "高德地图"),
    ("阶跃星辰", "阶跃星辰"),
    ("魔搭", "魔搭社区"),
    ("powercontext", "PowerContext"),
    ("原力灵机", "原力灵机"),
    ("seekdb", "seekdb"),
)

ALL_TRACKS: tuple[str, ...] = tuple(rule[1] for rule in TRACK_RULES) + (TRACK_GENERAL,)

# 全文检索用的大赛领域词：优先长词，避免整句 $contains 召回为空。
FT_LEXICON: tuple[str, ...] = (
    "提交材料", "提交产物", "提交方式", "提交要求", "运行说明", "验证样例", "技术使用记录",
    "代码差异", "测试结果", "算分规则", "评分标准", "必做项", "验收方式", "验收项",
    "截止时间", "比赛仓库", "私有仓库", "判题", "报名", "奖项", "常见问题", "向量数据库",
    "全文索引", "记忆处理", "重排服务", "增量传输", "断点恢复", "原始记录", "证据页",
    "上下文工程", "部署", "安装", "环境", "测试", "验收", "提交", "材料", "算分", "评分",
    "分数", "得分", "截止", "仓库", "分支", "题目", "赛题", "要求", "任务", "背景", "方案",
    "文档", "命令", "日志", "记录", "缓存", "并发", "会话", "索引", "向量", "检索", "召回",
    "预算", "证据", "规格", "数据库", "服务", "接口", "插件", "脚本", "镜像", "容器",
    "权限", "账号", "奖励", "时间", "方式", "规则", "报告", "说明", "样例",
)
_FT_LEXICON_SORTED: tuple[str, ...] = tuple(sorted(FT_LEXICON, key=len, reverse=True))

# --------------------------------------------------------------------------- #
# 解析
# --------------------------------------------------------------------------- #

_FRONT_MATTER_RE = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*\r?\n?", re.S)
_FENCE_RE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})")
_SECTION_RE = re.compile(r"^(#{2,3})[ \t]+(.+?)[ \t]*$")
_ASCII_TOKEN_RE = re.compile(r"[a-z][a-z0-9_.\-]{1,}")
_PARA_SPLIT_RE = re.compile(r"\n[ \t]*\n")
_FONT_TAG_RE = re.compile(r"</?font[^>]*>", re.I)
_BR_RE = re.compile(r"<br\s*/?>", re.I)
_MD_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_IMG_TAG_RE = re.compile(r"<img[^>]*>", re.I)
_MANY_NEWLINES_RE = re.compile(r"\n{3,}")
# 语雀导出里的卡片占位行：没有正文，只会污染检索结果（如「企业第八讲：MiniMax」）
_YUQUE_CARD_RE = re.compile(r"^[ \t]*\[此处为语雀卡片[^\]]*\]\([^)]*\)[ \t]*$", re.M)


@dataclass(frozen=True)
class ParsedDoc:
    file_name: str
    stem: str
    title: str
    path: str
    source_url: str
    body: str


@dataclass(frozen=True)
class Chunk:
    id: str
    text: str
    metadata: dict[str, Any]

    @property
    def content_sha(self) -> str:
        return str(self.metadata.get("content_sha", ""))


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def clean_body(text: str) -> str:
    """去掉语雀导出的 HTML 噪声、图片与卡片占位，保留表格、列表与附件链接。"""
    text = _FONT_TAG_RE.sub("", text)
    text = _BR_RE.sub("\n", text)
    text = _IMG_TAG_RE.sub("", text)
    text = _MD_IMAGE_RE.sub("", text)
    text = _YUQUE_CARD_RE.sub("", text)
    return _MANY_NEWLINES_RE.sub("\n\n", text).strip()


def parse_markdown(text: str, *, file_name: str = "") -> ParsedDoc:
    """解析 front matter（title/path/source_url）+ 正文。"""
    match = _FRONT_MATTER_RE.match(text)
    meta: dict[str, str] = {}
    body = text
    if match:
        body = text[match.end():]
        for line in match.group(1).splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or ":" not in stripped:
                continue
            key, value = stripped.split(":", 1)
            meta[key.strip().lower()] = _unquote(value)
    stem = Path(file_name).stem if file_name else "doc"
    title = meta.get("title") or stem
    return ParsedDoc(
        file_name=file_name or f"{stem}.md",
        stem=stem,
        title=title,
        path=meta.get("path") or title,
        source_url=meta.get("source_url", ""),
        body=clean_body(body),
    )


def read_doc(path: Path) -> ParsedDoc:
    return parse_markdown(path.read_text(encoding="utf-8"), file_name=path.name)


def infer_track(path: str, *, title: str = "") -> str:
    """从语雀层级路径推断赛道，从左到右取第一个命中的分段。"""
    segments = [seg.strip() for seg in re.split(r"\s*/\s*", path or "") if seg.strip()]
    if title:
        segments.append(title.strip())
    for segment in segments:
        lowered = segment.lower()
        for keyword, track in TRACK_RULES:
            if keyword in lowered:
                return track
    return TRACK_GENERAL


# --------------------------------------------------------------------------- #
# 切块
# --------------------------------------------------------------------------- #

def split_sections(body: str) -> list[tuple[list[str], str]]:
    """按 H2/H3 切段（代码围栏内的 # 不算标题），返回 (面包屑, 正文)。"""
    sections: list[tuple[list[str], str]] = []
    buffer: list[str] = []
    crumb: list[str] = []
    h2: str | None = None
    h3: str | None = None
    fence: str | None = None

    def flush() -> None:
        if not buffer:
            return
        text = "\n".join(buffer).strip()
        if text:
            sections.append((list(crumb), text))

    for raw_line in body.splitlines():
        line = raw_line.rstrip()
        fence_match = _FENCE_RE.match(line)
        if fence_match:
            token = fence_match.group(1)[0] * 3
            if fence is None:
                fence = token
            elif line.strip().startswith(fence):
                fence = None
            buffer.append(line)
            continue
        if fence is None:
            heading = _SECTION_RE.match(line)
            if heading:
                flush()
                buffer = []
                level, heading_text = len(heading.group(1)), heading.group(2).strip()
                if level == 2:
                    h2, h3 = heading_text, None
                else:
                    h3 = heading_text
                crumb = [part for part in (h2, h3) if part]
                continue
        buffer.append(line)
    flush()
    return sections


def _hard_split(text: str, max_chars: int) -> list[str]:
    parts: list[str] = []
    current = ""
    for line in text.splitlines():
        while len(line) > max_chars:
            if current:
                parts.append(current)
                current = ""
            parts.append(line[:max_chars])
            line = line[max_chars:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > max_chars:
            if current:
                parts.append(current)
            current = line
        else:
            current = candidate
    if current:
        parts.append(current)
    return [part for part in parts if part.strip()]


def pack_text(text: str, max_chars: int) -> list[str]:
    """超长章节按段落贪心切分；单段仍超长时按行硬切。"""
    if max_chars <= 0 or len(text) <= max_chars:
        return [text] if text.strip() else []
    parts: list[str] = []
    current = ""
    for paragraph in _PARA_SPLIT_RE.split(text):
        paragraph = paragraph.strip("\n")
        if not paragraph.strip():
            continue
        if len(paragraph) > max_chars:
            if current:
                parts.append(current)
                current = ""
            parts.extend(_hard_split(paragraph, max_chars))
            continue
        candidate = f"{current}\n\n{paragraph}" if current else paragraph
        if len(candidate) > max_chars:
            parts.append(current)
            current = paragraph
        else:
            current = candidate
    if current:
        parts.append(current)
    return [part for part in parts if part.strip()]


def content_sha(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def build_chunks(doc: ParsedDoc, *, max_chars: int = DEFAULT_MAX_CHUNK_CHARS) -> list[Chunk]:
    """文档 → 检索块，块文本带标题面包屑以提升召回。"""
    track = infer_track(doc.path, title=doc.title)
    chunks: list[Chunk] = []
    for crumb, content in split_sections(doc.body):
        for part_index, part in enumerate(pack_text(content, max_chars)):
            sequence = len(chunks)
            trail = " > ".join([doc.title, *crumb])
            text = f"【{trail}】\n{part}"
            chunks.append(
                Chunk(
                    id=f"{doc.stem}#{sequence:03d}",
                    text=text,
                    metadata={
                        "doc_title": doc.title,
                        "path": doc.path,
                        "source_url": doc.source_url,
                        "track": track,
                        "heading": crumb[-1] if crumb else doc.title,
                        "heading_path": " > ".join(crumb),
                        "doc_file": doc.stem,
                        "chunk_index": sequence,
                        "part_index": part_index,
                        "chars": len(part),
                        "content_sha": content_sha(text),
                    },
                )
            )
    return chunks


def build_ft_terms(query: str, max_terms: int = 4) -> list[str]:
    """从问题里抽适合做全文 $contains 的词；抽不到就交给纯向量检索。"""
    text = (query or "").strip().lower()
    if not text:
        return []
    terms: list[str] = []
    seen: set[str] = set()

    def add(term: str) -> None:
        term = term.strip()
        if len(term) >= 2 and term not in seen:
            seen.add(term)
            terms.append(term)

    for token in _ASCII_TOKEN_RE.findall(text):
        add(token)
    matches = [word for word in _FT_LEXICON_SORTED if word in text]
    for word in matches:
        if any(word != other and word in other for other in matches):
            continue  # 已有更长的词覆盖，例如「提交材料」覆盖「提交」
        add(word)
    return terms[:max_terms]


def _shares_term(text: str, terms: Sequence[str]) -> bool:
    """片段（含首行面包屑，里面是文档标题与章节）里是否出现了查询的词。"""
    lowered = (text or "").lower()
    return any(term in lowered for term in terms)


def overlap_terms(query: str) -> list[str]:
    """噪声过滤用的词表：比检索词更宽（同族短词不合并），只用于判断片段是否相关。"""
    lowered = (query or "").strip().lower()
    if not lowered:
        return []
    words = [word for word in _FT_LEXICON_SORTED if word in lowered]
    return list(dict.fromkeys(_ASCII_TOKEN_RE.findall(lowered) + words))


# --------------------------------------------------------------------------- #
# 结果对象
# --------------------------------------------------------------------------- #

@dataclass
class SearchHit:
    id: str
    text: str
    metadata: dict[str, Any]
    score: float | None = None

    def _meta(self, key: str) -> str:
        value = self.metadata.get(key)
        return "" if value is None else str(value)

    @property
    def track(self) -> str:
        return self._meta("track")

    @property
    def doc_title(self) -> str:
        return self._meta("doc_title")

    @property
    def heading(self) -> str:
        return self._meta("heading")

    @property
    def source_url(self) -> str:
        return self._meta("source_url")

    def snippet(self, max_chars: int = 320) -> str:
        # 首行是【面包屑】，展示时从正文起
        body = self.text.split("\n", 1)[1] if "\n" in self.text else self.text
        body = re.sub(r"\s+", " ", body).strip()
        return body if len(body) <= max_chars else body[: max_chars - 1] + "…"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "score": self.score,
            "track": self.track,
            "doc_title": self.doc_title,
            "heading": self.heading,
            "heading_path": self._meta("heading_path"),
            "path": self._meta("path"),
            "source_url": self.source_url,
            "snippet": self.snippet(),
            "text": self.text,
        }


@dataclass
class IngestReport:
    docs: int = 0
    chunks: int = 0
    added: int = 0
    updated: int = 0
    unchanged: int = 0
    deleted: int = 0
    skipped_docs: int = 0
    tracks: dict[str, int] = field(default_factory=dict)
    collection: str = ""
    elapsed_ms: float = 0.0
    rebuilt: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "docs": self.docs,
            "chunks": self.chunks,
            "added": self.added,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "deleted": self.deleted,
            "skipped_docs": self.skipped_docs,
            "tracks": dict(self.tracks),
            "collection": self.collection,
            "elapsed_ms": round(self.elapsed_ms, 1),
            "rebuilt": self.rebuilt,
        }

    def summary(self) -> str:
        tracks = ", ".join(
            f"{name}:{count}" for name, count in sorted(self.tracks.items(), key=lambda kv: -kv[1])
        )
        skipped = f"，无正文文档 {self.skipped_docs} 篇" if self.skipped_docs else ""
        return (
            f"文档 {self.docs} 篇 → 块 {self.chunks} 个（新增 {self.added}、更新 {self.updated}、"
            f"未变 {self.unchanged}、删除 {self.deleted}{skipped}），耗时 {self.elapsed_ms / 1000:.1f}s"
            + (f"\n赛道分布：{tracks}" if tracks else "")
        )


# --------------------------------------------------------------------------- #
# seekdb 客户端
# --------------------------------------------------------------------------- #

_CLIENTS: dict[str, Any] = {}
_CLIENTS_LOCK = threading.Lock()

_SCAN_PAGE = 5000
_BATCH_SIZE = 32


def get_client(seekdb_path: Path) -> Any:
    """嵌入式 seekdb client 每进程复用一个（模型与 libseekdb 实例只加载一次）。"""
    key = str(Path(seekdb_path))
    with _CLIENTS_LOCK:
        client = _CLIENTS.get(key)
        if client is None:
            import pyseekdb

            Path(key).mkdir(parents=True, exist_ok=True)
            client = pyseekdb.Client(path=key)
            _CLIENTS[key] = client
        return client


def reset_clients() -> None:
    with _CLIENTS_LOCK:
        _CLIENTS.clear()


# --------------------------------------------------------------------------- #
# 向量化后端：默认 ONNX 模型，离线拉不到时降级为纯全文检索
# --------------------------------------------------------------------------- #

EMBEDDING_MODEL = "all-MiniLM-L6-v2"
EMBEDDING_DIM = 384
MODE_HYBRID = "hybrid"
MODE_FULLTEXT = "fulltext_only"

_EMBEDDING_LOCK = threading.Lock()
_EMBEDDING_STATE: dict[str, dict[str, Any]] = {}


class FallbackEmbeddingFunction:
    """离线兜底：用文本哈希生成确定性伪向量。

    目的是让 collection 在没有模型的环境里也能建起来、全文检索照常可用；
    该模式下向量相似度无语义，检索只走全文腿（见 KnowledgeBase.search）。
    """

    def __init__(self, dim: int = EMBEDDING_DIM) -> None:
        self.dim = int(dim)

    def name(self) -> str:
        return "saitong-hash-fallback"

    def __call__(self, input: Sequence[str]) -> list[list[float]]:
        return [self.embed(text) for text in input]

    def embed(self, text: str) -> list[float]:
        seed = (text or "").encode("utf-8")
        values: list[float] = []
        counter = 0
        while len(values) < self.dim:
            digest = hashlib.sha256(seed + counter.to_bytes(4, "big")).digest()
            values.extend(byte / 255.0 for byte in digest)
            counter += 1
        vector = values[: self.dim]
        norm = math.sqrt(sum(value * value for value in vector)) or 1.0
        return [value / norm for value in vector]


def default_embedding_function() -> Any:
    from pyseekdb import DefaultEmbeddingFunction

    return DefaultEmbeddingFunction(model_name=EMBEDDING_MODEL)


def embedding_state(settings: Settings | None = None) -> dict[str, Any]:
    """决定向量化后端（进程内只做一次）：`KB_EMBEDDING=auto|onnx|fallback`。"""
    cfg = settings or get_settings()
    requested = (cfg.kb_embedding or "auto").strip().lower()
    with _EMBEDDING_LOCK:
        cached = _EMBEDDING_STATE.get(requested)
        if cached is not None:
            return cached

        started = time.perf_counter()
        if requested == "fallback":
            state = {"mode": MODE_FULLTEXT, "model": "saitong-hash-fallback",
                     "detail": "KB_EMBEDDING=fallback"}
        elif requested == "onnx":
            # 强制模式：模型不可用时让调用方直接看到异常（Docker 构建期自检用）
            default_embedding_function()
            state = {"mode": MODE_HYBRID, "model": EMBEDDING_MODEL, "detail": None}
        else:
            try:
                default_embedding_function()(["赛题通自检"])
                state = {"mode": MODE_HYBRID, "model": EMBEDDING_MODEL, "detail": None}
            except Exception as exc:
                state = {"mode": MODE_FULLTEXT, "model": "saitong-hash-fallback",
                         "detail": f"{type(exc).__name__}: {exc}"[:300]}
        state["checked_in_ms"] = round((time.perf_counter() - started) * 1000, 1)
        state["requested"] = requested
        _EMBEDDING_STATE[requested] = state
        log_event("seekdb", settings=cfg, event="embedding_probe", **state)
        return state


def reset_embedding_state() -> None:
    with _EMBEDDING_LOCK:
        _EMBEDDING_STATE.clear()


def embedding_function(settings: Settings | None = None) -> Any:
    if embedding_state(settings)["mode"] == MODE_FULLTEXT:
        return FallbackEmbeddingFunction()
    return default_embedding_function()


class KnowledgeBase:
    """seekdb 知识库：导入、增量更新与混合检索。"""

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        docs_dir: Path | str | None = None,
        data_dir: Path | str | None = None,
        collection: str | None = None,
        max_chunk_chars: int | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.docs_dir = Path(docs_dir) if docs_dir else self.settings.docs_dir
        self.data_dir = Path(data_dir) if data_dir else self.settings.data_dir
        self.collection_name = collection or self.settings.kb_collection
        self.max_chunk_chars = max_chunk_chars or self.settings.kb_max_chunk_chars
        self._collection: Any = None

    # -- seekdb ------------------------------------------------------------ #

    @property
    def seekdb_path(self) -> Path:
        return self.data_dir / "seekdb"

    @property
    def client(self) -> Any:
        return get_client(self.seekdb_path)

    @property
    def collection(self) -> Any:
        if self._collection is None:
            self._collection = self.client.get_or_create_collection(
                self.collection_name, embedding_function=embedding_function(self.settings)
            )
        return self._collection

    @property
    def search_mode(self) -> str:
        """hybrid：全文 + 向量 RRF 融合；fulltext_only：离线兜底，只用 $contains 全文腿。"""
        return embedding_state(self.settings)["mode"]

    def count(self) -> int:
        return int(self.collection.count())

    # -- 文档 -------------------------------------------------------------- #

    def doc_files(self) -> list[Path]:
        if not self.docs_dir.is_dir():
            raise FileNotFoundError(f"文档目录不存在：{self.docs_dir}")
        return sorted(self.docs_dir.glob("*.md"))

    def load_docs(self) -> list[ParsedDoc]:
        return [read_doc(path) for path in self.doc_files()]

    def chunks_for(self, docs: Iterable[ParsedDoc] | None = None) -> list[Chunk]:
        documents = self.load_docs() if docs is None else list(docs)
        chunks: list[Chunk] = []
        for doc in documents:
            chunks.extend(build_chunks(doc, max_chars=self.max_chunk_chars))
        return chunks

    def track_counts(self, docs: Sequence[ParsedDoc] | None = None) -> dict[str, int]:
        counts: dict[str, int] = {}
        for chunk in self.chunks_for(docs):
            counts[chunk.metadata["track"]] = counts.get(chunk.metadata["track"], 0) + 1
        return counts

    # -- 导入 -------------------------------------------------------------- #

    def _scan_existing(self) -> dict[str, dict[str, Any]]:
        """列出现有块（track 取值封闭，用 $in 做一次全量扫描）。"""
        rows: dict[str, dict[str, Any]] = {}
        offset = 0
        while True:
            page = self.collection.get(
                where={"track": {"$in": list(ALL_TRACKS)}},
                limit=_SCAN_PAGE,
                offset=offset,
                include=["metadatas"],
            )
            ids = list(page.get("ids") or [])
            metadatas = list(page.get("metadatas") or [])
            for chunk_id, meta in zip(ids, metadatas):
                rows[str(chunk_id)] = meta or {}
            if len(ids) < _SCAN_PAGE:
                break
            offset += _SCAN_PAGE
        return rows

    def ingest(
        self,
        *,
        rebuild: bool = False,
        progress: Callable[[str], None] | None = None,
    ) -> IngestReport:
        """按 content_sha 增量 upsert；结束后 refresh_index 让新块立即可检索。"""
        started = time.perf_counter()
        report = IngestReport(collection=self.collection_name)
        docs = self.load_docs()
        report.docs = len(docs)
        if progress:
            progress(f"解析 {report.docs} 篇文档 …")

        if rebuild and self.client.has_collection(self.collection_name):
            self.client.delete_collection(self.collection_name)
            self._collection = None
            report.rebuilt = True

        desired = self.chunks_for(docs)
        report.chunks = len(desired)
        for chunk in desired:
            track = str(chunk.metadata["track"])
            report.tracks[track] = report.tracks.get(track, 0) + 1

        existing = {} if report.rebuilt else self._scan_existing()
        desired_by_doc: dict[str, dict[str, Chunk]] = {}
        for chunk in desired:
            desired_by_doc.setdefault(str(chunk.metadata["doc_file"]), {})[chunk.id] = chunk
        existing_by_doc: dict[str, set[str]] = {}
        for chunk_id, meta in existing.items():
            existing_by_doc.setdefault(str(meta.get("doc_file", "")), set()).add(chunk_id)
        report.skipped_docs = report.docs - len(desired_by_doc)

        # 文档已从 data/docs 删除 → 它的块全部作废
        stale_ids: list[str] = [
            chunk_id
            for stem, chunk_ids in existing_by_doc.items()
            if stem not in desired_by_doc
            for chunk_id in chunk_ids
        ]

        to_write: list[Chunk] = []
        for chunk in desired:
            previous = existing.get(chunk.id)
            if previous is None:
                report.added += 1
                to_write.append(chunk)
            elif str(previous.get("content_sha", "")) != chunk.content_sha:
                report.updated += 1
                to_write.append(chunk)
            else:
                report.unchanged += 1

        # 标题结构变化导致 id 位移 → 删掉本轮没有生成的旧 id
        for stem, wanted in desired_by_doc.items():
            stale_ids.extend(chunk_id for chunk_id in existing_by_doc.get(stem, set()) if chunk_id not in wanted)
        stale_ids = sorted(set(stale_ids))

        if progress:
            progress(
                f"待写入 {len(to_write)} 块（新增 {report.added} / 更新 {report.updated}），"
                f"未变 {report.unchanged} 块，待删 {len(stale_ids)} 块 …"
            )

        collection = self.collection
        for start in range(0, len(to_write), _BATCH_SIZE):
            batch = to_write[start : start + _BATCH_SIZE]
            collection.upsert(
                ids=[chunk.id for chunk in batch],
                documents=[chunk.text for chunk in batch],
                metadatas=[chunk.metadata for chunk in batch],
            )
            if progress and len(to_write) > _BATCH_SIZE:
                progress(f"已写入 {min(start + _BATCH_SIZE, len(to_write))}/{len(to_write)} 块 …")

        if stale_ids:
            collection.delete(ids=stale_ids)
            report.deleted = len(stale_ids)

        collection.refresh_index()
        report.elapsed_ms = (time.perf_counter() - started) * 1000.0
        payload = report.as_dict()
        tracks = payload.pop("tracks")
        log_event(
            "seekdb",
            settings=self.settings,
            event="ingest",
            docs_dir=str(self.docs_dir),
            tracks=tracks,
            **payload,
        )
        return report

    # -- 检索 -------------------------------------------------------------- #

    def _unwrap(self, raw: dict[str, Any] | None) -> list[SearchHit]:
        if not raw:
            return []

        def first(key: str) -> list[Any]:
            values = raw.get(key) or []
            if values and isinstance(values[0], list):
                return list(values[0])
            return list(values)

        ids = first("ids")
        documents = first("documents")
        metadatas = first("metadatas")
        distances = first("distances")
        hits: list[SearchHit] = []
        for index, chunk_id in enumerate(ids):
            metadata = metadatas[index] if index < len(metadatas) and metadatas[index] else {}
            text = documents[index] if index < len(documents) and documents[index] else ""
            score = distances[index] if index < len(distances) else None
            hits.append(SearchHit(id=str(chunk_id), text=str(text), metadata=dict(metadata), score=score))
        return hits

    def search(
        self,
        query: str,
        *,
        tracks: Sequence[str] | None = None,
        top_k: int | None = None,
        log: bool = True,
    ) -> list[SearchHit]:
        """全文 $contains + 向量 knn，RRF 融合排序，可按赛道过滤。

        embedding 模型不可用（离线兜底）时退化为纯全文检索：没有全文词就不返回噪声。
        hybrid 模式下同样过滤「一个查询词都不含」的纯向量命中，避免弱模型把无关片段顶进来。
        """
        text = (query or "").strip()
        limit = max(1, int(top_k or self.settings.kb_top_k))
        track_filter = [t for t in (tracks or []) if t]
        where = {"track": {"$in": track_filter}} if track_filter else None
        terms = build_ft_terms(text)
        guard = overlap_terms(text)
        if len(terms) > 1:
            where_document: dict[str, Any] | None = {"$or": [{"$contains": t} for t in terms]}
        elif terms:
            where_document = {"$contains": terms[0]}
        else:
            where_document = None
        hybrid = self.search_mode == MODE_HYBRID
        if hybrid:
            mode = "hybrid" if where_document else "knn_only"
        else:
            mode = "fulltext_only" if where_document else "fulltext_only_no_terms"
        leg = min(max(limit * 4, 10), 60)

        started = time.perf_counter()
        hits: list[SearchHit] = []
        error: str | None = None
        dropped = 0
        if not text:
            error = "empty_query"
        elif mode == "fulltext_only_no_terms":
            hits = []
        else:
            try:
                kwargs: dict[str, Any] = {
                    "n_results": limit,
                    "include": ["documents", "metadatas"],
                }
                if where_document:
                    kwargs["query"] = {
                        "where_document": where_document,
                        "n_results": leg,
                        **({"where": where} if where else {}),
                    }
                if hybrid:
                    kwargs["knn"] = {"query_texts": [text], "n_results": leg,
                                     **({"where": where} if where else {})}
                    kwargs["rank"] = {"rrf": {"rank_window_size": 60, "rank_constant": 60}}
                hits = self._unwrap(self.collection.hybrid_search(**kwargs))
                if hybrid and guard:
                    # 默认英文 MiniLM 对中文几乎没有区分度（相近 0.335 / 无关 0.306），
                    # 纯向量腿会把无关块顶进来；有词可判时要求片段至少命中一个词。
                    kept = [hit for hit in hits if _shares_term(hit.text, guard)]
                    dropped = len(hits) - len(kept)
                    hits = kept
            except Exception as exc:  # 检索失败不能打挂请求，记录后返回空结果
                error = f"{type(exc).__name__}: {exc}"
        elapsed_ms = (time.perf_counter() - started) * 1000.0

        if log:
            log_event(
                "seekdb",
                settings=self.settings,
                event="hybrid_search",
                collection=self.collection_name,
                query=text,
                ft_terms=terms,
                guard_terms=guard,
                where_document=where_document,
                where=where,
                tracks=track_filter or None,
                top_k=limit,
                mode=mode,
                elapsed_ms=round(elapsed_ms, 1),
                n_hits=len(hits),
                matched_ids=[hit.id for hit in hits],
                scores=[hit.score for hit in hits],
                dropped_no_overlap=dropped,
                error=error,
            )
        return hits

    # -- 运维 -------------------------------------------------------------- #

    def stats(self) -> dict[str, Any]:
        rows = self._scan_existing()
        by_track: dict[str, int] = {}
        docs_by_track: dict[str, set[str]] = {}
        by_doc: set[str] = set()
        for meta in rows.values():
            track = str(meta.get("track", TRACK_GENERAL))
            by_track[track] = by_track.get(track, 0) + 1
            by_doc.add(str(meta.get("doc_file", "")))
            docs_by_track.setdefault(track, set()).add(str(meta.get("doc_title", "")))
        return {
            "collection": self.collection_name,
            "chunks": len(rows),
            "docs": len(by_doc),
            "tracks": dict(sorted(by_track.items(), key=lambda kv: -kv[1])),
            "track_docs": {track: sorted(titles) for track, titles in sorted(docs_by_track.items())},
            "seekdb_path": str(self.seekdb_path),
            "docs_dir": str(self.docs_dir),
        }

    def health(self) -> dict[str, Any]:
        embedding = embedding_state(self.settings)
        try:
            chunks = self.count()
        except Exception as exc:
            return {"ok": False, "chunks": 0, "collection": self.collection_name,
                    "search_mode": embedding["mode"], "embedding": embedding, "error": str(exc)}
        return {"ok": True, "chunks": chunks, "collection": self.collection_name,
                "search_mode": embedding["mode"], "embedding": embedding}
