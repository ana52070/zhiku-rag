"""复杂 Word 的按需 PDF 预览；原文件不变，转换缓存放在私有数据目录。"""
import hashlib
import shutil
import subprocess
import tempfile
import threading
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

_conversion_lock = threading.Lock()


def needs_pdf_preview(path):
    with zipfile.ZipFile(path) as archive:
        root=ET.fromstring(archive.read('word/document.xml'))
    return any('wordprocessingShape}' in element.tag or 'wordprocessingGroup}' in element.tag or element.tag in {'{urn:schemas-microsoft-com:vml}group','{urn:schemas-microsoft-com:vml}shape'} for element in root.iter())


def word_pdf(path, data_directory, identifier):
    command=shutil.which('soffice') or shutil.which('libreoffice')
    if not command: raise ValueError('复杂 Word 预览转换器未安装，请安装 LibreOffice Writer。')
    cache=Path(data_directory)/'preview-cache'; cache.mkdir(exist_ok=True)
    digest=hashlib.sha256(path.read_bytes()).hexdigest()
    destination=cache/(identifier+'-'+digest+'.pdf')
    if destination.parent.resolve()!=cache.resolve(): raise ValueError('转换缓存路径无效。')
    with _conversion_lock:
        if destination.exists(): return destination
        with tempfile.TemporaryDirectory(prefix='zhiku-word-') as directory:
            temporary=Path(directory); source=temporary/'document.docx'; shutil.copyfile(path,source)
            profile=temporary/'profile'; user=profile/'user'; user.mkdir(parents=True)
            (user/'registrymodifications.xcu').write_text('''<?xml version="1.0" encoding="UTF-8"?><oor:items xmlns:oor="http://openoffice.org/2001/registry"><item oor:path="/org.openoffice.Office.Common/Security/Scripting"><prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop></item></oor:items>''')
            try:
                result=subprocess.run([command,'--headless','--norestore','--nodefault','--nofirststartwizard','-env:UserInstallation='+profile.as_uri(),'--convert-to','pdf','--outdir',str(temporary),str(source)],capture_output=True,timeout=90)
            except subprocess.TimeoutExpired:
                raise ValueError('Word 预览转换超时，请下载源文件查看。') from None
            output=temporary/'document.pdf'
            if result.returncode or not output.exists(): raise ValueError('Word 预览转换失败，请检查文档或下载源文件。')
            staging=destination.with_suffix('.tmp'); shutil.copyfile(output,staging); staging.replace(destination)
    return destination
