"""Exercise one-click login and the independent periodic switch with a fake portal."""
import importlib.util
import json
from pathlib import Path
import tempfile
import tkinter as tk
import time
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('campus',Path(__file__).resolve().parents[1] / 'campus_login.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
fixture=json.loads(Path(__file__).with_name('rsa-fixtures.json').read_text('utf8'))
adapter={'index':8,'ips':['10.0.0.2'],'guid':'{TEST-ADAPTER}','name':'测试以太网','up':True}
calls=[]
steps=[]
class Fake:
    def __init__(self,a):self.online_now=False
    def online(self):return self.online_now,{}
    def discover(self):return m.PORTAL+'/eportal/index.jsp?wlanuserip=10.0.0.2&mac=aa-bb'
    def api(self,method,fields=None,raw_body=None):
        calls.append(method)
        if method=='pageInfo':return fixture['page']
        self.online_now=True
        return {'result':'success'}

with tempfile.TemporaryDirectory(prefix='hust-ui-test-') as tmp:
    m.DATA=Path(tmp);m.CONFIG=m.DATA/'settings.json';m.STATE=m.DATA/'status.json'
    m.write_json(m.CONFIG,dict(m.DEFAULTS,enabled=True,secret=m.crypt(json.dumps({'username':'demo-user','password':'Dummy-test-password'}).encode()).hex()))
    original=tk.Tk;real_cycle=m.run_cycle;result={};errors=[]
    def simulated_cycle(diagnostic=False,manual=False):
        return real_cycle(diagnostic,lambda guid:adapter,Fake,manual=manual)
    def factory():
        root=original();root.withdraw()
        root.report_callback_exception=lambda *args:errors.append(args[0].__name__)
        def widgets(w):
            for child in w.winfo_children():
                yield child
                yield from widgets(child)
        def login():
            b=next(w for w in widgets(root) if getattr(w,'text',None)=='一键登录')
            steps.append({'action':'login','enabled':b.enabled})
            b.invoke()
        def pause():
            next(w for w in widgets(root) if type(w).__name__=='Switch').command()
        def audit():
            state=m.read_json(m.STATE,{})
            result.update(title=root.title(),login_submissions=calls.count('login'),periodic_enabled=m.read_json(m.CONFIG,{})['enabled'],code=state.get('code'),callback_errors=errors,steps=steps)
            root.destroy()
        deadline=[None]
        phase=[0]
        def drive():
            if deadline[0] is None:deadline[0]=time.monotonic()+10
            b=next(w for w in widgets(root) if getattr(w,'text',None)=='一键登录')
            if time.monotonic()>deadline[0]:
                audit();return
            if b.enabled:
                if phase[0]==0:login();phase[0]=1
                elif phase[0]==1:pause();login();phase[0]=2
                else:audit();return
            root.after(150,drive)
        root.after_idle(drive)
        return root
    with patch.object(m,'adapters',return_value=[adapter]),patch.object(m,'run_cycle',side_effect=simulated_cycle),patch.object(tk,'Tk',side_effect=factory):m.gui()
    assert result['login_submissions']==2,result
    assert result['periodic_enabled'] is False,result
    assert result['code']=='RECONNECTED',result
    assert result['callback_errors']==[],result
    print(json.dumps(result,ensure_ascii=True))
    import logging
    logging.shutdown()
