'use strict';

function operationId() {
  return [...crypto.getRandomValues(new Uint8Array(16))].map(n => n.toString(16).padStart(2, '0')).join('');
}

function renderOperation(progress, context) {
  $('#operation-progress').hidden = false;
  $('#task-title').textContent = context.title;
  const done = context.batch ? context.index + progress.completed : progress.completed;
  const total = context.batch ? context.total : progress.total;
  const ended = progress.state !== 'running';
  $('#task-overall').value = total ? done / total * 100 : 0;
  $('#task-summary').textContent = `已处理 ${done}/${total || '?'} 份${ended ? progress.state === 'failed' ? ' · 需检查' : ' · 完成' : ' · 处理中'}`;
  $('#task-file').textContent = progress.filename || context.filename || '';
  const names = {queued: '等待处理', transfer: '传输文件', parsing: '解析文档', ocr: '识别图片文字', embedding: '生成向量', saving: '保存索引', done: progress.state === 'failed' ? '部分处理失败，请查看文件列表' : '处理完成'};
  let label = names[progress.stage] || '处理中';
  if (progress.stage === 'ocr') label += ` · 已处理 ${progress.current} 张图片`;
  else if (progress.stage_total) label += ` · ${progress.current}/${progress.stage_total}${progress.stage === 'embedding' ? ' 个片段' : ''}`;
  $('#task-stage-label').textContent = progress.error || label;
  $('#task-stage').max = progress.stage_total || 1;
  if (progress.stage_total) $('#task-stage').value = progress.current;
  else if (ended) $('#task-stage').value = 1;
  else $('#task-stage').removeAttribute('value');
  $('#operation-progress').classList.toggle('has-error', progress.state === 'failed');
  $('#task-elapsed').textContent = `已用时 ${Math.floor((Date.now() - context.started) / 1000)} 秒`;
}

function uploadWithProgress(path, body, changed) {
  return new Promise((resolve, reject) => {
    const request = new XMLHttpRequest();
    request.open('POST', path);
    request.setRequestHeader('Authorization', `Bearer ${state.token}`);
    request.upload.onprogress = event => {
      if (event.lengthComputable) changed(event.loaded, event.total);
    };
    request.onerror = () => reject(new Error('上传连接中断，请刷新检查文件是否已保存后再重试。'));
    request.onabort = () => reject(new Error('上传连接已中断，请刷新检查文件状态。'));
    request.onload = () => {
      let payload;
      try { payload = JSON.parse(request.responseText); } catch { payload = {}; }
      if (request.status === 401 && !$('#auth-dialog').open) $('#auth-dialog').showModal();
      if (request.status < 200 || request.status >= 300) reject(new Error(payload.detail || `操作失败，HTTP ${request.status}。`));
      else resolve(payload);
    };
    request.send(body);
  });
}

async function trackedOperation(path, options, context) {
  const id = operationId();
  context = {...context, started: context.started || Date.now()};
  let progress = {state: 'running', stage: options.body instanceof FormData ? 'transfer' : 'queued', total: context.total || 0, completed: 0, current: 0, stage_total: null};
  let stopped = false;
  let timer;
  let serverSeen = false;
  renderOperation(progress, context);
  async function poll() {
    try {
      const response = await fetch(`/api/operations/${id}`, {headers: {Authorization: `Bearer ${state.token}`}});
      if (response.ok && !stopped) {
        const next = await response.json();
        if (!stopped) { progress = next; serverSeen = true; renderOperation(progress, context); }
      } else if (!stopped) {
        renderOperation(progress, context);
      }
    } catch {
      if (!stopped) $('#task-stage-label').textContent = '进度连接暂不可用 · 操作仍在等待服务器响应';
    }
    if (!stopped) timer = setTimeout(poll, 800);
  }
  timer = setTimeout(poll, 200);
  const url = `${path}${path.includes('?') ? '&' : '?'}operation_id=${id}`;
  try {
    const result = options.body instanceof FormData ? await uploadWithProgress(url, options.body, (loaded, total) => {
      if (!serverSeen) {
        progress = {...progress, current: loaded, stage_total: total};
        renderOperation(progress, context);
        $('#task-stage-label').textContent = `传输文件 · ${Math.floor(loaded / total * 100)}%${loaded === total ? ' · 等待服务器处理' : ''}`;
      }
    }) : await api(url, options);
    stopped = true; clearTimeout(timer);
    try {
      progress = await api(`/api/operations/${id}`);
    } catch {
      progress = {...progress, state: result.status === 'failed' || result.failed_count ? 'failed' : 'complete', stage: 'done', completed: context.batch ? 1 : context.total || progress.total};
    }
    renderOperation(progress, context);
    return result;
  } catch (error) {
    stopped = true; clearTimeout(timer);
    renderOperation({...progress, state: 'failed', error: error.message}, context);
    throw error;
  }
}
