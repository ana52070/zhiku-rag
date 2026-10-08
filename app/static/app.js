'use strict';
const $ = (selector) => document.querySelector(selector);
const state = {token: '', status: null, documents: [], busy: false, page: 'library', preview: null, deleting: null};
const labels = {pending: '待索引', processing: '处理中', ready: '可检索', stale: '需要重建', failed: '处理失败'};
let toastTimer;
const escapeHtml = value => String(value).replace(/[&<>"']/g, char => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[char]));

function toast(message, failed = false) {
  const box = $('#toast');
  box.textContent = message;
  box.classList.toggle('error', failed);
  box.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { box.hidden = true; }, failed ? 9000 : 4500);
}

async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (state.token) headers.set('Authorization', `Bearer ${state.token}`);
  if (options.body && !(options.body instanceof FormData)) {
    headers.set('Content-Type', 'application/json');
    options.body = JSON.stringify(options.body);
  }
  let response;
  try { response = await fetch(path, {...options, headers}); }
  catch { throw new Error('无法连接服务器，请检查服务是否启动，然后点击右上角刷新。'); }
  const payload = await response.json().catch(() => ({}));
  if (response.status === 401) {
    if (!$('#auth-dialog').open) $('#auth-dialog').showModal();
    throw new Error('需要管理员访问令牌，请在弹窗中填写。');
  }
  if (!response.ok) throw new Error(payload.detail || `操作失败，HTTP ${response.status}。`);
  return payload;
}

function showPage(page) {
  state.page = page;
  document.querySelectorAll('.page').forEach(section => { section.hidden = section.id !== `page-${page}`; });
  document.querySelectorAll('[data-page]').forEach(button => {
    const active = button.dataset.page === page;
    button.classList.toggle('active', active);
    if (active) button.setAttribute('aria-current', 'page'); else button.removeAttribute('aria-current');
  });
  $('#breadcrumb').textContent = `研发部 / ${{library: '知识库', search: '检索试验', models: '模型配置', service: '服务与接入', keys: 'API Key 授权'}[page]}`;
  location.hash = page;
}

function renderStatus() {
  const status = state.status;
  if (!status) return;
  $('#nav-count').textContent = status.document_count;
  $('#document-count').textContent = status.document_count;
  $('#ready-count').textContent = status.ready_count;
  $('#chunk-count').textContent = status.chunk_count;
  $('#side-state').textContent = status.enabled ? '业务运行中' : '业务已暂停';
  $('#connection-state').textContent = status.enabled ? '已连接 · 运行中' : '已连接 · 已暂停';
  $('#side-dot').className = `dot ${status.enabled ? '' : 'paused'}`;
  $('#service-dot').className = `dot ${status.enabled ? '' : 'paused'}`;
  $('#service-heading').textContent = status.enabled ? '知识库业务正在运行' : '知识库业务已暂停';
  $('#service-toggle').textContent = status.enabled ? '暂停业务' : '恢复业务';
  $('#service-toggle').disabled = state.busy;
  $('#setup-notice').hidden = status.config.configured;
  $('#stale-notice').hidden = !status.stale_count;
  $('#file-input').disabled = !status.enabled || state.busy;
  $('#drop-zone').classList.toggle('disabled', !status.enabled || state.busy);
  document.querySelectorAll('[data-action="reindex-all"]').forEach(button => { button.disabled = !status.enabled || !status.config.configured || state.busy || !status.document_count; });
  $('#search-submit').disabled = !status.enabled || state.busy;
  $('#model-configured').textContent = status.config.configured ? `当前模型：${status.config.model}` : '尚未配置';
  $('#mcp-url').value = `${location.origin}/mcp/`;
  $('#mcp-auth-description').textContent = status.mcp_auth ? '已启用 MCP 访问保护。客户端请求需携带 Authorization: Bearer <RAG_MCP_TOKEN>。' : '当前未设置 MCP 令牌，仅适合本机访问。对外提供服务前请设置 RAG_MCP_TOKEN。';
  $('#auth-description').textContent = status.admin_auth ? '管理员令牌保护已开启。MCP 使用独立令牌；对外部署还需设置允许访问的主机名及 HTTPS。' : '当前未设置管理员令牌。默认 Docker 仅发布到本机；内网或公网访问前请设置管理员令牌和 MCP 令牌。';
}

function renderFiles() {
  const filter = $('#file-filter').value.toLocaleLowerCase();
  const files = state.documents.filter(item => item.library_id === catalogState.library && (item.folder_id || null) === catalogState.folder && item.filename.toLocaleLowerCase().includes(filter));
  const container = $('#file-list');
  if (!files.length) {
    container.innerHTML = `<div class="quiet-empty"><svg class="icon"><use href="#i-file"/></svg><h3>${state.documents.length ? '没有匹配的文件' : '知识库从第一份文件开始'}</h3><p>${state.documents.length ? '换一个文件名称试试。' : '把常用资料放进来，之后就能按意思检索，而不必记住文件名。'}</p></div>`;
    return;
  }
  container.innerHTML = files.map(file => {
    const disabled = state.busy ? 'disabled' : '';
    const disabledIndex = state.busy || !state.status?.enabled || !state.status?.config.configured ? 'disabled' : '';
    const size = file.size < 1024 ? `${file.size} B` : file.size < 1024 * 1024 ? `${(file.size / 1024).toFixed(1)} KiB` : `${(file.size / 1024 / 1024).toFixed(1)} MiB`;
    const downloadUrl = `/api/documents/${file.id}/download`;
    return `<article class="file-row"><span class="file-type"><svg class="icon"><use href="#i-file"/></svg></span><div class="file-information"><h3 class="file-name">${escapeHtml(file.filename)}</h3><div class="file-details"><span>${size}</span><span>${file.chunk_count} 个片段</span><span>${new Date(file.created_at).toLocaleDateString('zh-CN')}</span><span class="badge ${escapeHtml(file.status)}">${labels[file.status] || escapeHtml(file.status)}</span>${file.enabled ? '' : '<span class="badge disabled">已禁用</span>'}</div>${file.error ? `<p class="file-error">${escapeHtml(file.error)}</p>` : ''}</div><div class="file-controls"><a class="text-button" href="${downloadUrl}" download="${escapeHtml(file.filename)}" target="_blank" style="text-decoration:none;">下载</a><button class="text-button" data-file-action="move" data-id="${file.id}" ${disabled}>移动</button><button class="text-button" data-file-action="preview" data-id="${file.id}" ${disabled} ${!file.enabled || !state.status?.enabled ? 'disabled' : ''}>原文</button><button class="text-button" data-file-action="reindex" data-id="${file.id}" ${disabledIndex}>重建</button><button class="text-button" data-file-action="toggle" data-id="${file.id}" ${disabled}>${file.enabled ? '禁用' : '启用'}</button><button class="text-button delete-button" data-file-action="delete" data-id="${file.id}" ${disabled}>删除</button></div></article>`;
  }).join('');
}

async function refresh(fillConfig = false) {
  try {
    const [status, documents, libraries, folders, keys] = await Promise.all([api('/api/status'), api('/api/documents'), api('/api/libraries'), api('/api/folders'), api('/api/keys')]);
    state.status = status;
    state.documents = documents.documents;
    catalogState.libraries = libraries.libraries;
    catalogState.folders = folders.folders;
    catalogState.keys = keys.keys;
    $('#global-alert').hidden = true;
    renderStatus(); renderCatalog(); renderFiles(); renderKeys();
    if (fillConfig) {
      $('#base-url').value = status.config.base_url;
      $('#model-name').value = status.config.model;
      $('#api-key').value = '';
      $('#clear-key').checked = false;
    }
    $('#key-state').textContent = status.config.has_api_key ? '已保存，留空保留' : '未设置';
    return true;
  } catch (error) {
    $('#global-alert').textContent = error.message;
    $('#global-alert').hidden = false;
    $('#connection-state').textContent = '未连接';
    $('#side-state').textContent = '需要重新连接';
    $('#side-dot').className = 'dot offline';
    return false;
  }
}

async function busyTask(task) {
  if (state.busy) { toast('有操作正在进行，请等待完成。'); return; }
  state.busy = true; renderStatus(); renderFiles();
  try { await task(); }
  catch (error) { toast(error.message, true); }
  finally { state.busy = false; await refresh(); }
}

async function uploadFiles(files) {
  if (!files.length) return;
  await busyTask(async () => {
    $('#upload-progress').hidden = false;
    let successful = 0;
    let failed = 0;
    const messages = [];
    for (const [index, file] of [...files].entries()) {
      $('#upload-progress').textContent = `正在处理 ${index + 1}/${files.length}：${file.name}。生成索引可能需要一些时间。`;
      try {
        if (file.size > 50 * 1024 * 1024) throw new Error(`${file.name} 超过 50 MiB，请拆分后上传。`);
        const body = new FormData(); body.append('file', file);
        const params = new URLSearchParams({library_id: catalogState.library});
        if (catalogState.folder) params.set('folder_id', catalogState.folder);
        const document = await api(`/api/documents?${params}`, {method: 'POST', body});
        if (document.status === 'failed') { failed++; messages.push(`${document.filename}：${document.error}`); }
        else successful++;
      } catch (error) { failed++; messages.push(error.message); }
    }
    $('#upload-progress').textContent = `完成：${successful} 份文件已保存${failed ? `，${failed} 份需检查。${messages.join('；')}` : '。'}${!state.status?.config.configured ? ' 配置模型后，请重建索引。' : ''}`;
    toast(failed ? '部分文件处理失败，请查看上传区和文件列表。' : '文件已保存。', !!failed);
    $('#file-input').value = '';
  });
}

async function loadPreview(documentId, offset = 0) {
  const data = await api(`/api/documents/${documentId}/content?offset=${offset}`);
  state.preview = {id: documentId, next: data.next_offset};
  $('#preview-name').textContent = data.filename;
  $('#preview-meta').textContent = `共 ${data.total_characters.toLocaleString()} 字符；正在显示 ${offset + 1}–${Math.min(offset + 8000, data.total_characters)}。`;
  $('#preview-content').textContent = data.text;
  $('#preview-next').hidden = data.next_offset === null;
  if (!$('#preview-dialog').open) $('#preview-dialog').showModal();
}

function modelPayload() {
  return {base_url: $('#base-url').value.trim(), model: $('#model-name').value.trim(), api_key: $('#api-key').value.trim(), clear_api_key: $('#clear-key').checked};
}

document.querySelectorAll('[data-page]').forEach(button => button.addEventListener('click', () => showPage(button.dataset.page)));
document.querySelectorAll('[data-go]').forEach(button => button.addEventListener('click', () => showPage(button.dataset.go)));
document.querySelectorAll('[data-close]').forEach(button => button.addEventListener('click', () => $(`#${button.dataset.close}`).close()));
$('#refresh-button').addEventListener('click', () => refresh());
$('#auth-button').addEventListener('click', () => $('#auth-dialog').showModal());
$('#auth-form').addEventListener('submit', async event => {
  event.preventDefault(); state.token = $('#admin-token').value.trim(); $('#admin-token').value = '';
  $('#auth-dialog').close(); if (await refresh(true)) toast('已连接工作台。');
});
$('#file-filter').addEventListener('input', renderFiles);
$('#file-input').addEventListener('change', event => uploadFiles(event.target.files));
const drop = $('#drop-zone');
for (const name of ['dragenter', 'dragover']) drop.addEventListener(name, event => { event.preventDefault(); if (!state.busy) drop.classList.add('dragging'); });
for (const name of ['dragleave', 'drop']) drop.addEventListener(name, event => { event.preventDefault(); drop.classList.remove('dragging'); });
drop.addEventListener('drop', event => { if (state.status?.enabled && !state.busy) uploadFiles(event.dataTransfer.files); else toast('上传暂不可用，请恢复业务或等待当前操作完成。', true); });
document.querySelectorAll('[data-action="reindex-all"]').forEach(button => button.addEventListener('click', () => busyTask(async () => {
  toast('正在重建全部索引，请等待完成。');
  const result = await api('/api/reindex', {method: 'POST'});
  toast(result.failed_count ? `${result.failed_count} 份文件索引失败，请查看列表并重试。` : '全部文件索引已重建。', !!result.failed_count);
})));
$('#file-list').addEventListener('click', event => {
  const button = event.target.closest('[data-file-action]'); if (!button) return;
  const file = state.documents.find(item => item.id === button.dataset.id); if (!file) return;
  if (button.dataset.fileAction === 'delete') {
    state.deleting = file.id; $('#delete-description').textContent = file.filename; $('#delete-dialog').showModal();
  } else if (button.dataset.fileAction === 'move') openMove(file);
  else if (button.dataset.fileAction === 'preview') loadPreview(file.id).catch(error => toast(error.message, true));
  else busyTask(async () => {
    if (button.dataset.fileAction === 'toggle') {
      await api(`/api/documents/${file.id}`, {method: 'PATCH', body: {enabled: !file.enabled}} );
      toast(file.enabled ? '文件已停止参与检索。' : '文件已启用。');
    } else {
      const result = await api(`/api/documents/${file.id}/reindex`, {method: 'POST'});
      toast(result.status === 'failed' ? result.error : '文件索引已重建。', result.status === 'failed');
    }
  });
});
$('#confirm-delete').addEventListener('click', () => {
  const id = state.deleting; $('#delete-dialog').close();
  busyTask(async () => { await api(`/api/documents/${id}`, {method: 'DELETE'}); toast('文件及其索引已删除。'); });
});
$('#preview-next').addEventListener('click', () => { if (state.preview) loadPreview(state.preview.id, state.preview.next).catch(error => toast(error.message, true)); });
$('#model-form').addEventListener('submit', event => {
  event.preventDefault();
  busyTask(async () => {
    const result = await api('/api/config', {method: 'PUT', body: modelPayload()});
    $('#api-key').value = ''; $('#clear-key').checked = false; $('#model-test-result').hidden = true;
    toast(result.index_invalidated && state.documents.length ? '配置已保存，请重建文件索引。' : '模型配置已保存。');
  });
});
$('#test-model').addEventListener('click', async () => {
  if (!$('#model-form').reportValidity()) return;
  const button = $('#test-model'); button.disabled = true; button.textContent = '正在测试…';
  const box = $('#model-test-result'); box.hidden = false; box.classList.remove('failed'); box.textContent = '正在向接口发送一条测试文字，不会保存配置。';
  try { const result = await api('/api/config/test', {method: 'POST', body: modelPayload()}); box.textContent = `连接成功，返回 ${result.dimension} 维向量。请保存配置后上传或重建文件。`; }
  catch (error) { box.textContent = error.message; box.classList.add('failed'); }
  finally { button.disabled = false; button.textContent = '测试连接'; }
});
$('#search-form').addEventListener('submit', event => {
  event.preventDefault();
  busyTask(async () => {
    $('#search-submit').textContent = '正在检索…';
    try {
      const started = performance.now();
      const result = await api('/api/search', {method: 'POST', body: {query: $('#search-query').value, top_k: Number($('#top-k').value), library_id: $('#search-library').value || null}});
      $('#search-meta').textContent = `${result.results.length} 个片段，用时 ${((performance.now() - started) / 1000).toFixed(2)} 秒`;
      $('#search-results').innerHTML = result.results.length ? result.results.map(hit => {
        const downloadUrl = `/api/documents/${hit.document_id}/download`;
        return `<article class="result"><div class="result-header"><svg class="icon"><use href="#i-file"/></svg><h3>${escapeHtml(hit.filename)}</h3><span>片段 ${hit.chunk_index}</span><span class="similarity">相似度 ${hit.score.toFixed(3)}</span></div><p>${escapeHtml(hit.text)}</p><div style="margin-top:0.5rem;display:flex;gap:0.75rem;"><button class="text-button" data-preview-id="${hit.document_id}">查看原文</button><a class="text-button" href="${downloadUrl}" download="${escapeHtml(hit.filename)}" target="_blank" style="text-decoration:none;">下载源文件</a></div></article>`;
      }).join('') : '<div class="quiet-empty"><h3>暂无匹配的片段</h3><p>请先上传文件并完成索引，检查文件是否启用，以及模型变化后是否已重建。</p></div>';
    } finally { $('#search-submit').innerHTML = '<svg class="icon"><use href="#i-search"/></svg>开始检索'; }
  });
});
$('#search-results').addEventListener('click', event => { const button = event.target.closest('[data-preview-id]'); if (button) loadPreview(button.dataset.previewId).catch(error => toast(error.message, true)); });
$('#service-toggle').addEventListener('click', () => busyTask(async () => { const enabled = !state.status.enabled; await api('/api/service', {method: 'PUT', body: {enabled}}); toast(enabled ? '知识库业务已恢复。' : '知识库业务已暂停，管理入口仍可使用。'); }));
$('#copy-mcp').addEventListener('click', async () => {
  try { await navigator.clipboard.writeText($('#mcp-url').value); toast('MCP 地址已复制。'); }
  catch { $('#mcp-url').select(); toast('浏览器不允许自动复制，请复制已选中的地址。'); }
});
window.addEventListener('hashchange', () => { const page = location.hash.slice(1); if (['library', 'search', 'models', 'service', 'keys'].includes(page)) showPage(page); });
const initialPage = location.hash.slice(1);
showPage(['library', 'search', 'models', 'service', 'keys'].includes(initialPage) ? initialPage : 'library');

const catalogState = {library: 'default', folder: null, libraries: [], folders: [], keys: [], resource: null, moving: null, editingKey: null, revoking: null};
const libraryDescriptions = {default: '待整理的资料，从这里开始归档', faq: '培训资料、软件说明与调试常见问题', retrospective: '技术路线、需求判断与项目风险复盘', projects: '产品、项目清单与近似方案检索'};
function folderPath(folderId) {
  const names = []; const seen = new Set();
  while (folderId && !seen.has(folderId)) {
    seen.add(folderId); const folder = catalogState.folders.find(f => f.id === folderId); if (!folder) break;
    names.unshift(folder.name); folderId = folder.parent_id;
  }
  return names.join(' / ');
}
function libraryOptions() { return catalogState.libraries.map(lib => `<option value="${escapeHtml(lib.id)}">${escapeHtml(lib.name)}</option>`).join(''); }
function renderCatalog() {
  $('#library-cards').innerHTML = catalogState.libraries.map(lib => `<button class="library-card ${lib.id === catalogState.library ? 'selected' : ''}" data-library="${escapeHtml(lib.id)}" aria-pressed="${lib.id === catalogState.library}"><span class="card-icon"><svg class="icon"><use href="#i-book"/></svg></span><strong>${escapeHtml(lib.name)}</strong><span>${escapeHtml(libraryDescriptions[lib.id] || '独立管理的知识资料')}</span><small>${lib.document_count} 份文件<span>${lib.id === catalogState.library ? '当前知识库' : '打开知识库 →'}</span></small></button>`).join('');
  const library = catalogState.libraries.find(lib => lib.id === catalogState.library);
  $('#collection-title').textContent = library?.name || '知识库';
  const path = folderPath(catalogState.folder);
  $('#upload-location').textContent = `上传到：${library?.name || ''}${path ? ' / ' + path : ' / 根目录'}`;
  const chain = []; let folderId = catalogState.folder;
  while (folderId) { const f = catalogState.folders.find(item => item.id === folderId); if (!f) break; chain.unshift(f); folderId = f.parent_id; }
  $('#folder-breadcrumb').innerHTML = `<button class="text-button" data-folder="">根目录</button>` + chain.map(f => `<span>/</span><button class="text-button" data-folder="${f.id}">${escapeHtml(f.name)}</button>`).join('');
  $('#delete-folder').hidden = !catalogState.folder;
  $('#folder-list').innerHTML = catalogState.folders.filter(f => f.library_id === catalogState.library && f.parent_id === catalogState.folder).map(f => `<button class="folder-chip" data-folder="${f.id}"><svg class="icon"><use href="#i-book"/></svg>${escapeHtml(f.name)}<span>→</span></button>`).join('');
  const searchSelected = $('#search-library').value;
  $('#search-library').innerHTML = '<option value="">全部知识库</option>' + libraryOptions();
  if (catalogState.libraries.some(lib => lib.id === searchSelected)) $('#search-library').value = searchSelected;
}
function renderKeys() {
  $('#key-list').innerHTML = catalogState.keys.length ? catalogState.keys.map(key => {
    const scope = key.all_libraries ? '全部知识库（包含未来新建）' : key.library_ids.map(id => catalogState.libraries.find(lib => lib.id === id)?.name || '已删除的知识库').join('、');
    return `<article class="key-row"><span class="card-icon"><svg class="icon"><use href="#i-key"/></svg></span><div class="key-information"><h3>${escapeHtml(key.name)} <span class="badge ${key.revoked ? 'disabled' : ''}">${key.revoked ? '已撤销' : '有效'}</span></h3><p>${escapeHtml(scope)}</p><small>${escapeHtml(key.prefix)}… · ${new Date(key.created_at).toLocaleDateString('zh-CN')}</small></div>${key.revoked ? '' : `<div class="file-controls"><button class="text-button" data-key-edit="${key.id}">调整授权</button><button class="text-button" data-key-revoke="${key.id}">撤销</button></div>`}</article>`;
  }).join('') : '<div class="quiet-empty"><svg class="icon"><use href="#i-key"/></svg><h3>给第一个客户端创建访问凭证</h3><p>选择它能使用的知识库，不需要共享管理员令牌。</p></div>';
}
$('#library-cards').addEventListener('click', event => {
  const button = event.target.closest('[data-library]'); if (!button || state.busy) return;
  catalogState.library = button.dataset.library; catalogState.folder = null; $('#file-filter').value = ''; renderCatalog(); renderFiles();
});
for (const selector of ['#folder-list', '#folder-breadcrumb']) $(selector).addEventListener('click', event => {
  const button = event.target.closest('[data-folder]'); if (!button || state.busy) return;
  catalogState.folder = button.dataset.folder || null; renderCatalog(); renderFiles();
});
function openResource(kind) { catalogState.resource = kind; $('#resource-title').textContent = kind === 'library' ? '新建知识库' : '新建文件夹'; $('#resource-name').value = ''; $('#resource-dialog').showModal(); }
$('#new-library').addEventListener('click', () => openResource('library'));
$('#new-folder').addEventListener('click', () => openResource('folder'));
$('#resource-form').addEventListener('submit', event => {
  event.preventDefault(); const body = {name: $('#resource-name').value.trim()}; const isLibrary = catalogState.resource === 'library';
  if (!isLibrary) Object.assign(body, {library_id: catalogState.library, parent_id: catalogState.folder});
  busyTask(async () => { const result = await api(isLibrary ? '/api/libraries' : '/api/folders', {method: 'POST', body}); if (isLibrary) { catalogState.library = result.id; catalogState.folder = null; } $('#resource-dialog').close(); toast('已创建。'); });
});
$('#delete-folder').addEventListener('click', () => busyTask(async () => {
  const folder = catalogState.folders.find(f => f.id === catalogState.folder); if (!folder) return;
  await api(`/api/folders/${folder.id}`, {method: 'DELETE'}); catalogState.folder = folder.parent_id; toast('空文件夹已删除。');
}));
function renderMoveFolders() { $('#move-folder').innerHTML = '<option value="">根目录</option>' + catalogState.folders.filter(f => f.library_id === $('#move-library').value).map(f => `<option value="${f.id}">${escapeHtml(folderPath(f.id))}</option>`).join(''); }
function openMove(file) { catalogState.moving = file.id; $('#move-name').textContent = file.filename; $('#move-library').innerHTML = libraryOptions(); $('#move-library').value = file.library_id; renderMoveFolders(); $('#move-folder').value = file.folder_id || ''; $('#move-dialog').showModal(); }
$('#move-library').addEventListener('change', renderMoveFolders);
$('#move-form').addEventListener('submit', event => { event.preventDefault(); busyTask(async () => { await api(`/api/documents/${catalogState.moving}/location`, {method: 'PATCH', body: {library_id: $('#move-library').value, folder_id: $('#move-folder').value || null}}); $('#move-dialog').close(); toast('资料已移动，原索引和引用链接保持有效。'); }); });
function openKey(key = null) {
  catalogState.editingKey = key?.id || null; $('#key-title').textContent = key ? '调整授权' : '创建 API Key'; $('#key-name').value = key?.name || ''; $('#key-all').checked = key?.all_libraries ?? true;
  $('#key-scopes').innerHTML = catalogState.libraries.map(lib => `<label class="checkbox-label"><input type="checkbox" value="${lib.id}" ${key?.library_ids.includes(lib.id) ? 'checked' : ''}>${escapeHtml(lib.name)}</label>`).join(''); updateScopeControls(); $('#key-dialog').showModal();
}
function updateScopeControls() { $('#key-scopes').querySelectorAll('input').forEach(input => { input.disabled = $('#key-all').checked; }); $('#key-scopes').classList.toggle('muted-scopes', $('#key-all').checked); }
$('#key-all').addEventListener('change', updateScopeControls);
$('#new-key').addEventListener('click', () => openKey());
$('#key-list').addEventListener('click', event => {
  const edit = event.target.closest('[data-key-edit]'); if (edit) openKey(catalogState.keys.find(k => k.id === edit.dataset.keyEdit));
  const revoke = event.target.closest('[data-key-revoke]'); if (revoke) { catalogState.revoking = revoke.dataset.keyRevoke; $('#revoke-name').textContent = catalogState.keys.find(k => k.id === catalogState.revoking)?.name || ''; $('#revoke-dialog').showModal(); }
});
$('#key-form').addEventListener('submit', event => {
  event.preventDefault(); const editing = catalogState.editingKey;
  const body = {name: $('#key-name').value.trim(), all_libraries: $('#key-all').checked, library_ids: $('#key-all').checked ? [] : [...$('#key-scopes').querySelectorAll('input:checked')].map(input => input.value)};
  busyTask(async () => { const result = await api('/api/keys' + (editing ? '/' + editing : ''), {method: editing ? 'PATCH' : 'POST', body}); $('#key-dialog').close(); if (result.key) { $('#created-key').value = result.key; $('#secret-dialog').showModal(); } else toast('授权范围已更新。'); });
});
$('#secret-dialog').addEventListener('close', () => { $('#created-key').value = ''; });
$('#copy-key').addEventListener('click', async () => { try { await navigator.clipboard.writeText($('#created-key').value); toast('已复制。'); } catch { $('#created-key').select(); toast('请复制选中的 Key。'); } });
$('#confirm-revoke').addEventListener('click', () => busyTask(async () => { await api(`/api/keys/${catalogState.revoking}`, {method: 'DELETE'}); $('#revoke-dialog').close(); toast('API Key 已撤销。'); }));
document.addEventListener('click', async event => {
  const link = event.target.closest('a[download]'); if (!link) return;
  const target = new URL(link.href);
  // Blob 下载锚点必须交给浏览器；只拦截本服务的源文件接口。
  if (target.origin !== location.origin || !/^\/api\/documents\/[^/]+\/download$/.test(target.pathname)) return;
  event.preventDefault();
  try {
    const response = await fetch(link.getAttribute('href'), {headers: state.token ? {Authorization: `Bearer ${state.token}`} : {}});
    if (!response.ok) throw new Error('下载失败，请确认访问令牌和资料状态。');
    const url = URL.createObjectURL(await response.blob()); const anchor = document.createElement('a'); anchor.href = url; anchor.download = link.getAttribute('download'); document.body.append(anchor); anchor.click(); anchor.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000);
  } catch (error) { toast(error.message, true); }
});
refresh(true);
