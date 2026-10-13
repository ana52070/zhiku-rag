# 独立 HTTP 采集环境，不运行网站脚本，不持有知识库写入凭据。
FROM python:3.12-slim
WORKDIR /srv
COPY collectors/engineering_handbook/requirements.txt /srv/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY collectors /srv/collectors
ENV PYTHONUNBUFFERED=1 HANDBOOK_OUTPUT=/output
CMD ["python", "-m", "collectors.engineering_handbook.collector"]
