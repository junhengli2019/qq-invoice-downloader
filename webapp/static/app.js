(() => {
  'use strict';
  const $ = (id) => document.getElementById(id);
  const state = { csrf: null, sessionPromise: null, job: null, files: [], selected: new Set(), pollTimer: null, pollVersion: 0, pollingErrors: 0, busy: false, downloading: false, demo: false, expiresAt: null };
  let toastTimer;
  const formatBytes = (value) => value >= 1048576 ? `${(value / 1048576).toFixed(1)} MB` : `${Math.max(1, Math.round(value / 1024))} KB`;
  const localDate = (date) => `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`;
  function node(tag, className, text) {
    const el = document.createElement(tag);
    if (className) el.className = className;
    if (text !== undefined) el.textContent = String(text);
    return el;
  }
  function icon(name) {
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('class', 'icon'); svg.setAttribute('aria-hidden', 'true');
    const use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
    use.setAttribute('href', `#i-${name}`); svg.append(use); return svg;
  }
  function notify(message) {
    clearTimeout(toastTimer); $('toast').textContent = message; $('toast').hidden = false;
    toastTimer = setTimeout(() => { $('toast').hidden = true; }, 6500);
  }
  function notice(message, retry) {
    $('notice').replaceChildren(); $('notice').hidden = !message;
    if (!message) return;
    $('notice').append(node('span', '', message));
    if (retry) {
      const button = node('button', 'text-link notice-retry', '继续获取结果');
      button.type = 'button'; button.addEventListener('click', retry); $('notice').append(button);
    }
  }
  function panel(name) {
    ['connect', 'progress', 'results'].forEach((key) => { $(`${key}-panel`).hidden = key !== name; });
    const current = name === 'connect' ? 1 : name === 'progress' ? 2 : 3;
    for (let n = 1; n <= 3; n++) {
      const step = $(`step-${n}`);
      step.classList.toggle('current', n === current); step.classList.toggle('complete', n < current);
      if (n === current) step.setAttribute('aria-current', 'step'); else step.removeAttribute('aria-current');
      step.querySelector('.step-number').textContent = n < current ? '✓' : String(n);
    }
  }
  function setBusy(busy) {
    state.busy = busy; $('form-fields').disabled = busy; $('demo-button').disabled = busy;
  }
  async function request(path, options = {}) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), options.download ? 120000 : 45000);
    const method = options.method || 'GET';
    const headers = { Accept: options.download ? 'application/zip' : 'application/json' };
    if (options.body !== undefined) headers['Content-Type'] = 'application/json';
    if (method !== 'GET') headers['X-CSRF-Token'] = state.csrf || '';
    try {
      const response = await fetch(path, { method, headers, credentials: 'same-origin', cache: 'no-store', signal: controller.signal, ...(options.body !== undefined ? { body: JSON.stringify(options.body) } : {}) });
      if (!response.ok) {
        let details; try { details = await response.json(); } catch { /* Deployment proxy can return HTML. */ }
        const error = new Error(details?.error || '服务暂时没有响应，请稍后重试。免费服务器首次启动可能需要约一分钟。');
        error.status = response.status; throw error;
      }
      return options.download ? response.blob() : response.json();
    } catch (error) {
      if (error.name === 'AbortError') throw new Error('等待服务器响应超时，请检查网络后重试。免费服务器首次启动可能需要约一分钟。');
      if (error instanceof TypeError) throw new Error('暂时无法连接服务器，请检查网络连接后重试。');
      throw error;
    } finally { clearTimeout(timeout); }
  }
  async function ensureSession() {
    if (state.csrf) return;
    if (!state.sessionPromise) {
      state.sessionPromise = request('/api/session').then((data) => {
        if (!data.csrf_token) throw new Error('页面初始化失败，请刷新后重试。');
        state.csrf = data.csrf_token;
        if (data.active_job_id && !state.job) state.job = data.active_job_id;
      }).finally(() => { state.sessionPromise = null; });
    }
    await state.sessionPromise;
  }
  function stopPolling() { clearTimeout(state.pollTimer); state.pollVersion++; }
  async function deleteCurrent() {
    stopPolling();
    if (state.job) {
      try { await request(`/api/jobs/${encodeURIComponent(state.job)}`, { method: 'DELETE' }); }
      catch (error) { if (error.status !== 404) throw error; }
    }
    state.job = null; state.files = []; state.selected.clear(); state.expiresAt = null;
  }
  function setRange(range) {
    const end = new Date(); const start = new Date(end);
    if (range === 'month') start.setDate(1); else start.setDate(start.getDate() - Number(range) + 1);
    $('start-date').value = localDate(start); $('end-date').value = localDate(end);
    document.querySelectorAll('[data-range]').forEach((button) => {
      const active = button.dataset.range === range; button.classList.toggle('active', active); button.setAttribute('aria-pressed', String(active));
    });
  }
  function validateForm() {
    const email = $('email').value.trim().toLowerCase();
    const provider = document.querySelector('input[name="provider"]:checked').value;
    const domain = email.split('@').pop();
    if (!(provider === 'qq' ? ['qq.com', 'foxmail.com'] : ['163.com']).includes(domain)) {
      $('email').setCustomValidity(provider === 'qq' ? '请填写 @qq.com 或 @foxmail.com 邮箱；163 邮箱请选择网易 163。' : '请填写 @163.com 邮箱；QQ 邮箱请选择 QQ。');
      $('email').reportValidity(); return null;
    }
    if ($('start-date').value > $('end-date').value) {
      $('end-date').setCustomValidity('截止日期不能早于开始日期。'); $('end-date').reportValidity(); return null;
    }
    return { provider, email, auth_code: $('auth-code').value.trim(), start_date: $('start-date').value, end_date: $('end-date').value, consent: $('consent').checked };
  }
  async function begin(demo, values) {
    if (state.busy) return;
    setBusy(true); notice('');
    try {
      await ensureSession();
      await deleteCurrent();
      const data = await request(demo ? '/api/demo' : '/api/jobs', { method: 'POST', ...(demo ? {} : { body: values }) });
      if (!data.id) throw new Error('任务未能创建，请刷新页面后重试。');
      state.job = data.id; state.demo = demo; state.pollingErrors = 0;
      $('auth-code').value = ''; $('auth-code').type = 'password';
      $('toggle-secret').textContent = '显示'; $('toggle-secret').setAttribute('aria-pressed', 'false'); $('toggle-secret').setAttribute('aria-label', '显示授权码');
      $('progress-title').textContent = demo ? '正在整理示例发票' : '正在查找你的发票';
      $('progress-message').textContent = demo ? '准备一份可以下载的演示结果…' : '正在连接邮箱，请稍等…';
      $('progress-stats').textContent = '通常需要几秒到几分钟，请保持页面打开。';
      $('progress-demo-label').hidden = !demo; $('cancel-button').disabled = false;
      panel('progress');
      await poll(state.pollVersion);
    } catch (error) {
      setBusy(false); panel('connect'); notice(error.message);
    } finally {
      if (values) { values.auth_code = ''; $('auth-code').value = ''; }
    }
  }
  async function poll(version) {
    if (version !== state.pollVersion || !state.job) return;
    try {
      const data = await request(`/api/jobs/${encodeURIComponent(state.job)}`);
      if (version !== state.pollVersion) return;
      state.pollingErrors = 0; notice('');
      if (data.status === 'done') { setBusy(false); renderResults(data); return; }
      if (data.status === 'error' || data.status === 'cancelled') {
        setBusy(false); panel('connect'); notice(data.error || '查找已取消。调整设置后可以重新开始。'); return;
      }
      $('progress-message').textContent = data.progress?.message || '正在读取邮件，请稍等…';
      const scanned = Number(data.progress?.scanned) || 0;
      const matched = Number(data.progress?.matched) || 0;
      $('progress-stats').textContent = scanned ? `已检查 ${scanned} 封邮件 · 找到 ${matched} 封相关邮件` : '正在准备读取收件箱，不会改变邮件的已读状态。';
      state.pollTimer = setTimeout(() => poll(version), 1300);
    } catch (error) {
      if (version !== state.pollVersion) return;
      if (error.status === 404 || error.status === 403) {
        state.job = null; setBusy(false); panel('connect'); state.csrf = null;
        notice('任务已过期或服务器已重启。请重新连接邮箱查找发票。'); return;
      }
      state.pollingErrors++;
      if (state.pollingErrors < 3) {
        $('progress-message').textContent = '网络暂时中断，正在重新获取进度…';
        state.pollTimer = setTimeout(() => poll(version), 2500);
      } else {
        notice(`${error.message} 查找任务可能仍在进行。`, () => {
          notice(''); state.pollingErrors = 0; poll(state.pollVersion);
        });
      }
    }
  }
  function renderResults(data) {
    state.files = Array.isArray(data.files) ? data.files : []; state.demo = !!data.demo;
    state.expiresAt = data.expires_at; state.selected = new Set(state.files.map((file) => file.id));
    const count = state.files.length;
    $('results-title').textContent = count ? '发票已经收好了' : '查找完成';
    $('results-summary').textContent = `已检查 ${Number(data.progress?.scanned) || 0} 封邮件，找到 ${count} 份可下载的 PDF。`;
    $('result-eyebrow-text').textContent = data.demo ? '示例体验' : '整理完成';
    $('demo-badge').hidden = !data.demo; $('demo-explainer').hidden = !data.demo;
    $('empty-state').hidden = count > 0; $('file-area').hidden = count === 0;
    $('file-count').textContent = `${count} 份 PDF`;
    $('file-list').replaceChildren();
    state.files.forEach((file, index) => {
      const row = node('div', 'file-row');
      const checkbox = node('input', 'file-checkbox'); checkbox.type = 'checkbox'; checkbox.id = `file-select-${index}`; checkbox.checked = true;
      checkbox.setAttribute('aria-label', `选择 ${file.name || '发票 PDF'}`); checkbox.dataset.fileId = file.id;
      checkbox.addEventListener('change', () => { if (checkbox.checked) state.selected.add(file.id); else state.selected.delete(file.id); updateSelection(); });
      const pdf = node('span', 'pdf-icon', 'PDF'); pdf.setAttribute('aria-hidden', 'true');
      const info = node('div', 'file-info'); const name = node('label', 'file-name', file.name || '发票.pdf'); name.htmlFor = checkbox.id;
      const meta = node('div', 'file-meta');
      if (file.date) meta.append(node('span', '', file.date));
      if (file.sender) meta.append(node('span', '', file.sender));
      if (file.source) meta.append(node('span', 'source-tag', file.source));
      if (file.subject) info.title = file.subject;
      info.append(name, meta); row.append(checkbox, pdf, info, node('span', 'file-size', formatBytes(Number(file.size) || 0)));
      $('file-list').append(row);
    });
    $('warnings').replaceChildren();
    const warnings = Array.isArray(data.warnings) ? data.warnings : [];
    $('warnings').hidden = warnings.length === 0;
    warnings.forEach((warning) => $('warnings').append(node('p', '', warning)));
    $('manual-list').replaceChildren();
    const links = Array.isArray(data.manual_links) ? data.manual_links : [];
    $('manual-area').hidden = links.length === 0;
    links.forEach((link) => {
      const item = node('div', 'manual-item'); const detail = node('div');
      detail.append(node('div', 'manual-subject', link.subject || '发票下载链接'), node('p', 'manual-reason', [link.date, link.reason].filter(Boolean).join(' · ')));
      item.append(detail);
      try {
        const url = new URL(link.url);
        if (!['https:', 'http:'].includes(url.protocol) || url.username || url.password) throw new Error('Unsafe link');
        const open = node('a', '', '打开网页'); open.href = url.href; open.target = '_blank'; open.rel = 'noopener noreferrer'; open.append(icon('external')); item.append(open);
      } catch { detail.append(node('p', 'manual-reason', '链接无法安全打开，请在原邮件中查看。')); }
      $('manual-list').append(item);
    });
    updateSelection(); updateExpiry(); panel('results'); $('results-title').focus({ preventScroll: true });
  }
  function updateSelection() {
    const selectedFiles = state.files.filter((file) => state.selected.has(file.id));
    const count = selectedFiles.length;
    $('selection-summary').textContent = `已选择 ${count} 份`;
    $('selection-size').textContent = count ? `合计 ${formatBytes(selectedFiles.reduce((sum, file) => sum + (Number(file.size) || 0), 0))}` : '勾选需要的发票后即可下载';
    $('select-all').checked = state.files.length > 0 && count === state.files.length;
    $('select-all').indeterminate = count > 0 && count < state.files.length;
    $('download-button').disabled = count === 0 || state.downloading || (state.expiresAt && state.expiresAt * 1000 <= Date.now());
  }
  function updateExpiry() {
    const target = $('expiry-note').querySelector('span');
    if (!state.expiresAt) { target.textContent = '结果将在 30 分钟后清除'; return; }
    const minutes = Math.ceil((state.expiresAt * 1000 - Date.now()) / 60000);
    target.textContent = minutes > 0 ? `临时结果将在约 ${minutes} 分钟后清除` : '结果已过期，请重新查找';
    if (minutes <= 0) { $('download-button').disabled = true; }
  }
  async function reset(message) {
    if (state.downloading) return;
    setBusy(true); $('cancel-button').disabled = true; $('clear-button').disabled = true; $('new-search-button').disabled = true;
    try {
      await ensureSession(); await deleteCurrent();
      $('auth-code').value = ''; $('consent').checked = false; $('file-list').replaceChildren(); $('manual-list').replaceChildren();
      notice(''); panel('connect'); if (message) notify(message); $('email').focus({ preventScroll: true });
    } catch (error) { notice(error.message); }
    finally { setBusy(false); $('cancel-button').disabled = false; $('clear-button').disabled = false; $('new-search-button').disabled = false; }
  }
  $('connect-form').addEventListener('submit', (event) => { event.preventDefault(); const values = validateForm(); if (values) begin(false, values); });
  $('demo-button').addEventListener('click', () => begin(true));
  $('email').addEventListener('input', () => $('email').setCustomValidity(''));
  document.querySelectorAll('input[name="provider"]').forEach((radio) => radio.addEventListener('change', () => {
    $('email').setCustomValidity(''); $('email').placeholder = radio.value === '163' ? '例如 yourname@163.com' : '例如 123456789@qq.com';
  }));
  document.querySelectorAll('[data-range]').forEach((button) => button.addEventListener('click', () => { setRange(button.dataset.range); $('end-date').setCustomValidity(''); }));
  ['start-date', 'end-date'].forEach((id) => $(id).addEventListener('input', () => {
    $('end-date').setCustomValidity(''); document.querySelectorAll('[data-range]').forEach((button) => { button.classList.remove('active'); button.setAttribute('aria-pressed', 'false'); });
  }));
  $('toggle-secret').addEventListener('click', () => {
    const show = $('auth-code').type === 'password'; $('auth-code').type = show ? 'text' : 'password';
    $('toggle-secret').textContent = show ? '隐藏' : '显示'; $('toggle-secret').setAttribute('aria-pressed', String(show)); $('toggle-secret').setAttribute('aria-label', show ? '隐藏授权码' : '显示授权码');
  });
  $('select-all').addEventListener('change', () => {
    state.selected = new Set($('select-all').checked ? state.files.map((file) => file.id) : []);
    document.querySelectorAll('.file-checkbox').forEach((checkbox) => { checkbox.checked = state.selected.has(checkbox.dataset.fileId); }); updateSelection();
  });
  $('download-button').addEventListener('click', async () => {
    if (!state.job || !state.selected.size || state.downloading) return;
    state.downloading = true; updateSelection(); notice(''); $('new-search-button').disabled = true; $('clear-button').disabled = true; $('demo-button').disabled = true;
    const button = $('download-button'); button.replaceChildren(icon('download'), document.createTextNode('正在打包…'));
    try {
      const blob = await request(`/api/jobs/${encodeURIComponent(state.job)}/download`, { method: 'POST', body: { ids: [...state.selected] }, download: true });
      const url = URL.createObjectURL(blob); const anchor = node('a'); anchor.href = url;
      anchor.download = `${state.demo ? '示例_' : ''}收票夹_${localDate(new Date())}.zip`;
      document.body.append(anchor); anchor.click(); anchor.remove(); setTimeout(() => URL.revokeObjectURL(url), 60000);
      notify('已开始下载 ZIP，请在浏览器下载记录或设备的「下载」文件夹查看。');
    } catch (error) { notice(error.message); }
    finally {
      state.downloading = false; button.replaceChildren(icon('download'), document.createTextNode('打包下载 ZIP')); updateSelection(); updateExpiry();
      $('new-search-button').disabled = false; $('clear-button').disabled = false; $('demo-button').disabled = false;
    }
  });
  $('new-search-button').addEventListener('click', () => reset('上次临时结果已清除，可以开始新的查找。'));
  $('clear-button').addEventListener('click', () => reset('本次临时数据已清除。'));
  $('cancel-button').addEventListener('click', () => reset('已取消查找，并清除本次临时数据。'));
  function helpTab(provider) {
    ['qq', '163'].forEach((key) => { const selected = provider === key; $(`help-${key}`).hidden = !selected; $(`help-tab-${key}`).setAttribute('aria-selected', String(selected)); $(`help-tab-${key}`).tabIndex = selected ? 0 : -1; });
  }
  document.querySelectorAll('[data-dialog]').forEach((button) => button.addEventListener('click', (event) => {
    event.preventDefault();
    const type = button.dataset.dialog;
    if (type === 'help') helpTab(document.querySelector('input[name="provider"]:checked').value);
    $(`${type}-dialog`).showModal();
  }));
  document.querySelectorAll('[data-close]').forEach((button) => button.addEventListener('click', () => button.closest('dialog').close()));
  document.querySelectorAll('dialog').forEach((dialog) => dialog.addEventListener('click', (event) => {
    if (event.target !== dialog) return;
    const rect = dialog.getBoundingClientRect(); if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) dialog.close();
  }));
  document.querySelectorAll('[data-help-tab]').forEach((button) => {
    button.addEventListener('click', () => helpTab(button.dataset.helpTab));
    button.addEventListener('keydown', (event) => { if (['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) { event.preventDefault(); const next = event.key === 'Home' ? 'qq' : event.key === 'End' ? '163' : button.dataset.helpTab === 'qq' ? '163' : 'qq'; helpTab(next); $(`help-tab-${next}`).focus(); } });
  });
  setRange('30');
  setInterval(() => { if (!$('results-panel').hidden) updateExpiry(); }, 30000);
  // Start early, but defer any initialization error to a deliberate user action.
  ensureSession().then(() => {
    if (state.job && !state.busy) {
      setBusy(true); panel('progress');
      $('progress-message').textContent = '正在恢复上次查找的结果…';
      poll(state.pollVersion);
    }
  }).catch(() => {});
})();
