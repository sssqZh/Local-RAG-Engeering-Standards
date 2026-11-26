from __future__ import annotations

import json
import os
import re
import sqlite3
import io
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import httpx
import pymupdf
from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field


load_dotenv()
DATABASE_PATH = Path(os.getenv("DATABASE_PATH", "./data/standards.db"))
app = FastAPI(title="工程规范随身查 API", version="0.1.0")
FRONTEND_FILE = Path(__file__).resolve().parent.parent / "static" / "index.html"


@app.get("/", include_in_schema=False)
def home() -> FileResponse:
    return FileResponse(FRONTEND_FILE)


@contextmanager
def db() -> Iterator[sqlite3.Connection]:
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS clauses (
            id INTEGER PRIMARY KEY,
            standard_title TEXT NOT NULL,
            clause_no TEXT NOT NULL,
            text TEXT NOT NULL,
            source_url TEXT NOT NULL DEFAULT '',
            page_no INTEGER,
            UNIQUE(standard_title, clause_no, text)
        )""")
        conn.commit()
        yield conn
    finally:
        conn.close()


class ClauseIn(BaseModel):
    standard_title: str = Field(min_length=1, max_length=300)
    clause_no: str = Field(min_length=1, max_length=100)
    text: str = Field(min_length=1, max_length=20000)
    source_url: str = ""
    page_no: int | None = Field(default=None, ge=1)


class QueryIn(BaseModel):
    question: str = Field(min_length=2, max_length=2000)
    top_k: int = Field(default=5, ge=1, le=10)


ARTICLE_PATTERN = re.compile(r"(第[一二三四五六七八九十百千万零〇两\d]+条)")
HEADING_PATTERN = re.compile(
    r"(?m)^[ \t]*((?:[一二三四五六七八九十]+[、.．])|(?:（[一二三四五六七八九十]+）)|(?:\d+[.、]))(?=\s*\S)"
)


def split_pdf_page(text: str, page_no: int) -> list[tuple[str, str]]:
    """Keep article markers when present; otherwise use page-local paragraph chunks."""
    text = re.sub(r"[ \t]+", " ", text).strip()
    if not text:
        return []
    matches = list(ARTICLE_PATTERN.finditer(text))
    if not matches:
        matches = list(HEADING_PATTERN.finditer(text))
    if matches:
        chunks = []
        for index, match in enumerate(matches):
            end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
            body = text[match.start() : end].strip()
            if body:
                chunks.append((match.group(1).strip(), body))
        # Preserve preamble content rather than silently dropping it.
        if matches[0].start() > 0:
            preamble = text[: matches[0].start()].strip()
            if len(preamble) > 30:
                chunks.insert(0, (f"第{page_no}页前言", preamble))
        return chunks
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]
    if not paragraphs:
        paragraphs = [text]
    return [(f"第{page_no}页片段{i + 1}", part) for i, part in enumerate(paragraphs)]


def terms(text: str) -> set[str]:
    """Small dependency-free tokenizer: words/numbers plus Chinese character bigrams."""
    words = set(re.findall(r"[a-zA-Z0-9]+", text.lower()))
    han = "".join(re.findall(r"[\u4e00-\u9fff]", text))
    words.update(han[i : i + 2] for i in range(max(0, len(han) - 1)))
    words.update(han)
    return words


def retrieve(conn: sqlite3.Connection, question: str, top_k: int) -> list[sqlite3.Row]:
    query_terms = terms(question)
    rows = conn.execute("SELECT * FROM clauses").fetchall()
    ranked = []
    for row in rows:
        content_terms = terms(f"{row['standard_title']} {row['clause_no']} {row['text']}")
        overlap = len(query_terms & content_terms)
        if overlap:
            ranked.append((overlap / max(1, len(query_terms) ** 0.5), row))
    ranked.sort(key=lambda item: item[0], reverse=True)
    return [row for _, row in ranked[:top_k]]


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "engineering-standards-rag"}


@app.post("/clauses", status_code=201)
def add_clause(item: ClauseIn) -> dict[str, object]:
    with db() as conn:
        cursor = conn.execute(
            "INSERT OR IGNORE INTO clauses (standard_title, clause_no, text, source_url, page_no) VALUES (?, ?, ?, ?, ?)",
            (item.standard_title, item.clause_no, item.text, item.source_url, item.page_no),
        )
        conn.commit()
        result = conn.execute(
            "SELECT id FROM clauses WHERE standard_title=? AND clause_no=? AND text=?",
            (item.standard_title, item.clause_no, item.text),
        ).fetchone()
    return {"id": result["id"], "created": cursor.rowcount == 1}


@app.post("/documents/upload", status_code=201)
async def upload_pdf(
    file: UploadFile = File(...),
    standard_title: str = Form(..., min_length=1, max_length=300),
    source_url: str = Form(default=""),
    replace_existing: bool = Form(default=False),
) -> dict[str, object]:
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="请上传 PDF 文件")
    raw = await file.read()
    if len(raw) > 30 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="PDF 大小不能超过 30 MB")
    if not raw.startswith(b"%PDF"):
        raise HTTPException(status_code=400, detail="文件内容不是有效的 PDF")
    try:
        document = pymupdf.open(stream=io.BytesIO(raw), filetype="pdf")
        page_chunks = [
            (page_number, clause_no, text)
            for page_number, page in enumerate(document, start=1)
            for clause_no, text in split_pdf_page(page.get_text("text"), page_number)
        ]
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"PDF 解析失败：{exc}") from exc
    if not page_chunks:
        raise HTTPException(status_code=422, detail="未提取到可检索文字；该 PDF 可能是扫描图片版，当前版本尚不支持 OCR")

    with db() as conn:
        removed = 0
        if replace_existing:
            cursor = conn.execute("DELETE FROM clauses WHERE standard_title = ?", (standard_title,))
            removed = cursor.rowcount
        before = conn.total_changes
        conn.executemany(
            "INSERT OR IGNORE INTO clauses (standard_title, clause_no, text, source_url, page_no) VALUES (?, ?, ?, ?, ?)",
            [(standard_title, clause_no, text, source_url, page_no) for page_no, clause_no, text in page_chunks],
        )
        conn.commit()
        inserted = conn.total_changes - before
    return {
        "filename": file.filename,
        "pages": len(document),
        "chunks_found": len(page_chunks),
        "chunks_added": inserted,
        "old_chunks_removed": removed,
        "source_url": source_url,
        "message": "PDF 已解析并入库；请抽查条文切分和页码后再用于正式查询。",
    }


@app.get("/clauses")
def list_clauses(limit: int = 100) -> dict[str, object]:
    if not 1 <= limit <= 500:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 500")
    with db() as conn:
        rows = conn.execute("SELECT * FROM clauses ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return {"items": [dict(row) for row in rows]}


@app.post("/query")
async def query(payload: QueryIn) -> dict[str, object]:
    with db() as conn:
        matches = retrieve(conn, payload.question, payload.top_k)
    if not matches:
        return {"answer": "当前已收录的规范资料中没有检索到可靠依据。", "citations": [], "verified": None}

    citations = [
        {
            "id": row["id"], "standard_title": row["standard_title"],
            "clause_no": row["clause_no"], "quote": row["text"],
            "source_url": row["source_url"], "page_no": row["page_no"],
        }
        for row in matches
    ]
    api_key = os.getenv("LLM_API_KEY", "")
    base_url = os.getenv("LLM_BASE_URL", "").rstrip("/")
    model = os.getenv("LLM_MODEL", "")
    if not (api_key and base_url and model):
        return {
            "answer": "已检索到相关条文。请配置 LLM_API_KEY、LLM_BASE_URL 和 LLM_MODEL 后启用归纳回答。",
            "citations": citations, "verified": None, "llm_enabled": False,
        }

    evidence = "\n\n".join(
        f"[{i}] {c['standard_title']} 第{c['clause_no']}条：{c['quote']}"
        for i, c in enumerate(citations, start=1)
    )
    prompt = (
        "你是工程规范检索助手。仅依据给定条文回答；资料不足时明确说不足。"
        "每个事实结论后标注引用编号，如[1]。不要创造条文号或引用。\n\n"
        f"问题：{payload.question}\n\n检索条文：\n{evidence}"
    )
    try:
        async with httpx.AsyncClient(timeout=45) as client:
            response = await client.post(
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={"model": model, "temperature": 0, "messages": [{"role": "user", "content": prompt}]},
            )
            response.raise_for_status()
            answer = response.json()["choices"][0]["message"]["content"]
    except (httpx.HTTPError, KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=502, detail=f"LLM API request failed: {exc}") from exc

    used_ids = {int(value) for value in re.findall(r"\[(\d+)\]", answer)}
    valid_ids = set(range(1, len(citations) + 1))
    verified = bool(used_ids) and used_ids <= valid_ids
    return {"answer": answer, "citations": citations, "verified": verified, "llm_enabled": True}
