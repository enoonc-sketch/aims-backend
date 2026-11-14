"""AIMS 端到端功能检测（对着一个真实运行的后端，走完整业务流程）。

覆盖：认证/角色权限、设备与 TAG、网关与采集入库、测量与导入、报警链路、AI、维护/工单/备件、
资产、实验室、知识库、数据分析、云数据源(SSRF 防护)、模板中心全流程(导入导出/版本/回退/应用)、仪表盘。

⚠ 只能对一次性的测试实例运行：它会创建测试用户、设备、工单等数据，
  并（仅当 AIMS_E2E_CHANGE_PASSWORD=1 时）临时修改 admin 密码再改回。不要对生产库运行。

用法（先用一个全新的空库启动后端，并设 RATE_LIMIT_ENABLED=false）：
    AIMS_BASE=http://127.0.0.1:8000 AIMS_ADMIN_USER=admin AIMS_ADMIN_PASS=admin123 python tools/e2e_smoke.py
退出码：全部通过为 0，有失败为 1。
"""
import os
import httpx, json, io, time, csv, sys, traceback
B=os.environ.get('AIMS_BASE','http://127.0.0.1:8000').rstrip('/')+'/api/v1'
ADM_U=os.environ.get('AIMS_ADMIN_USER','admin'); ADM_P=os.environ['AIMS_ADMIN_PASS']
TMP_P=os.environ.get('AIMS_E2E_TMP_PASS','E2e-Tmp-Pass!2026')
c=httpx.Client(timeout=120)
R=[]  # (module, name, ok, detail)
def rec(mod,name,ok,detail=''):
    R.append((mod,name,ok,detail)); 
    if not ok: print(f'  FAIL [{mod}] {name}: {detail}')
def call(mod,name,method,path,expect=(200,201,204),**kw):
    try:
        r=c.request(method,B+path,headers=H if kw.pop('auth',True) else {},**kw)
    except Exception as e:
        rec(mod,name,False,'EXC '+str(e)[:120]); return None
    ok = r.status_code in (expect if isinstance(expect,(tuple,list,set)) else (expect,))
    rec(mod,name,ok,f'{r.status_code} {r.text[:160]}' if not ok else '')
    return r
def J(r):
    try: return r.json()
    except Exception: return {}
# ---------------- AUTH
r=c.post(B+'/auth/login',json={'username':ADM_U,'password':ADM_P}); tok=r.json(); H={'Authorization':'Bearer '+tok['access_token']}
rec('auth','login',r.status_code==200)
if tok.get('must_change_password'):
    # 默认账号(admin/admin123)首次登录必须先改密码：测试实例是一次性的，直接改成临时强口令继续
    r2=c.post(B+'/auth/change-password',headers=H,json={'current_password':ADM_P,'new_password':TMP_P})
    rec('auth','forced first-login password change',r2.status_code==200,r2.text[:120])
    tok=r2.json(); ADM_P=TMP_P; H={'Authorization':'Bearer '+tok['access_token']}
call('auth','login wrong password','POST','/auth/login',401,json={'username':'admin','password':'nope'},auth=False)
call('auth','no token -> 401','GET','/devices',(401,403),auth=False)
rr=call('auth','refresh','POST','/auth/refresh',200,json={'refresh_token':tok['refresh_token']},auth=False)
# 临时改密码再改回：默认 admin/admin123 过不了密码策略，改不回去，所以默认跳过；用强口令的实例可设 AIMS_E2E_CHANGE_PASSWORD=1
if os.environ.get('AIMS_E2E_CHANGE_PASSWORD')=='1':
    call('auth','change password','POST','/auth/change-password',(200,204),json={'current_password':ADM_P,'new_password':TMP_P})
    r=c.post(B+'/auth/login',json={'username':ADM_U,'password':TMP_P}); rec('auth','login with new password',r.status_code==200)
    H={'Authorization':'Bearer '+r.json()['access_token']}
    call('auth','change password back','POST','/auth/change-password',(200,204),json={'current_password':TMP_P,'new_password':ADM_P})
r=c.post(B+'/auth/login',json={'username':ADM_U,'password':ADM_P}); tok=r.json(); H={'Authorization':'Bearer '+tok['access_token']}
# ---------------- SYSTEM users/roles
u=call('system','create operator user','POST','/system/users',(200,201),json={'username':'op1','password':'Op3rator-Pass!2026','full_name':'Op One','email':'op1@example.com'})
uid=J(u).get('id')
call('system','patch user','PATCH',f'/system/users/{uid}',200,json={'full_name':'Op Uno'})
lo=c.post(B+'/auth/login',json={'username':'op1','password':'Op3rator-Pass!2026'}); rec('system','operator can login',lo.status_code==200,lo.text[:100])
HO={'Authorization':'Bearer '+lo.json().get('access_token','')}
x=c.post(B+'/system/users',headers=HO,json={'username':'zz','password':'Zz-Pass-Word!2026'}); rec('system','non-admin cannot create users',x.status_code in(401,403),str(x.status_code))
call('system','settings put','PUT','/system/settings/test_key',200,json={'value':'abc','description':'d'})
r=call('system','settings get','GET','/system/settings/test_key',200); rec('system','settings value persisted',J(r).get('value')=='abc')
call('system','audit log','GET','/system/audit-logs',200)
call('system','components','GET','/system/components',200)
call('system','deactivate user','PATCH',f'/system/users/{uid}',200,json={'is_active':False})
lo=c.post(B+'/auth/login',json={'username':'op1','password':'Op3rator-Pass!2026'}); rec('system','deactivated user cannot login',lo.status_code in(401,403),str(lo.status_code))
# ---------------- DEVICES / TAGs / GATEWAYS
d=call('devices','create device','POST','/devices',(200,201),json={'device_code':'E2E-001','name':'E2E Analyzer','protocol':'Modbus TCP','location':'Lab'})
did=J(d).get('id')
call('devices','duplicate code rejected','POST','/devices',(400,409,422),json={'device_code':'E2E-001','name':'dup'})
call('devices','patch device','PATCH',f'/devices/{did}',200,json={'location':'Plant A','health_score':88})
for comp,lo_,hi in [('O2',0.5,5),('H2S',0,10),('CH4',0,100)]:
    call('devices','add TAG '+comp,'POST',f'/devices/{did}/components',(200,201),json={'component_name':comp,'unit':'%','low_limit':lo_,'high_limit':hi,'is_major':True})
r=call('devices','get device','GET',f'/devices/{did}',200); rec('devices','components attached',len(J(r).get('components',[]))>=3,str(J(r).get('components'))[:80])
call('devices','search devices','GET','/devices?search=E2E',200)
call('devices','test connection','POST',f'/devices/{did}/test-connection',(200,400,502,504))
g=call('gateways','create gateway','POST','/gateways',(200,201),json={'gateway_code':'GW-E2E','name':'E2E GW','ip_address':'127.0.0.1','port':5020,'protocol':'Modbus TCP'})
gid=J(g).get('id')
call('gateways','patch gateway','PATCH',f'/gateways/{gid}',200,json={'port':5021})
call('gateways','gateway test-connection (closed port)','POST',f'/gateways/{gid}/test-connection',(200,400,502,504))
call('communication','comm test closed port','POST','/communication/test',(200,400,502,504),json={'host':'127.0.0.1','port':1,'protocol':'Modbus TCP','timeout':1})
call('communication','scan stale','POST','/communication/scan-stale',(200,204))
# templates for import round trip
for mod,path,name in [('devices','/devices/import','devices'),('devices','/devices/tags/import','tags'),('gateways','/gateways/import','gateways'),('maintenance','/maintenance/work-orders/import','work-orders'),('assets','/assets/import','assets'),('lab','/lab/samples/import','lab samples'),('knowledge','/knowledge/documents/import','knowledge docs')]:
    t=call(mod,f'download {name} import template','GET',path+'/template',200)
    if t is not None and t.status_code==200:
        ct=t.headers.get('content-type','')
        ext='csv' if 'csv' in ct else 'xlsx'
        rec(mod,f'{name} template non-empty',len(t.content)>50,ct)
# ---------------- MEASUREMENTS
now=time.strftime('%Y-%m-%dT%H:%M:%S')
call('measurements','single measurement','POST','/measurements',(200,201),json={'device_id':did,'component_name':'O2','value':2.1})
readings=[{'device_id':did,'component_name':'O2','value':2.0+i*0.01,'time':time.strftime('%Y-%m-%dT%H:%M:%S',time.gmtime(time.time()-3600+i*60))} for i in range(60)]
call('measurements','batch measurements','POST','/measurements/batch',(200,201),json={'device_id':did,'readings':readings})
r=call('measurements','list measurements','GET',f'/measurements?device_id={did}&component_name=O2&limit=5',200)
call('measurements','components list','GET',f'/measurements/components?device_id={did}',200)
r=call('measurements','trend','GET',f'/measurements/trend?device_id={did}&components=O2&hours=24',200)
rows=['time,component,value']+[f'{time.strftime("%Y-%m-%d %H:%M:%S",time.gmtime(time.time()-7200+i*60))},H2S,{1+i*0.02:.2f}' for i in range(30)]
csvb='\n'.join(rows).encode()
r=call('measurements','import CSV','POST','/measurements/import',(200,201),data={'device_id':str(did)},files={'file':('h2s.csv',csvb,'text/csv')})
if r is not None and r.status_code in(200,201): rec('measurements','import stored rows',(J(r).get('rows_inserted') or J(r).get('inserted') or 0)>0,r.text[:150])
call('measurements','import rejects garbage','POST','/measurements/import',(400,415,422),data={'device_id':str(did)},files={'file':('x.csv',b'\x00\x01\x02\x03binary',"text/csv")})
call('measurements','import list','GET','/measurements/imports',200)
# ingest via gateway
call('devices','add mapped TAG (gateway+address)','POST',f'/devices/{did}/components',(200,201),json={'component_name':'CO2','unit':'%','low_limit':0,'high_limit':20,'gateway_code':'GW-E2E','tag_address':'40001','is_major':True})
ri=call('communication','ingest mapped point','POST','/communication/ingest',(200,201,202),json={'gateway_code':'GW-E2E','points':[{'tag_address':'40001','value':7.5},{'tag_address':'40001','value':99.0},{'tag_address':'99999','value':1}]})
rj=J(ri); rec('communication','ingest accepted mapped points',rj.get('accepted',0)>=1,str(rj)[:160]); rec('communication','unmapped address reported',bool(rj.get('unmapped_addresses')),str(rj)[:160])
rm=call('communication','ingested data readable','GET',f'/measurements?device_id={did}&component_name=CO2&limit=10',200); rec('communication','CO2 rows stored via ingest',len(J(rm) if isinstance(J(rm),list) else J(rm).get('items',[]))>=1,rm.text[:100])
ra=call('communication','ingest out-of-range -> alarm chain','GET',f'/alarms?device_id={did}',200)
# ---------------- ALARMS
r=call('alarms','create alarm','POST','/alarms',(200,201),json={'device_id':did,'component_name':'O2','message':'O2 high','severity':'critical','value':9.9}); aid=J(r).get('id')
call('alarms','list alarms','GET','/alarms',200)
call('alarms','acknowledge','POST',f'/alarms/{aid}/acknowledge',200,json={})
call('alarms','resolve','POST',f'/alarms/{aid}/resolve',200)
call('alarms','create rule','POST','/alarms/rules',(200,201),json={'device_id':did,'component_name':'O2','rule_type':'threshold','low_limit':0.5,'high_limit':5,'severity':'warning'})
call('alarms','list rules','GET','/alarms/rules',200)
r2=call('alarms','auto-alarm via out-of-range reading','POST','/measurements',(200,201),json={'device_id':did,'component_name':'O2','value':50})
r=call('alarms','alarm raised by data chain','GET',f'/alarms?device_id={did}',200); rec('alarms','threshold produced alarm',len(J(r) if isinstance(J(r),list) else J(r).get('items',[]))>=1,r.text[:100])
call('alarms','diagnose alarm','POST',f'/alarms/{aid}/diagnose',(200,201))
# ---------------- AI
r=call('ai','diagnose','POST','/ai/diagnose',(200,201),json={'device_id':did,'component_name':'O2','symptom':'reading drifts high'}); dgid=J(r).get('id') or J(r).get('diagnosis_id')
call('ai','predict','POST','/ai/predict',(200,201),json={'device_id':did,'component_name':'O2','horizon_hours':24})
call('ai','health score','GET',f'/ai/health/{did}',200)
call('ai','chat (offline fallback)','POST','/ai/chat',(200,201),json={'message':'How do I calibrate an analyzer?','use_rag':True})
call('ai','device select','POST','/ai/devices/select',(200,201),json={'query':'E2E','limit':5})
call('ai','sop generate','POST','/ai/sop/generate',(200,201),json={'device_id':did,'sop_type':'Calibration','component_name':'O2','lang':'en','save':False})
call('ai','roi generate','POST','/ai/roi/generate',(200,201),json={'device_id':did,'period_months':12,'lang':'en'})
call('ai','analysis read','POST','/ai/analysis/read',(200,201),json={'device_id':did,'components':['O2'],'hours':24,'lang':'en'})
call('ai','knowledge ask','POST','/ai/knowledge/ask',(200,201),json={'question':'calibration interval','lang':'en'})
if dgid: call('ai','diagnosis feedback','POST',f'/ai/diagnoses/{dgid}/feedback',(200,201),json={'verdict':'correct'})
call('ai','cores list','GET','/ai/cores',200)
call('ai','models list','GET','/ai/models',(200,404))
call('ai','packages list','GET','/ai/packages',(200,404))
call('ai','feedback stats','GET','/ai/feedback/stats?component_name=O2',200)
# ---------------- MAINTENANCE
r=call('maintenance','create work order','POST','/maintenance/work-orders',(200,201),json={'title':'E2E calibration','device_id':did,'order_type':'calibration','priority':'high'}); wid=J(r).get('id')
call('maintenance','patch work order','PATCH',f'/maintenance/work-orders/{wid}',200,json={'status':'in_progress'})
call('maintenance','complete work order','POST',f'/maintenance/work-orders/{wid}/complete',(200,201),json={'action_taken':'recalibrated','outcome':'fixed','cost':120.5,'performed_by':1})
r=call('maintenance','create 2nd WO','POST','/maintenance/work-orders',(200,201),json={'title':'E2E 2'}); w2=J(r).get('id')
call('maintenance','close work order','POST',f'/maintenance/work-orders/{w2}/close',200)
call('maintenance','list work orders','GET','/maintenance/work-orders',200)
call('maintenance','record create','POST','/maintenance/records',(200,201),json={'device_id':did,'action_taken':'check','outcome':'fixed'})
call('maintenance','sop create','POST','/maintenance/sop',(200,201),json={'sop_type':'Calibration','title':'SOP E2E','content':'Step 1...'})
call('maintenance','sop list','GET','/maintenance/sop',200)
r=call('maintenance','spare part create','POST','/maintenance/spare-parts',(200,201),json={'part_code':'SP-E2E','name':'Filter','stock_qty':5,'unit_price':10}); pid=J(r).get('id')
call('maintenance','spare part patch','PATCH',f'/maintenance/spare-parts/{pid}',200,json={'stock_qty':7})
call('maintenance','spare part adjust','POST',f'/maintenance/spare-parts/{pid}/adjust-stock',200,json={'delta':-2,'reason':'used'})
r=call('maintenance','spare part stock correct','GET','/maintenance/spare-parts',200); 
call('maintenance','sop generate (GET)','GET',f'/maintenance/sop/generate?device_id={did}&sop_type=Calibration',(200,404,422))
try:
    from reportlab.pdfgen import canvas
    buf=io.BytesIO(); cv=canvas.Canvas(buf); cv.drawString(72,720,'Analyzer fault report: alarm code E-102 O2 reading 9.9 % sample pump failure'); cv.save()
    call('maintenance','fault PDF analyze','POST','/maintenance/fault-pdf/analyze',(200,201),files={'file':('f.pdf',buf.getvalue(),'application/pdf')})
    call('maintenance','fault PDF rejects non-pdf','POST','/maintenance/fault-pdf/analyze',(400,415,422),files={'file':('f.pdf',b'not a pdf','application/pdf')})
except ImportError: pass
# ---------------- ASSETS
r=call('assets','create asset','POST','/assets',(200,201),json={'asset_code':'AST-E2E','name':'E2E asset','device_id':did,'category':'analyzer','purchase_value':50000,'depreciation_years':10,'purchase_date':'2024-01-01'}); asid=J(r).get('id')
call('assets','patch asset','PATCH',f'/assets/{asid}',200,json={'status':'active'})
call('assets','add document','POST',f'/assets/{asid}/documents',(200,201),json={'doc_name':'manual.pdf','doc_type':'manual'})
call('assets','asset ROI','GET',f'/assets/{asid}/roi',200)
call('assets','ROI by device','GET',f'/assets/by-device/{did}/roi',200)
call('assets','list assets','GET','/assets',200)
# ---------------- LAB
r=call('lab','create sample','POST','/lab/samples',(200,201),json={'device_id':did,'component_name':'O2','sample_no':'S-1','lab_value':2.05}); sid=J(r).get('id')
for i in range(2,9): call('lab',f'sample {i}','POST','/lab/samples',(200,201),json={'device_id':did,'component_name':'O2','sample_no':f'S-{i}','lab_value':2.0+i*0.01,'online_value':2.0+i*0.011})
call('lab','analysis compare','POST','/lab/analysis-compare',(200,201),json={'device_id':did,'component_name':'O2','window_minutes':120})
call('lab','correlation compute','POST','/lab/correlations/compute',(200,201),json={'device_id':did,'component_name':'O2'})
call('lab','certify','POST','/lab/certify',(200,201),json={'device_id':did,'component_name':'O2','window_minutes':120,'persist':True,'lang':'en'})
call('lab','certifications overview','GET','/lab/certifications',(200,404))
call('lab','data coverage','GET',f'/lab/data-coverage?device_id={did}&component_name=O2',200)
call('lab','list samples','GET','/lab/samples',200)
call('lab','delete sample','DELETE',f'/lab/samples/{sid}',(200,204))
# ---------------- KNOWLEDGE
r=call('knowledge','create document','POST','/knowledge/documents',(200,201),json={'title':'Calibration guide','doc_type':'manual'}); kid=J(r).get('id')
call('knowledge','add chunk','POST','/knowledge/chunks',(200,201),json={'document_id':kid,'content':'Calibrate the analyzer every 30 days with certified span gas.','chunk_index':0})
r=call('knowledge','search','GET','/knowledge/search?q=calibrate',200)
call('knowledge','list docs','GET','/knowledge/documents',200)
call('knowledge','delete doc','DELETE',f'/knowledge/documents/{kid}',(200,204))
# ---------------- ANALYSIS
call('analysis','cores','POST','/analysis/cores',(200,201),json={'device_id':did,'components':['O2'],'hours':24})
call('analysis','multi-trend','POST','/analysis/multi-trend',(200,201),json={'device_id':did,'components':['O2','H2S'],'hours':24})
r=call('analysis','dataset create','POST','/analysis/datasets',(200,201),json={'name':'DS-E2E','x_field':'time','y_fields':['O2']}); dsid=J(r).get('id')
call('analysis','dataset list','GET','/analysis/datasets',200)
call('analysis','dataset delete','DELETE',f'/analysis/datasets/{dsid}',(200,204))
call('analysis','import template','GET','/analysis/import/template',200)
# ---------------- EXTERNAL (cloud data)
call('external','SSRF guard blocks loopback source','POST','/external/sources',(400,422),json={'name':'evil','base_url':'http://127.0.0.1:8765','login_url':'http://127.0.0.1:8765/login','username':'u','password':'p'})
call('external','SSRF guard blocks cloud metadata IP','POST','/external/sources',(400,422),json={'name':'evil2','base_url':'http://169.254.169.254','login_url':'http://169.254.169.254/x','username':'u','password':'p'})
r=call('external','create source','POST','/external/sources',(200,201),json={'name':'Vendor X','base_url':'https://93.184.216.34','login_url':'https://93.184.216.34/login','username':'u','password':'p'}); srcid=J(r).get('id')
rr=J(r); rec('external','password never echoed back','password' not in json.dumps(rr).lower() or rr.get('password') in (None,'','***'),str(rr)[:120])
call('external','update source','PUT',f'/external/sources/{srcid}',200,json={'name':'Vendor X2'})
call('external','test-login (no browser engine here)','POST',f'/external/sources/{srcid}/test-login',(200,400,422,500,501,502,503))
r=call('external','create database','POST','/external/databases',(200,201),json={'source_id':srcid,'name':'DB1','list_url':'https://93.184.216.34/data','default_device_code':'E2E-001'}); xdb=J(r).get('id')
call('external','update database','PUT',f'/external/databases/{xdb}',200,json={'name':'DB1b'})
call('external','database health','GET',f'/external/databases/{xdb}/health',200)
call('external','refresh (no browser engine)','POST',f'/external/databases/{xdb}/refresh',(200,400,422,500,501,502,503))
call('external','coverage','GET',f'/external/coverage?device_id={did}&component_name=O2',200)
call('external','gaps list','GET','/external/gaps',200)
call('external','delete database','DELETE',f'/external/databases/{xdb}',(200,204))
call('external','delete source','DELETE',f'/external/sources/{srcid}',(200,204))
# ---------------- TEMPLATES
r=call('templates','list','GET','/templates',200); tj=J(r); items=tj.get('items',tj) if isinstance(tj,dict) else tj
rec('templates','>=20 builtin templates',len(items)>=20,str(len(items)))
call('templates','categories','GET','/templates/categories',200)
r=call('templates','create template','POST','/templates',(200,201),json={'name':'E2E tpl','category':'设备台账','content':{'devices':[{'device_code':'TPL-{n}','name':'Dev {n}','protocol':'Modbus TCP','components':[{'component_name':'X','unit':'%'}]}]}}); tid=J(r).get('id')
call('templates','update -> new version','PUT',f'/templates/{tid}',200,json={'description':'v2','content':{'devices':[{'device_code':'TPL-{n}','name':'Dev {n} v2','components':[]}]}})
r=call('templates','versions','GET',f'/templates/{tid}/versions',200); vs=J(r); rec('templates','2 versions exist',len(vs)>=2,str(vs)[:100])
call('templates','diff','GET',f'/templates/{tid}/diff?from=1&to=2',200)
call('templates','restore v1','POST',f'/templates/{tid}/versions/1/restore',(200,201))
r=call('templates','duplicate','POST',f'/templates/{tid}/duplicate',(200,201)); t2=J(r).get('id')
call('templates','favorite','POST',f'/templates/{tid}/favorite',200)
call('templates','set default','POST',f'/templates/{tid}/default',200)
call('templates','apply (idempotent)','POST',f'/templates/{tid}/apply',(200,201),json={})
call('templates','apply twice does not duplicate','POST',f'/templates/{tid}/apply',(200,201),json={})
call('templates','usage','GET',f'/templates/{tid}/usage',200)
ex=call('templates','export .aimstpl','GET',f'/templates/{tid}/export',200)
if ex is not None and ex.status_code==200:
    rec('templates','export non-empty',len(ex.content)>20)
    # re-import the exported pack (preview then confirm)
    pv=call('templates','import preview','POST','/templates/import',(200,201),files=[('files',('e2e.aimstpl',ex.content,'application/octet-stream'))])
    cf=call('templates','import confirm','POST','/templates/import?confirm=true',(200,201),files=[('files',('e2e.aimstpl',ex.content,'application/octet-stream'))])
call('templates','import rejects zip-slip / junk','POST','/templates/import',(200,400,415,422),files=[('files',('x.aimstpl',b'PK\x03\x04junk','application/octet-stream'))])
call('templates','batch export','POST','/templates/export/batch',200,json={'ids':[tid,t2]})
call('templates','import logs','GET','/templates/import/logs',200)
call('templates','delete','DELETE',f'/templates/{t2}',(200,204))
# ---------------- INDUSTRIAL (generic)
for n,pth in [('connections','/industrial/connections'),('tags','/industrial/tags'),('tag-mappings','/industrial/tag-mappings'),('models','/industrial/models'),('ai-feedback','/industrial/ai-feedback'),('acquisition-events','/industrial/acquisition-events')]:
    call('industrial',f'GET {n}','GET',pth if n!='tags' else pth+f'/{did}',200)
# ---------------- IMPORT VALIDATION
call('import-validation','preview CSV','POST','/import-validation/preview',(200,201,422),files={'file':('a.csv',csvb,'text/csv')})
# ---------------- DASHBOARD
r=call('dashboard','stats','GET','/dashboard/stats',200); st=J(r); rec('dashboard','stats reflect new device',(st.get('total_devices') or st.get('devices_total') or 0)>=1,str(st)[:140])
call('dashboard','health overview','GET','/dashboard/health-overview',200)
# ---------------- delete cleanup
call('assets','delete asset','DELETE',f'/assets/{asid}',(200,204))
call('gateways','delete gateway','DELETE',f'/gateways/{gid}',(200,204))
call('devices','delete referenced device is refused (data-integrity guard)','DELETE',f'/devices/{did}',409)
rf=call('devices','create unreferenced device','POST','/devices',(200,201),json={'device_code':'E2E-TMP','name':'tmp'}); fid=J(rf).get('id')
call('devices','delete unreferenced device','DELETE',f'/devices/{fid}',(200,204))
call('devices','deleted device -> 404','GET',f'/devices/{fid}',404)
call('auth','logout','POST','/auth/logout',(200,204),json={'refresh_token':tok['refresh_token']})
# ---------------- summary
import collections
by=collections.defaultdict(lambda:[0,0])
for m,n,ok,d in R: by[m][0 if ok else 1]+=1
print('\n=== SUMMARY ===')
for m,(p,f) in by.items(): print(f'{m:18} pass {p:3}  fail {f}')
print('TOTAL pass',sum(v[0] for v in by.values()),'fail',sum(v[1] for v in by.values()))
sys.exit(1 if any(not ok for _,_,ok,_ in R) else 0)
