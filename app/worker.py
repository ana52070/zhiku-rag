"""Python 离线文档解析与预览微服务 Worker
供 Rust 主控核心调用：提取 Office/PDF/Markdown/文本与本地 OCR，以及 Office 预览渲染。
"""
import io
import json
import os
import tempfile
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile, Header, Body
from fastapi.responses import HTMLResponse, JSONResponse, Response

from .documents import extract_document
from .ocr import engine_status
from .office_converter import needs_pdf_preview, word_pdf
from .preview import render_preview

worker_app = FastAPI(title="联大研发部知识平台 - 文档解析 Worker")
app = worker_app


def check_worker_token(token):
    import hmac
    expected = os.environ.get('RAG_WORKER_TOKEN', '')
    if not expected or not hmac.compare_digest(expected, token or ''):
        raise HTTPException(status_code=401, detail='内部 Worker 令牌无效。')


@worker_app.post('/smb/{action}')
def smb_action(action: str, payload: dict = Body(...), x_worker_token: Optional[str] = Header(None)):
    check_worker_token(x_worker_token)
    if action not in ('test', 'scan', 'read'):
        raise HTTPException(status_code=404, detail='SMB 操作不存在。')
    from .smb_source import connect
    try:
        with connect(payload) as source:
            if action == 'read':
                content = source.read(payload.get('path', ''), payload.get('hash'))
                return Response(content=content, media_type='application/octet-stream')
            if action == 'scan':
                return source.scan()
            # 实际列出根目录，确认账号具有列目录权限。
            from contextlib import closing
            with closing(source.fs.scandir(source.root, **source.kwargs)) as entries:
                directories = [entry.name for entry in entries if entry.is_dir(follow_symlinks=False)]
            return {'connected': True, 'directories': sorted(directories)}
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error))
    except Exception:
        # SMB 库错误可能含连接参数；禁止将凭据或原始异常输出到响应和日志。
        raise HTTPException(status_code=502, detail='无法完整访问共享：请检查连接、账号权限及文件是否正在写入；本次不执行删除。')


@worker_app.post('/local/{action}')
def local_action(action: str, payload: dict = Body(...), x_worker_token: Optional[str] = Header(None)):
    check_worker_token(x_worker_token)
    if action not in ('test', 'scan', 'read'):
        raise HTTPException(status_code=404, detail='本地来源操作不存在。')
    from .local_source import LocalSource
    try:
        source = LocalSource(payload)
        if action == 'read':
            return Response(content=source.read(payload.get('path', ''), payload.get('hash')),
                            media_type='application/octet-stream')
        return source.scan() if action == 'scan' else source.test()
    except (ValueError, FileNotFoundError) as error:
        raise HTTPException(status_code=400, detail=str(error) if isinstance(error, ValueError) else '本地来源文件不存在，取消本次同步。')
    except Exception:
        raise HTTPException(status_code=502, detail='无法完整读取本地来源，本次不执行删除。')


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
        html = render_preview(doc, io.BytesIO(content))
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

        if suffix == '.docx' and needs_pdf_preview(io.BytesIO(content)):
            if not document_id or len(document_id) > 100 or not all(c.isascii() and (c.isalnum() or c in '_-') for c in document_id):
                raise ValueError('文档标识无效。')
            # 转换器按源内容 SHA-256 缓存，更新原文件后不会命中旧 PDF。
            with tempfile.TemporaryDirectory(prefix='zhiku-preview-source-') as directory:
                source_path = Path(directory) / 'document.docx'
                source_path.write_bytes(content)
                pdf_bytes = word_pdf(source_path, cache_path, document_id).read_bytes()

            return Response(
                content=pdf_bytes,
                media_type="application/pdf",
                headers={"X-Preview-Mode": "pdf"},
            )

        # 默认作为文本/源文件输出
        return Response(content=content, media_type="text/plain; charset=utf-8")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
