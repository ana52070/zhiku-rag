"""Python 离线文档解析与预览微服务 Worker
供 Rust 主控核心调用：提取 Office/PDF/Markdown/文本与本地 OCR，以及 Office 预览渲染。
"""
import io
import json
import os
import tempfile
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response

from .documents import extract_document
from .ocr import engine_status
from .office_converter import needs_pdf_preview, word_pdf
from .preview import render_preview

worker_app = FastAPI(title="联大研发部知识平台 - 文档解析 Worker")
app = worker_app


@worker_app.get("/health")
async def health():
    return {"status": "ok", "service": "doc-parser-worker"}


@worker_app.get("/ocr/status")
async def get_ocr_status():
    return engine_status()


@worker_app.post("/extract")
async def extract(file: UploadFile = File(...)):
    filename = file.filename or "unknown"
    content = await file.read()
    try:
        suffix, text, extraction_info = extract_document(filename, content)
        return {
            "suffix": suffix,
            "text": text,
            "extraction_info": extraction_info or {},
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@worker_app.post("/preview")
async def preview(
    document: Optional[str] = Form(None),
    document_json: Optional[str] = Form(None),
    file: UploadFile = File(...),
):
    raw_doc = document or document_json or "{}"
    try:
        doc = json.loads(raw_doc)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid document JSON")

    content = await file.read()
    try:
        html = render_preview(doc, content)
        return HTMLResponse(content=html)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@worker_app.post("/preview-source")
async def preview_source(
    document_id: str = Form(...),
    filename: str = Form(...),
    preview_cache_dir: Optional[str] = Form(None),
    file: UploadFile = File(...),
):
    content = await file.read()
    suffix = Path(filename).suffix.lower()

    if not preview_cache_dir:
        preview_cache_dir = tempfile.gettempdir()
    cache_path = Path(preview_cache_dir)
    cache_path.mkdir(parents=True, exist_ok=True)

    try:
        if suffix == ".pdf":
            return Response(content=content, media_type="application/pdf")

        if needs_pdf_preview(filename):
            cache_file = cache_path / f"{document_id}-{suffix.lstrip('.')}.pdf"
            if not cache_file.exists():
                pdf_bytes = word_pdf(content, suffix)
                cache_file.write_bytes(pdf_bytes)
            else:
                pdf_bytes = cache_file.read_bytes()

            return Response(
                content=pdf_bytes,
                media_type="application/pdf",
                headers={"X-Preview-Mode": "pdf"},
            )

        # 默认作为文本/源文件输出
        return Response(content=content, media_type="text/plain; charset=utf-8")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
