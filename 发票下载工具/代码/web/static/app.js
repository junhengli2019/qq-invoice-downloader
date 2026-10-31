'use strict';
const $ = id => document.getElementById(id);
let csrf = '', step = 1, timer = null, busy = false, generation = 0;
function dateString(d) { return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`; }
function range(which) {
  const now = new Date();
  if (which === 'this') { $('start-date').value = dateString(new Date(now.getFullYear(), now.getMonth(), 1)); $('end-date').value = dateString(now); }
  if (which === 'last') { $('start-date').value = dateString(new Date(now.getFullYear(), now.getMonth()-1, 1)); $('end-date').value = dateString(new Date(now.getFullYear(), now.getMonth(), 0)); }
  document.querySelectorAll('[data-range]').forEach(b => { const active=b.dataset.range === which; b.classList.toggle('selected',active); b.setAttribute('aria-pressed',String(active)); });
  if (which === 'custom') $('start-date').focus();
}
range('this'); $('end-date').max = dateString(new Date()); $('start-date').max = dateString(new Date());
document.querySelectorAll('[data-range]').forEach(b => b.addEventListener('click',()=>range(b.dataset.range)));
['start-date','end-date'].forEach(id=>$(id).addEventListener('change',()=>document.querySelectorAll('[data-range]').forEach(b=>{ const custom=b.dataset.range==='custom';b.classList.toggle('selected',custom);b.setAttribute('aria-pressed',String(custom)); })));
function showStep(n, focus=true) {
  step=n;
  for(let i=1;i<=3;i++) $(`step-${i}`).hidden=i!==n;
  document.querySelectorAll('[data-step]').forEach(el=>{const i=Number(el.dataset.step);el.classList.toggle('active',i===n);el.classList.toggle('complete',i<n);if(i===n)el.setAttribute('aria-current','step');else el.removeAttribute('aria-current');});
  if(focus) $(`step-${n}`).querySelector('h2').focus({preventScroll:true});
}
function error(message) { $('error').textContent=message; $('error').hidden=false; $('error').scrollIntoView({block:'nearest'}); }
function clearMessages() { $('error').hidden=true; $('notice').hidden=true; }
function notice(message) { $('notice').textContent=message; $('notice').hidden=false; }
async function api(path, body) {
  const options = body === undefined ? {} : {method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:JSON.stringify(body)};
  const controller=new AbortController();const timeout=setTimeout(()=>controller.abort(),20000);
  try {
    const response=await fetch(path,{...options,signal:controller.signal,cache:'no-store'});
    let data;
    try {data=await response.json();} catch {throw new Error('网站暂时没有响应，请稍后刷新重试。');}
    if(!response.ok) throw new Error(data.error||'操作没有完成，请稍后重试。');
    return data;
  } catch(e) { if(e.name==='AbortError')throw new Error('连接用时较长，请稍等后刷新页面查看任务状态。');throw e; }
  finally {clearTimeout(timeout);}
}
function lock(value) {busy=value;['next-button','demo','start-button','back'].forEach(id=>$(id).disabled=value||!csrf);}
$('show-code').addEventListener('click',()=>{const showing=$('code').type==='password';$('code').type=showing?'text':'password';$('show-code').textContent=showing?'隐藏':'显示';$('show-code').setAttribute('aria-pressed',String(showing));});
$('mail-form').addEventListener('submit',event=>{
  event.preventDefault();clearMessages();if(busy)return;
  const email=$('email').value.trim();const code=$('code').value.replace(/\s/g,'');
  if(!/^[^@\s]{1,64}@(qq\.com|vip\.qq\.com|foxmail\.com)$/i.test(email)){error('请填写完整的 QQ 邮箱地址。');$('email').focus();return;}
  if(!/^[A-Za-z]{16}$/.test(code)){error('授权码应是 QQ 邮箱生成的 16 位字母，不是 QQ 密码。');$('code').focus();return;}
  $('mail-summary').textContent=email;$('code').value=code;showStep(2);
});
$('back').addEventListener('click',()=>{clearMessages();showStep(1);});
function schedulePoll(delay=1800) {clearTimeout(timer);const current=generation;timer=setTimeout(async()=>{try{const job=await api('/api/status');if(current===generation)render(job);}catch(e){if(current===generation){error(e.message);schedulePoll(delay);}}},delay);}
function render(job, restore=false) {
  clearTimeout(timer);
  if(job.state==='idle') {
    if(restore||step===3) {showStep(1,!restore);if(!restore)notice('本次任务已清除或到期。可以重新填写邮箱开始整理。');}
    lock(false);return;
  }
  showStep(3,!restore&&step!==3);
  $('demo-label').hidden=!job.demo;
  $('running').hidden=job.state!=='running';$('result').hidden=job.state!=='success';$('failed').hidden=job.state!=='error';
  lock(job.state==='running');
  if(job.state==='running') {
    $('result-heading').textContent=job.demo?'正在准备示例':'正在整理发票';
    const messages={connect:'正在连接 QQ 邮箱…',scan:'正在查找发票邮件…',pack:'正在打包下载文件…'};
    $('progress-message').textContent=job.total?`正在检查第 ${job.checked} / ${job.total} 封邮件`:messages[job.stage]||messages.connect;
    if(job.total){$('progress').max=job.total;$('progress').value=job.checked;}else $('progress').removeAttribute('value');
    schedulePoll();
  } else if(job.state==='success') {
    const summary=job.summary||{};const count=summary.saved_pdfs||0;
    $('result-heading').textContent=count?'发票已整理好':job.has_download?'有些链接需要你打开':'这段时间没有找到发票';
    $('result-message').textContent=job.demo?'这是示例结果，没有连接你的邮箱。下载的示例 PDF 不可用于报销。':count?'下载到手机或电脑，方便接下来报销。':'可以换一个日期范围，或确认发票是否在收件箱中。';
    $('pdf-count').textContent=count;$('mail-count').textContent=summary.scanned_messages||0;
    const warnings=[];if(summary.manual_links)warnings.push(`${summary.manual_links} 个链接需在原邮件中完成登录或验证，详细清单已放入下载包。`);if(summary.failed_items)warnings.push(`${summary.failed_items} 项未能处理，结果可能不完整，请回到原邮箱核对。`);
    $('result-warning').hidden=!warnings.length;$('result-warning').textContent=warnings.join(' ');
    $('file-list').replaceChildren(...(job.files||[]).map(name=>{const li=document.createElement('li');li.textContent=name;return li;}));
    $('file-details').hidden=!(job.files||[]).length;$('download').hidden=!job.has_download;
    $('retention').textContent=job.has_download?`文件将在约 ${Math.max(1,Math.ceil(job.expires_in/60))} 分钟后清除，请及时下载。`:'本次已检查完毕，没有生成下载文件。';
    schedulePoll(30000);
  } else {
    $('result-heading').textContent='这次还没有整理成功';$('failure-message').textContent=job.message||'请稍后重新开始。';
  }
}
async function start(path, body) {
  if(busy)return;clearMessages();generation++;lock(true);
  try {const job=await api(path,body);$('code').value='';render(job);}
  catch(e){$('code').value='';lock(false);showStep(1);error(e.message+' 授权码已从输入框清除。');}
}
$('date-form').addEventListener('submit',event=>{
  event.preventDefault();const from=$('start-date').value,to=$('end-date').value;
  const days=(Date.parse(to+'T00:00:00Z')-Date.parse(from+'T00:00:00Z'))/86400000;
  if(!Number.isFinite(days)||days<0||days>30){error('请选择最多 31 天的日期范围，开始日期不能晚于结束日期。');return;}
  start('/api/start',{email:$('email').value.trim(),authorization_code:$('code').value,consent:$('consent').checked,start_date:from,end_date:to});
});
$('demo').addEventListener('click',()=>start('/api/demo',{}));
async function clearJob(message) {
  generation++;clearTimeout(timer);clearMessages();
  try{await api('/api/clear',{});$('code').value='';$('consent').checked=false;lock(false);showStep(1);notice(message);}
  catch(e){error(e.message);}
}
document.querySelectorAll('.clear-trigger').forEach(b=>b.addEventListener('click',()=>clearJob('本次任务和文件已清除，邮箱已断开。')));
$('again').addEventListener('click',()=>clearJob('上次的临时文件已清除。请重新填写授权码，开始下一次整理。'));
$('retry').addEventListener('click',()=>clearJob('请核对邮箱和授权码后重试。'));
document.querySelectorAll('.help-trigger').forEach(b=>b.addEventListener('click',()=>$('help-dialog').showModal()));
$('privacy-open').addEventListener('click',()=>$('privacy-dialog').showModal());
document.querySelectorAll('.close-dialog').forEach(b=>b.addEventListener('click',()=>b.closest('dialog').close()));
document.querySelectorAll('dialog').forEach(dialog=>dialog.addEventListener('click',e=>{if(e.target===dialog){const rect=dialog.getBoundingClientRect();if(e.clientX<rect.left||e.clientX>rect.right||e.clientY<rect.top||e.clientY>rect.bottom)dialog.close();}}));
async function init(){try{const data=await api('/api/session');csrf=data.csrf;lock(false);render(data.job,true);}catch(e){error(e.message);}}
init();
