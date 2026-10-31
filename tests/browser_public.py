import json,zipfile,time,os
from pathlib import Path
from playwright.sync_api import sync_playwright,expect
URL=os.environ.get('INVOICE_TEST_URL','https://piao-jia-jh.onrender.com')
out=Path(os.environ.get('INVOICE_TEST_OUTPUT','work/public-browser'));out.mkdir(parents=True,exist_ok=True)
record={'url':URL,'anonymous':True,'checks':{},'real_account_login_tested':False}
with sync_playwright() as p:
 b=p.chromium.launch(headless=True,channel='chrome');c=b.new_context(viewport={'width':1440,'height':1100},reduced_motion='reduce');page=c.new_page();errors=[]
 page.on('pageerror',lambda e:errors.append(str(e)))
 try:
  r=page.goto(URL,timeout=90000);page.wait_for_load_state('networkidle');assert r.status==200
  expect(page.get_by_role('button',name='先用示例体验',exact=False)).to_be_enabled()
  health=page.evaluate("async()=> (await fetch('/healthz')).json()")
  record['health']=health;assert health['status']=='ok';assert health['qq_connection']=='reachable',health
  record['checks']['publicWithoutLogin']=True
  page.screenshot(path=str(out/'票夹-公开网站.png'),full_page=True)
  page.get_by_role('button',name='不会获取？打开三步说明',exact=True).click()
  expect(page.get_by_role('dialog')).to_be_visible()
  expect(page.get_by_role('link',name='查看 QQ 邮箱官方说明',exact=False)).to_be_visible()
  page.keyboard.press('Escape');record['checks']['authorizationGuide']=True
  page.get_by_role('button',name='先用示例体验',exact=False).click()
  expect(page.get_by_role('heading',name='发票已整理好',exact=True)).to_be_visible(timeout=30000)
  expect(page.get_by_text('示例体验 · 未连接邮箱',exact=True)).to_be_visible()
  with page.expect_download() as info:page.get_by_role('link',name='下载全部发票（ZIP）',exact=False).click()
  with zipfile.ZipFile(info.value.path()) as archive:
   assert len(archive.namelist())==2
   assert all('不可报销' in n and archive.read(n).startswith(b'%PDF-') for n in archive.namelist())
  record['checks']['cloudProcessAndZipDownload']=True
  page.reload();page.wait_for_load_state('networkidle');expect(page.get_by_role('heading',name='发票已整理好',exact=True)).to_be_visible()
  record['checks']['refreshRetainsResult']=True
  page.screenshot(path=str(out/'票夹-公网下载结果.png'),full_page=True)
  c2=b.new_context(viewport={'width':390,'height':844},is_mobile=True,reduced_motion='reduce');other=c2.new_page();other.goto(URL,timeout=90000);other.wait_for_load_state('networkidle')
  expect(other.get_by_role('heading',name='准备好你的 QQ 邮箱',exact=True)).to_be_visible()
  assert other.evaluate("async()=> (await fetch('/api/download')).status")==404
  assert other.evaluate('document.documentElement.scrollWidth <= innerWidth')
  record['checks']['visitorIsolation']=True;record['checks']['mobileNoOverflow']=True
  other.screenshot(path=str(out/'票夹-公网手机.png'),full_page=True)
  cookies=c.cookies();cookie=next(x for x in cookies if x['name']=='invoice_visitor')
  assert cookie['httpOnly'] and cookie['secure'] and cookie['sameSite']=='Lax';record['checks']['secureCookie']=True
  page.get_by_role('button',name='立即清除本次文件',exact=True).click();expect(page.get_by_text('本次任务和文件已清除，邮箱已断开。',exact=True)).to_be_visible()
  assert page.evaluate("async()=> (await fetch('/api/download')).status")==404
  record['checks']['deleteRevokesDownload']=True
  denied=page.evaluate("async()=> {const r=await fetch('/api/demo',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});return r.status}")
  assert denied==403;record['checks']['csrfRequired']=True
  assert not errors,errors;record['checks']['noBrowserErrors']=True;record['success']=True
 finally:
  record['browserErrors']=errors
  (out/'票夹-公网检查记录.json').write_text(json.dumps(record,ensure_ascii=False,indent=2)+'\n')
  print(json.dumps(record,ensure_ascii=False),flush=True);b.close()
