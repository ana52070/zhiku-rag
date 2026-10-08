import io
import zipfile
from pathlib import Path

import openpyxl
from docx import Document
from markitdown import MarkItDown
from pypdf import PdfReader

MAX_UPLOAD = 50 * 1024 * 1024
CHUNK_SIZE = 800
CHUNK_OVERLAP = 120
MAX_CHUNKS = 10000

SUPPORTED_EXTENSIONS = {".txt", ".md", ".pdf", ".docx", ".xlsx", ".pptx", ".csv"}


def split_text(text):
    return [text[start:start + CHUNK_SIZE] for start in range(0, len(text), CHUNK_SIZE - CHUNK_OVERLAP) if text[start:start + CHUNK_SIZE].strip()]


def _extract_xlsx(content):
    workbook = openpyxl.load_workbook(io.BytesIO(content), data_only=True, read_only=True)
    blocks = []
    for name in workbook.sheetnames:
        sheet = workbook[name]
        raw_rows = []
        max_cols = 0
        for row in sheet.iter_rows(values_only=True):
            if not any(cell is not None and str(cell).strip() for cell in row):
                continue
            cells = [str(cell).replace("\n", " ").strip() if cell is not None else "" for cell in row]
            while cells and not cells[-1]:
                cells.pop()
            if cells:
                max_cols = max(max_cols, len(cells))
                raw_rows.append(cells)
        if raw_rows:
            formatted_rows = []
            for r in raw_rows:
                padded = r + [""] * (max_cols - len(r))
                formatted_rows.append("| " + " | ".join(padded) + " |")
            sep = "| " + " | ".join(["---"] * max_cols) + " |"
            table = formatted_rows[0] + "\n" + sep + "\n" + "\n".join(formatted_rows[1:]) if len(formatted_rows) > 1 else formatted_rows[0]
            blocks.append(f"## {name}\n\n{table}")
    workbook.close()
    return "\n\n".join(blocks)


def extract_text(filename, content):
    suffix = Path(filename).suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        raise ValueError("仅支持 TXT、Markdown、文字 PDF、Word (DOCX)、Excel (XLSX)、PPT (PPTX) 和 CSV 文件。")
    if len(content) > MAX_UPLOAD:
        raise ValueError("文件超过 50 MiB，请拆分后上传。")
    try:
        if suffix in {".txt", ".md"}:
            text = content.decode("utf-8-sig")
        elif suffix == ".csv":
            decoded = None
            for enc in ("utf-8-sig", "gb18030", "gbk"):
                try:
                    decoded = content.decode(enc)
                    break
                except UnicodeDecodeError:
                    continue
            if decoded is None:
                raise ValueError("CSV 编码无法识别，请另存为 UTF-8 或 GBK 后上传。")
            md = MarkItDown()
            result = md.convert_stream(io.BytesIO(decoded.encode("utf-8")), file_extension=".csv")
            text = result.text_content
        elif suffix == ".pdf":
            reader = PdfReader(io.BytesIO(content))
            if reader.is_encrypted:
                raise ValueError("请先解除 PDF 密码保护。")
            if len(reader.pages) > 1000:
                raise ValueError("PDF 超过 1000 页，请拆分后上传。")
            text = "\n\n".join(page.extract_text() or "" for page in reader.pages)
        elif suffix == ".docx":
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                if sum(item.file_size for item in archive.infolist()) > 200 * 1024 * 1024:
                    raise ValueError("DOCX 解压后超过 200 MiB，请拆分后上传。")
            document = Document(io.BytesIO(content))
            paragraphs = [paragraph.text for paragraph in document.paragraphs]
            for table in document.tables:
                paragraphs.extend(" | ".join(cell.text for cell in row.cells) for row in table.rows)
            text = "\n".join(paragraphs)
        elif suffix == ".xlsx":
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                if sum(item.file_size for item in archive.infolist()) > 200 * 1024 * 1024:
                    raise ValueError("Excel 解压后超过 200 MiB，请拆分后上传。")
            try:
                md = MarkItDown()
                result = md.convert_stream(io.BytesIO(content), file_extension=".xlsx")
                text = result.text_content
            except Exception:
                text = _extract_xlsx(content)
        elif suffix == ".pptx":
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                if sum(item.file_size for item in archive.infolist()) > 200 * 1024 * 1024:
                    raise ValueError("PPT 解压后超过 200 MiB，请拆分后上传。")
            md = MarkItDown()
            result = md.convert_stream(io.BytesIO(content), file_extension=".pptx")
            text = result.text_content
        else:
            raise ValueError("不支持的文件格式。")
    except UnicodeDecodeError:
        raise ValueError("文本编码无法识别，请另存为 UTF-8 后上传。") from None
    except ValueError:
        raise
    except Exception:
        raise ValueError("文件无法解析，请检查格式、文件完整性或密码保护。") from None
    text = text.replace("\x00", "").strip()
    if not text:
        if suffix == ".pdf":
            raise ValueError("未提取到文字；扫描版 PDF 需先进行文字识别。")
        raise ValueError("未提取到有效文字，请检查文件内容。")
    if len(text) > (CHUNK_SIZE - CHUNK_OVERLAP) * MAX_CHUNKS:
        raise ValueError("文件文字超过处理上限，请拆分后上传。")
    return suffix, text
