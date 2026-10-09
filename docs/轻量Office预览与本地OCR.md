# 轻量 Office 预览与本地 OCR

日期：2026-10-09。

## 问题与改动

原导航把接入放在创建 API Key 之前，现调整为先授权、后接入。

原知识库三项统计直接读取平台总数，切换知识库只更新列表。现改为当前知识库及全部子文件夹的文件总数；可检索数量和片段数量只包含该库中已启用、索引就绪的文件。侧边栏仍为平台文件总数。

原 Word/PPT 预览丢弃图片和布局。真实 VPS 调查显示 PPT 返回 59 字节 HTML、Word 返回 388 字节 HTML，虽然接口成功，内容不足。

- Word：固定版本 docx-preview 与 JSZip 静态组件，浏览器显示页面、图片、表格和页眉页脚，不访问运行时 CDN。
  真实 Word 含大量 DrawingML/VML 复杂绘图，组件不能完整呈现；经用户确认增加 LibreOffice Writer 按需转 PDF 的兜底。转换串行执行，完成后进程退出，缓存位于私有数据目录，原 DOCX 不变。
- Excel：工作表标签、行列标题、单元格尺寸、合并单元格、直接 RGB 样式、边框、保存的公式结果及内嵌图片。支持滚动浏览；大表明确提示预览为前 2000 行、150 列，原文件不截断。
- PPT：使用原幻灯片比例与形状坐标展示图片、文本和基础表格。
- PDF：仍使用原文件预览，未转换或替换原 PDF。

轻量预览不承诺与 Microsoft Office 完全一致；复杂图表、动画、主题样式和特殊对象可能无法完整显示。旧版 `.doc/.xls/.ppt` 仍需另存为 `.docx/.xlsx/.pptx`。

## OCR 与知识库

OCR 使用本地 Tesseract、简体中文与英文语言包。只识别文字，不分析图纸结构、曲线含义或图片中的物体。

- 扫描 PDF：将无原生文字的页面渲染后识别，结果标记页码。
- 混合 PDF：保留原生文字，对图片补充 OCR，标记页码和图片序号。
- Word/Excel/PPT：读取包内引用的图片，OCR 结果标记文档位置、工作表或幻灯片页码。
- 识别结果使用 `〔OCR · 来源〕` 标记，和原生文字一起分块、建立 embedding 索引。
- 单张图片超时 30 秒、每份文档最多处理 200 张图片；超限或识别失败显示“需检查”，不声称完整识别。
- 未识别到文字的图片不产生知识片段；低清晰度、手写、复杂底纹与特殊文字可能识别错误。
- 新上传自动解析；旧文件在重建索引时重新解析，原文件与 ID 保留，旧提取文字和向量由新的解析及索引替换。批量处理现有业务资料需与维护人确认。

文件列表显示 OCR 状态和异常，上传区域显示本地引擎是否就绪。临时 OCR 图片自动删除，原图片不会发送到 OCR 云服务；提取后的文字仍会发送到已配置的 embedding 接口。

## 部署与验证

VPS 实测为 2 核、约 1.57 GiB 内存，因此采用轻量预览和单线程本地 OCR，未安装完整 Office 服务。

Docker 镜像与 VPS 都安装 `tesseract-ocr`、`tesseract-ocr-chi-sim`、`tesseract-ocr-eng`、`libreoffice-writer`、`fonts-noto-cjk`。Python 依赖固定 Pillow 11.3.0、pypdfium2 5.13.0。PDFium 调用使用互斥锁，避免跨线程同时执行。

保持 `script-src 'self'`；Word 动态样式使用每次页面请求的 nonce，未允许内联脚本或任意内联样式。表格样式由本站脚本设置。

测试覆盖扫描 PDF 与 Office 图片 OCR 来源、识别失败保留原生文字、工作表切换与样式、幻灯片图片与坐标、Word 实际图片显示、分库统计和严格 CSP。真实 Tesseract 测试使用固定中英图片；本地未安装引擎时明确跳过，VPS 必须执行。

## 参考

- [docx-preview 源码与能力说明](https://github.com/VolodymyrBaydalka/docxjs)
- [Tesseract 官方安装与语言包](https://tesseract-ocr.github.io/tessdoc/Installation.html)
- [Tesseract 图像质量说明](https://tesseract-ocr.github.io/tessdoc/ImproveQuality.html)
- [pypdfium2 线程安全与 API](https://pypdfium2.readthedocs.io/en/stable/python_api.html)
