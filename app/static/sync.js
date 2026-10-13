'use strict';
const syncState = {tasks: [], editing: null, history: null, timer: null};
const syncLabels = {daily: '每日', weekly: '每周', interval: '固定间隔', cron: 'Cron'};
const syncDate = value => value ? new Date(value).toLocaleString('zh-CN', {hour12: false}) : '—';
const syncSourceLabel = task => task.source_kind === 'local' ? '本地目录：' + task.source_id : '\\\\' + task.server + '\\' + task.share + (task.subdir ? '\\' + task.subdir.replaceAll('/', '\\') : '');

function syncSourceFields() {
  const local = $('#sync-source-kind').value === 'local';
  $('#sync-local-field').hidden = !local;
  $('#sync-source-id').required = local;
  for (const key of ['address', 'server', 'share', 'subdir', 'port', 'username', 'password']) {
    const input = $('#sync-' + key);
    input.closest('label').hidden = local;
    input.disabled = local;
  }
}

function syncFeedback(message, failed = false) {
  const box = $('#sync-feedback'); box.hidden = false; box.textContent = message; box.classList.toggle('failed', failed);
}
async function syncLoad() {
  try {
    syncState.tasks = await api('/api/sync/tasks'); $('#sync-error').hidden = true;
    $('#sync-list').innerHTML = syncState.tasks.length ? syncState.tasks.map(task => `<article class="sync-card"><div><h3>${escapeHtml(task.name)}</h3><p class="sync-source">${escapeHtml(syncSourceLabel(task))}</p><p>${escapeHtml(catalogState.libraries.find(lib => lib.id === task.library_id)?.name || '知识库已删除')} / ${escapeHtml(task.folder_id ? folderPath(task.folder_id) : '根目录')}</p><p>${escapeHtml(syncLabels[task.schedule.kind])} · ${escapeHtml(task.schedule.timezone)} · ${task.enabled ? `下次 ${syncDate(task.next_run)}` : '定时已停用'}${task.running ? ' · 正在同步' : ''}</p></div><div class="sync-actions"><button class="secondary" data-sync-action="run" data-id="${task.id}" ${task.running ? 'disabled' : ''}>立即同步</button><button class="text-button" data-sync-action="edit" data-id="${task.id}" ${task.running ? 'disabled' : ''}>编辑</button><button class="text-button" data-sync-action="toggle" data-id="${task.id}" ${task.running ? 'disabled' : ''}>${task.enabled ? '停用定时' : '启用定时'}</button><button class="text-button" data-sync-action="runs" data-id="${task.id}">执行记录</button><button class="text-button delete-button" data-sync-action="delete" data-id="${task.id}" ${task.running ? 'disabled' : ''}>删除任务</button></div></article>`).join('') : '<div class="quiet-empty"><h3>暂无同步任务</h3><p>添加远端共享目录，选择知识库和执行时间。</p></div>';
  } catch (error) { $('#sync-error').hidden = false; $('#sync-error').textContent = error.message; }
}
function syncFields() {
  const kind = $('#sync-kind').value;
  $('#sync-time-field').hidden = !['daily', 'weekly'].includes(kind);
  $('#sync-weekdays').hidden = kind !== 'weekly';
  $('#sync-interval-field').hidden = kind !== 'interval'; $('#sync-cron-field').hidden = kind !== 'cron';
}
function syncFolders() {
  $('#sync-folder').innerHTML = '<option value="">根目录</option>' + catalogState.folders.filter(f => f.library_id === $('#sync-library').value).map(f => `<option value="${f.id}">${escapeHtml(folderPath(f.id))}</option>`).join('');
}
async function syncEdit(task) {
  await refresh();
  syncState.editing = task?.id || null;
  $('#sync-form').reset(); $('#sync-feedback').hidden = true; $('#sync-form').hidden = false;
  $('#sync-form-title').textContent = task ? '编辑同步任务' : '新建同步任务';
  $('#sync-source-kind').value = task?.source_kind || 'smb';
  $('#sync-source-id').value = task?.source_id || '';
  syncSourceFields();
  for (const key of ['name', 'server', 'share', 'subdir', 'username', 'port']) $('#sync-' + key).value = task?.[key] ?? (key === 'port' ? 445 : '');
  $('#sync-password').value = ''; $('#sync-password-note').textContent = task?.has_password ? '已保存密码；留空保留原密码。' : '加密保存，网页不会读回明文。';
  $('#sync-library').innerHTML = libraryOptions(); $('#sync-library').value = task?.library_id || catalogState.library || catalogState.libraries[0]?.id || '';
  syncFolders(); $('#sync-folder').value = task?.folder_id || '';
  const schedule = task?.schedule || {kind: 'daily', time: '02:00', timezone: 'Asia/Shanghai', interval_hours: 24, cron: '0 2 * * *', weekdays: []};
  for (const key of ['kind', 'time', 'timezone', 'cron']) $('#sync-' + key).value = schedule[key] || (key === 'cron' ? '0 2 * * *' : '');
  $('#sync-interval').value = schedule.interval_hours; $('#sync-enabled').checked = task?.enabled ?? true;
  $('#sync-weekdays').querySelectorAll('input').forEach(input => { input.checked = (schedule.weekdays || []).includes(Number(input.value)); });
  syncFields(); $('#sync-name').focus();
}
function syncSchedule() {
  return {kind: $('#sync-kind').value, time: $('#sync-time').value, timezone: $('#sync-timezone').value.trim(), interval_hours: Number($('#sync-interval').value), cron: $('#sync-cron').value.trim(), weekdays: [...$('#sync-weekdays').querySelectorAll('input:checked')].map(input => Number(input.value))};
}
function syncConnection() {
  if ($('#sync-source-kind').value === 'local') return {source_kind: 'local', source_id: $('#sync-source-id').value.trim()};
  return {server: $('#sync-server').value.trim(), share: $('#sync-share').value.trim(), subdir: $('#sync-subdir').value.trim().replaceAll('\\', '/').replace(/^\/+|\/+$/g, ''), username: $('#sync-username').value, password: $('#sync-password').value, port: Number($('#sync-port').value)};
}
async function syncHistory(task) {
  syncState.history = task.id; $('#sync-history').hidden = false; $('#sync-history-title').textContent = `${task.name} · 执行记录`;
  const runs = await api(`/api/sync/tasks/${task.id}/runs`);
  $('#sync-runs').innerHTML = runs.length ? runs.map(run => `<article class="sync-card"><div><h3>${escapeHtml({running: '同步中', success: '同步成功', failed: '同步失败'}[run.state] || run.state)}</h3><p>${syncDate(run.started_at)}${run.finished_at ? ' → ' + syncDate(run.finished_at) : ''}</p><p>已处理 ${run.completed || 0}/${run.total || 0} · 新增 ${run.added || 0} · 更新 ${run.updated || 0} · 跳过 ${run.skipped || 0} · 删除 ${run.deleted || 0} · 失败 ${run.failed || 0}</p>${(run.errors || []).map(message => `<p class="file-error">${escapeHtml(message)}</p>`).join('')}</div></article>`).join('') : '<p>暂无执行记录；保存后可点击立即同步。</p>';
}
$('#new-sync').addEventListener('click', () => syncEdit(null).catch(error => toast(error.message, true)));
$('#sync-kind').addEventListener('change', syncFields); $('#sync-library').addEventListener('change', syncFolders);
$('#sync-source-kind').addEventListener('change', syncSourceFields);
$('#sync-cancel').addEventListener('click', () => { $('#sync-form').hidden = true; $('#sync-password').value = ''; });
$('#sync-address').addEventListener('change', () => {
  const value = $('#sync-address').value.trim().replaceAll('\\', '/');
  const parts = value.replace(/^\/+/, '').split('/');
  if (parts.length < 2) return;
  $('#sync-server').value = parts[0]; $('#sync-share').value = parts[1]; $('#sync-subdir').value = parts.slice(2).join('/');
});
$('#sync-test').addEventListener('click', async () => {
  const button = $('#sync-test'); button.disabled = true; syncFeedback('正在连接远端共享…');
  try { const result = await api('/api/sync/test', {method: 'POST', body: {...syncConnection(), task_id: syncState.editing}}); syncFeedback(`连接成功。子目录：${result.directories?.join('、') || '无子目录'}`); }
  catch (error) { syncFeedback(error.message, true); } finally { button.disabled = false; }
});
$('#sync-preview').addEventListener('click', async () => {
  try { const result = await api('/api/sync/schedule', {method: 'POST', body: syncSchedule()}); syncFeedback(`下次执行：${syncDate(result.next_run)}（按浏览器本地时区显示）`); }
  catch (error) { syncFeedback(error.message, true); }
});
$('#sync-form').addEventListener('submit', async event => {
  event.preventDefault(); const button = $('#sync-form button[type=submit]'); button.disabled = true;
  try {
    const body = {...syncConnection(), name: $('#sync-name').value.trim(), library_id: $('#sync-library').value, folder_id: $('#sync-folder').value || null, enabled: $('#sync-enabled').checked, schedule: syncSchedule()};
    await api(syncState.editing ? `/api/sync/tasks/${syncState.editing}` : '/api/sync/tasks', {method: syncState.editing ? 'PUT' : 'POST', body});
    $('#sync-password').value = ''; $('#sync-form').hidden = true; await syncLoad(); toast('同步任务已保存。');
  } catch (error) { syncFeedback(error.message, true); } finally { button.disabled = false; }
});
$('#sync-list').addEventListener('click', async event => {
  const button = event.target.closest('[data-sync-action]'); if (!button) return;
  const task = syncState.tasks.find(t => t.id === button.dataset.id); if (!task) return;
  button.disabled = true;
  try {
    if (button.dataset.syncAction === 'edit') await syncEdit(task);
    else if (button.dataset.syncAction === 'runs') await syncHistory(task);
    else if (button.dataset.syncAction === 'run') { await api(`/api/sync/tasks/${task.id}/run`, {method: 'POST'}); toast('同步已启动，可查看执行记录。'); await syncHistory(task); await syncLoad(); }
    else if (button.dataset.syncAction === 'toggle') { await api(`/api/sync/tasks/${task.id}`, {method: 'PUT', body: {...task, enabled: !task.enabled, password: ''}}); await syncLoad(); }
    else if (button.dataset.syncAction === 'delete') confirmManagement('删除同步任务？', `“${task.name}”将停止同步并移除执行记录；知识库文件保留。`, async () => { await api(`/api/sync/tasks/${task.id}`, {method: 'DELETE'}); $('#sync-history').hidden = true; syncState.history = null; await syncLoad(); });
  } catch (error) { toast(error.message, true); } finally { button.disabled = false; }
});
$('#sync-history-refresh').addEventListener('click', () => {
  const task = syncState.tasks.find(t => t.id === syncState.history); if (task) syncHistory(task).catch(error => toast(error.message, true));
});
document.querySelector('[data-page="sync"]').addEventListener('click', syncLoad);
$('#auth-form').addEventListener('submit', () => { if (state.page === 'sync') setTimeout(syncLoad, 100); });
syncState.timer = setInterval(async () => {
  if (state.page !== 'sync' || !state.token || document.hidden) return;
  await syncLoad(); const task = syncState.tasks.find(t => t.id === syncState.history);
  if (task) { try { await syncHistory(task); } catch {} }
}, 3000);
if (location.hash === '#sync') syncLoad();
