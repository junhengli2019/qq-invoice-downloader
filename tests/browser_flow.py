"""Run with the production server :8766 and the synthetic mailbox fixture :8767."""
import io
import json
import os
import zipfile
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

OUT = Path(os.environ.get('INVOICE_TEST_OUTPUT', 'work/browser'))
OUT.mkdir(parents=True, exist_ok=True)
BASE = os.environ.get('INVOICE_TEST_URL', 'http://127.0.0.1:8766')
FIXTURE = os.environ.get('INVOICE_FIXTURE_URL', 'http://127.0.0.1:8767')
checks = {}; errors=[]

with sync_playwright() as p:
    browser=p.chromium.launch(headless=True,channel='chrome')
    context=browser.new_context(viewport={'width':1440,'height':1100},reduced_motion='reduce')
    page=context.new_page();page.on('pageerror',lambda e:errors.append(str(e)))
    response=page.goto(BASE);page.wait_for_load_state('networkidle')
    assert response.status==200
    expect(page.get_by_role('button',name='下一步：选择日期',exact=False)).to_be_enabled()
    page.screenshot(path=str(OUT/'票夹-电脑首页.png'),full_page=True)
    page.get_by_role('button',name='授权码怎么找？',exact=False).click()
    expect(page.get_by_role('dialog')).to_be_visible()
    expect(page.get_by_role('dialog').get_by_text('这里填授权码，不是 QQ 登录密码。')).to_have_count(0)
    expect(page.get_by_role('link',name='查看 QQ 邮箱官方说明',exact=False)).to_be_visible()
    page.screenshot(path=str(OUT/'票夹-授权引导.png'),full_page=True)
    page.keyboard.press('Escape');expect(page.get_by_role('dialog')).to_have_count(0)
    checks['authorizationGuideAndKeyboard']=True
    page.get_by_role('button',name='先用示例体验',exact=False).click()
    expect(page.get_by_role('heading',name='发票已整理好',exact=True)).to_be_visible(timeout=15000)
    expect(page.get_by_text('示例体验 · 未连接邮箱',exact=True)).to_be_visible()
    with page.expect_download() as download_info:page.get_by_role('link',name='下载全部发票（ZIP）',exact=False).click()
    download=download_info.value
    with zipfile.ZipFile(download.path()) as archive:
        assert len(archive.namelist())==2
        assert all('不可报销' in name and archive.read(name).startswith(b'%PDF-') for name in archive.namelist())
    checks['publicDemoZipDownload']=True
    page.reload();page.wait_for_load_state('networkidle')
    expect(page.get_by_role('heading',name='发票已整理好',exact=True)).to_be_visible()
    checks['resultSurvivesReload']=True
    page.screenshot(path=str(OUT/'票夹-下载结果.png'),full_page=True)
    other_context=browser.new_context(viewport={'width':390,'height':844},is_mobile=True,reduced_motion='reduce')
    other=other_context.new_page();other.goto(BASE);other.wait_for_load_state('networkidle')
    expect(other.get_by_role('heading',name='准备好你的 QQ 邮箱',exact=True)).to_be_visible()
    assert other.request.get(BASE+'/api/download').status==404
    checks['independentVisitors']=True
    assert other.evaluate('document.documentElement.scrollWidth <= innerWidth')
    other.screenshot(path=str(OUT/'票夹-手机首页.png'),full_page=True)
    other.set_viewport_size({'width':320,'height':750})
    assert other.evaluate('document.documentElement.scrollWidth <= innerWidth')
    checks['mobile320And390NoOverflow']=True
    page.get_by_role('button',name='立即清除本次文件',exact=True).click()
    expect(page.get_by_text('本次任务和文件已清除，邮箱已断开。',exact=True)).to_be_visible()
    assert page.request.get(BASE+'/api/download').status==404
    checks['deleteRevokesDownload']=True
    # Complete form -> real download core with a synthetic IMAP mailbox.
    page.goto(FIXTURE);page.wait_for_load_state('networkidle')
    for email in ['fail@qq.com','sample@qq.com']:
        page.get_by_label('QQ 邮箱地址',exact=True).fill(email)
        page.get_by_label('邮箱授权码',exact=True).fill('abcdefghijklmnop')
        page.get_by_label('我同意网站在本次整理中',exact=False).check()
        page.get_by_role('button',name='下一步：选择日期',exact=False).click()
        expect(page.get_by_role('heading',name='想整理哪段时间的发票？',exact=True)).to_be_visible()
        page.get_by_role('button',name='开始整理发票',exact=False).click()
        expect(page.get_by_label('邮箱授权码',exact=True)).to_have_value('')
        if email.startswith('fail'):
            expect(page.get_by_role('heading',name='这次还没有整理成功',exact=True)).to_be_visible(timeout=15000)
            expect(page.locator('#failure-message')).to_contain_text('登录失败')
            page.get_by_role('button',name='重新填写邮箱',exact=True).click()
            checks['authFailureRecovery']=True
        else:
            expect(page.get_by_role('heading',name='发票已整理好',exact=True)).to_be_visible(timeout=15000)
            expect(page.locator('#pdf-count')).to_have_text('1')
            with page.expect_download() as info:page.get_by_role('link',name='下载全部发票（ZIP）',exact=False).click()
            with zipfile.ZipFile(info.value.path()) as archive:assert len(archive.namelist())==1
            checks['formToMailboxCoreToZip']=True
    assert not errors,errors
    checks['noBrowserErrors']=True
    browser.close()
(OUT/'browser-checks.json').write_text(json.dumps(checks,ensure_ascii=False,indent=2)+'\n')
print(json.dumps(checks,ensure_ascii=False))
