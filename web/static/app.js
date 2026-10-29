'use strict';
(() => {
  const $ = (id) => document.getElementById(id);
  const state = {csrf: '', job: null, items: [], selected: new Set(), busy: false, expires: 0, timer: null};
  const form = $('search-form');
  const provider = () => form.querySelector('input[name="provider"]:checked').value;
  const dateString = (d) => `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`;
  const today = new Date();
  $('end').value = dateString(today);
  $('start').value = dateString(new Date(today.getFullYear(), today.getMonth(), 1));
  $('start').max = $('end').max = dateString(today);
  const fmtBytes = (n) => n < 1024 ? `${n} B` : n < 1048576 ? `${(n/1024).toFixed(1)} KB` : `${(n/1048576).toFixed(1)} MB`;
  function notice(id, text, kind = '') {const el=$(id); el.textContent=text; el.className=`notice ${kind}`; el.hidden=!text;}
  function errorMessage(data) {return typeof data.error === 'string' ? data.error : (data.message || '请求未完成，请稍后重试。');}
  async function api(path, method='GET', body) {
    const response = await fetch(path, {method, credentials:'same-origin', cache:'no-store', headers:{'Content-Type':'application/json', ...(method !== 'GET' ? {'X-CSRF-Token':state.csrf} : {})}, ...(body !== undefined ? {body:JSON.stringify(body)} : {})});
    if (!response.ok) {let data={};try{data=await response.json();}catch{}const err=new Error(errorMessage(data));err.status=response.status;throw err;}
    return response;
  }
  function setBusy(busy) {
    state.busy=busy;
    form.querySelectorAll('input, button').forEach(el=>el.disabled=busy);
    $('search-label').textContent=busy?'检索中，请稍候…':'检索发票';
    $('loading-state').hidden=!busy;
    $('results').hidden=busy || !state.job;
    $('empty-state').hidden=busy || !!state.job;
    $('logout-button').disabled=busy;
  }
  function resetResults() {
    state.job=null;state.items=[];state.selected.clear();state.expires=0;
    clearInterval(state.timer);state.timer=null;
    $('file-list').replaceChildren();$('file-count').textContent='0 份';
    $('results').hidden=true;$('empty-state').hidden=false;$('loading-state').hidden=true;
    $('result-subtitle').textContent='检索完成后，在这里选择需要的发票';
    updateSelection();
  }
  async function loadSession() {
    const data=await (await api('/api/session')).json();state.csrf=data.csrf;
    const authed=!!data.authenticated;
    $('workspace').hidden=!authed;$('access-panel').hidden=authed;$('logout-button').hidden=!authed;
    if (!data.configured) {notice('access-status','网站尚未设置访问口令，请部署管理员完成安全配置后再使用。');$('login-button').disabled=true;$('access-code').disabled=true;}
    else {$('login-button').disabled=false;$('access-code').disabled=false;}
    return data;
  }
  $('access-form').addEventListener('submit',async e=>{e.preventDefault();if($('login-button').disabled)return;const code=$('access-code').value;notice('access-status','');$('login-button').disabled=true;try{await api('/api/login','POST',{access_code:code});$('access-code').value='';await loadSession();$('email').focus();}catch(err){notice('access-status',err.message);$('access-code').value='';$('login-button').disabled=false;}});
  $('logout-button').addEventListener('click', async()=>{if(state.busy)return;$('logout-button').disabled=true;try{await api('/api/logout','POST',{});resetResults();form.reset();$('auth-code').value='';$('end').value=dateString(new Date());$('start').value=dateString(new Date(new Date().getFullYear(),new Date().getMonth(),1));await loadSession();updateProvider();notice('access-status','已退出，当前会话的临时结果已清除。','success');}catch(err){notice('form-status',err.message);$('logout-button').disabled=false;}});
  function updateProvider() {
    const qq=provider()==='qq';
    document.querySelectorAll('.provider').forEach(el=>el.classList.toggle('selected',el.querySelector('input').checked));
    $('email').placeholder=qq?'你的邮箱@qq.com':'你的邮箱@163.com';
    $('help-title').textContent=qq?'QQ 邮箱授权码':'网易 163 授权密码';
    $('help-copy').textContent=qq?'在 QQ 邮箱网页版“设置 → 账号与安全 → 安全设置”中找到 POP3/IMAP 服务，开启服务并按官方提示生成授权码。不同版本的入口可能略有区别。不要填写 QQ 登录密码。':'在网易 163 邮箱网页版“设置 → POP3/SMTP/IMAP”中开启 IMAP/SMTP，按页面提示新增授权密码并完成验证。这里填写授权密码，不是邮箱登录密码。';
    $('help-link').href=qq?'https://help.mail.qq.com/detail/106/985':'https://mail.163.com/';
    $('help-link').textContent=qq?'查看 QQ 官方设置说明 ↗':'前往网易 163 邮箱 ↗';
  }
  document.querySelectorAll('[data-days]').forEach(button=>button.addEventListener('click',()=>{const end=new Date(),start=new Date(end);if(button.dataset.days==='month')start.setDate(1);else start.setDate(end.getDate()-Number(button.dataset.days)+1);$('start').value=dateString(start);$('end').value=dateString(end);}));
  document.querySelectorAll('input[name="provider"]').forEach(el=>el.addEventListener('change',updateProvider));
  $('help-toggle').addEventListener('click',()=>{const open=$('auth-help').hidden;$('auth-help').hidden=!open;$('help-toggle').setAttribute('aria-expanded',String(open));});
  $('show-code').addEventListener('click',()=>{const show=$('auth-code').type==='password';$('auth-code').type=show?'text':'password';$('show-code').textContent=show?'隐藏':'显示';$('show-code').setAttribute('aria-label',show?'隐藏授权码':'显示授权码');});
  function updateSelection() {
    const count=state.selected.size,total=state.items.length;
    $('selection-count').textContent=`已选 ${count} / ${total} 份`;
    $('select-all').checked=total>0 && count===total;$('select-all').indeterminate=count>0&&count<total;
    $('download-button').disabled=count===0 || (state.expires>0&&Date.now()>=state.expires);
    const bytes=state.items.filter(i=>state.selected.has(i.id)).reduce((sum,i)=>sum+Number(i.size||0),0);
    $('selected-size').textContent=fmtBytes(bytes);
  }
  function renderResults(data) {
    state.job=data.job_id;state.items=Array.isArray(data.items)?data.items:[];state.selected=new Set(state.items.map(i=>i.id));state.expires=Date.now()+(data.expires_in||900)*1000;
    $('file-count').textContent=`${state.items.length} 份`;
    $('result-subtitle').textContent=state.items.length?'勾选需要的 PDF，一次打包带走':'没有找到符合条件的 PDF 附件';
    $('results-summary').textContent=`已扫描 ${data.scanned||0} 封邮件 · 匹配 ${data.matched||0} 封 · ${state.items.length} 份 PDF${data.skipped?` · 跳过 ${data.skipped} 项`:''}`;
    const warning=[data.warning, data.link_only?`${data.link_only} 封匹配邮件仅含网页链接或没有可下载 PDF 附件，请回原邮箱查看。`:''].filter(Boolean).join(' ');
    notice('result-warning',warning,'warning');
    const fragment=document.createDocumentFragment();
    state.items.forEach((item,index)=>{
      const row=document.createElement('div');row.className='file-row';
      const check=document.createElement('input');check.type='checkbox';check.id=`file-${index}`;check.checked=true;check.setAttribute('aria-label',`选择 ${item.name}`);
      check.addEventListener('change',()=>{if(check.checked)state.selected.add(item.id);else state.selected.delete(item.id);updateSelection();});
      const icon=document.createElement('span');icon.className='file-pdf';icon.textContent='PDF';
      const detail=document.createElement('div');detail.className='file-detail';
      const name=document.createElement('label');name.className='file-name';name.htmlFor=check.id;name.textContent=item.name;
      const subject=document.createElement('p');subject.className='file-subject';subject.textContent=item.subject||'（无主题）';
      const meta=document.createElement('div');meta.className='file-meta';
      [item.date,fmtBytes(Number(item.size||0)),item.sender].filter(Boolean).forEach(value=>{const span=document.createElement('span');span.textContent=value;meta.append(span);});
      detail.append(name,subject,meta);row.append(check,icon,detail);fragment.append(row);
    });
    if(!state.items.length){const p=document.createElement('p');p.className='file-subject';p.textContent='可以尝试调整日期或关键词。此版本只读取收件箱中的 PDF 附件，不自动打开正文链接。';fragment.append(p);}
    $('file-list').replaceChildren(fragment);clearInterval(state.timer);
    function tick(){const seconds=Math.max(0,Math.ceil((state.expires-Date.now())/1000));$('expiry-text').textContent=seconds?`临时结果 ${Math.ceil(seconds/60)} 分钟后失效`:'临时结果已到期，请重新检索';if(!seconds){clearInterval(state.timer);state.selected.clear();updateSelection();$('file-list').replaceChildren();notice('result-warning','临时结果已过期。服务器中的文件已失效，请重新检索。','warning');}}
    tick();state.timer=setInterval(tick,1000);updateSelection();
  }
  $('select-all').addEventListener('change',()=>{state.selected=$('select-all').checked?new Set(state.items.map(i=>i.id)):new Set();$('file-list').querySelectorAll('input').forEach(el=>el.checked=$('select-all').checked);updateSelection();});
  form.addEventListener('submit', async e=>{
    e.preventDefault();if(state.busy)return;notice('form-status','');
    const start=$('start').value,end=$('end').value;
    if(start>end){notice('form-status','开始日期不能晚于结束日期。');return;}
    if((Date.parse(end)-Date.parse(start))/86400000>365){notice('form-status','一次检索的日期范围不能超过 366 天，请分段查询。');return;}
    const payload={provider:provider(),email:$('email').value.trim(),auth_code:$('auth-code').value,start,end,keyword:$('keyword').value.trim(),consent:$('consent').checked};
    setBusy(true);
    if(state.job){try{await api(`/api/results/${encodeURIComponent(state.job)}`,'DELETE');}catch(err){if(err.status!==404){notice('form-status','无法清除上一批临时结果，请刷新页面后再试。');payload.auth_code='';$('auth-code').value='';setBusy(false);return;}}}
    resetResults();setBusy(true);
    try{const response=await api('/api/search','POST',payload);$('auth-code').value='';payload.auth_code='';renderResults(await response.json());notice('form-status',state.items.length?`检索完成，找到 ${state.items.length} 份 PDF。`:'检索完成，没有找到可下载的 PDF 附件。','success');}
    catch(err){notice('form-status',err.message);if(err.status===401){await loadSession().catch(()=>{});}}
    finally{payload.auth_code='';$('auth-code').value='';$('auth-code').type='password';$('show-code').textContent='显示';setBusy(false);if(state.job&&window.matchMedia('(max-width:700px)').matches)$('result-title').scrollIntoView({behavior:window.matchMedia('(prefers-reduced-motion:reduce)').matches?'auto':'smooth',block:'start'});}
  });
  $('download-button').addEventListener('click', async()=>{
    if(!state.job||!state.selected.size||$('download-button').disabled)return;
    $('download-button').disabled=true;$('download-button').textContent='打包中…';notice('form-status','');
    try{const response=await api('/api/download','POST',{job_id:state.job,item_ids:[...state.selected]});const blob=await response.blob();const url=URL.createObjectURL(blob);const a=document.createElement('a');a.href=url;a.download=`发票-${$('start').value}-${$('end').value}.zip`;document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),60000);notice('form-status','已交给浏览器下载。请查看浏览器下载列表；手机可保存到“文件”。','success');}
    catch(err){notice('form-status',err.message);}
    finally{$('download-button').textContent='下载 ZIP ↓';updateSelection();}
  });
  $('clear-button').addEventListener('click',async()=>{if(!state.job)return;$('clear-button').disabled=true;try{await api(`/api/results/${encodeURIComponent(state.job)}`,'DELETE');resetResults();notice('form-status','本次临时文件已清除。','success');}catch(err){if(err.status===404){resetResults();notice('form-status','本次结果已失效。','success');}else notice('form-status',err.message);}finally{$('clear-button').disabled=false;}});
  updateProvider();loadSession().catch(()=>notice('access-status','暂时无法连接服务，请检查网络后刷新页面。'));
})();
