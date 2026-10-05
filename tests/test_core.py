import importlib.util
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, unquote

spec = importlib.util.spec_from_file_location('campus', Path(__file__).resolve().parents[1] / 'campus_login.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
FIXTURES = json.loads(Path(__file__).with_name('rsa-fixtures.json').read_text('utf8'))
ADAPTER = {'index':8,'ips':['10.0.0.2']}
URL = m.PORTAL + '/eportal/index.jsp?wlanuserip=10.0.0.2&mac=aa-bb-cc-dd-ee-ff'


class FakePortal:
    online_now = False
    redirect = URL
    reply = {'result':'success'}
    page = FIXTURES['page']
    requests = []

    def __init__(self, adapter): pass
    def online(self): return self.online_now, {}
    def discover(self): return self.redirect
    def api(self, method, fields=None, raw_body=None):
        self.requests.append(method)
        if method == 'pageInfo': return self.page
        if self.reply['result'] == 'success': self.online_now = True
        return self.reply


class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='campus-test-')
        self.data = Path(self.tmp.name)
        self.patchers = [patch.object(m,'DATA',self.data), patch.object(m,'CONFIG',self.data/'settings.json'),
                         patch.object(m,'STATE',self.data/'state.json'), patch.object(m,'make_logger')]
        for p in self.patchers: p.start()
        FakePortal.online_now=False
        FakePortal.redirect=URL
        FakePortal.reply={'result':'success'}
        FakePortal.page=FIXTURES['page']
        FakePortal.requests=[]
        secret = m.crypt(json.dumps({'username':'fake+账号','password':'Fake Test P@ssword'}).encode()).hex()
        m.write_json(m.CONFIG,dict(m.DEFAULTS, enabled=True, secret=secret))

    def tearDown(self):
        for p in self.patchers: p.stop()
        self.tmp.cleanup()

    def cycle(self, diagnostic=False):
        return m.run_cycle(diagnostic, lambda guid: ADAPTER, FakePortal)

    def test_dpapi_roundtrip(self):
        value = b'fake credential\x00utf8'
        encrypted = m.crypt(value)
        self.assertNotIn(value, encrypted)
        self.assertEqual(m.crypt(encrypted, True), value)

    def test_first_setup_selects_connected_physical_adapter(self):
        choices=[dict(ADAPTER,guid='disconnected',up=False),dict(ADAPTER,guid='connected',up=True)]
        with patch.object(m,'adapters',return_value=choices):
            self.assertEqual(m.get_adapter('')['guid'],'connected')

    def test_saved_adapter_never_silently_changes(self):
        choices=[dict(ADAPTER,guid='different',up=True)]
        with patch.object(m,'adapters',return_value=choices):
            with self.assertRaises(m.SafeError):m.get_adapter('saved-guid')

    def test_no_connected_adapter_waits(self):
        with patch.object(m,'adapters',return_value=[]):
            with self.assertRaises(m.SafeError):m.get_adapter('')

    def test_task_and_gui_share_lock(self):
        with m.single_instance() as first:
            self.assertTrue(first)
            with m.single_instance() as second:
                self.assertFalse(second)
        with m.single_instance() as released:
            self.assertTrue(released)

    def test_rsa_matches_actual_portal_js(self):
        for c in FIXTURES['cases']:
            self.assertEqual(m.rsa_password(c['password'],c['query'],FIXTURES['page']),c['expected'])

    def test_double_encoding(self):
        secret={'username':'a+b&c中文','password':'Fake!'}
        body=m.login_body(secret,'校园服务',URL,FIXTURES['page']).decode()
        fields={k:unquote(v[0]) for k,v in parse_qs(body,keep_blank_values=True).items()}
        self.assertEqual(fields['userId'],secret['username'])
        self.assertEqual(fields['service'],'校园服务')
        self.assertEqual(fields['queryString'],m.urlsplit(URL).query)
        self.assertNotIn(secret['password'],body)

    def test_online_does_not_login(self):
        FakePortal.online_now=True
        self.assertEqual(self.cycle()['code'],'ONLINE')
        self.assertEqual(FakePortal.requests,[])

    def test_unknown_network_does_not_login(self):
        FakePortal.redirect=None
        self.assertEqual(self.cycle()['code'],'WAIT_NETWORK')
        self.assertEqual(FakePortal.requests,[])

    def test_realistic_reconnect(self):
        self.assertEqual(self.cycle()['code'],'RECONNECTED')
        self.assertEqual(FakePortal.requests,['pageInfo','login'])

    def test_diagnostic_never_submits(self):
        self.assertEqual(self.cycle(True)['code'],'LOGIN_REQUIRED')
        self.assertEqual(FakePortal.requests,[])

    def test_captcha_never_submits(self):
        FakePortal.page=dict(FIXTURES['page'],validCodeUrl='captcha.jpg')
        self.assertEqual(self.cycle()['code'],'CAPTCHA')
        self.assertEqual(FakePortal.requests,['pageInfo'])

    def test_backoff_keeps_retrying_after_monthly_outage(self):
        FakePortal.reply={'result':'fail','message':'temporary failure'}
        self.assertEqual(self.cycle()['code'],'LOGIN_FAILED')
        self.assertEqual(self.cycle()['code'],'BACKOFF')
        for _ in range(2):
            state=m.read_json(m.STATE,{})
            state['retry_after']=0
            m.write_json(m.STATE,state)
            self.cycle()
        self.assertEqual(m.read_json(m.STATE,{})['code'],'LOGIN_FAILED')
        self.assertFalse(m.read_json(m.STATE,{})['blocked'])
        count=FakePortal.requests.count('login')
        self.cycle()
        self.assertEqual(FakePortal.requests.count('login'),count)
        self.assertEqual(count,3)
        state=m.read_json(m.STATE,{})
        state['retry_after']=0
        m.write_json(m.STATE,state)
        FakePortal.reply={'result':'success'}
        self.assertEqual(self.cycle()['code'],'RECONNECTED')

    def test_wrong_password_stops(self):
        FakePortal.reply={'result':'fail','message':'密码错误'}
        self.assertTrue(self.cycle()['blocked'])
        self.cycle()
        self.assertEqual(FakePortal.requests.count('login'),1)

    def test_paused(self):
        c=m.read_json(m.CONFIG,{})
        c['enabled']=False
        m.write_json(m.CONFIG,c)
        self.assertEqual(self.cycle()['code'],'PAUSED')
        self.assertEqual(FakePortal.requests,[])

    def test_one_click_login_when_periodic_checks_are_paused(self):
        c=m.read_json(m.CONFIG,{})
        c['enabled']=False
        m.write_json(m.CONFIG,c)
        result=m.run_cycle(adapter_provider=lambda guid:ADAPTER,portal_factory=FakePortal,manual=True)
        self.assertEqual(result['code'],'RECONNECTED')
        self.assertFalse(m.read_json(m.CONFIG,{})['enabled'])

    def test_one_click_when_online_does_not_repeat_authentication(self):
        FakePortal.online_now=True
        result=m.run_cycle(adapter_provider=lambda guid:ADAPTER,portal_factory=FakePortal,manual=True)
        self.assertEqual(result['code'],'ONLINE')
        self.assertEqual(FakePortal.requests,[])

    def test_allowed_redirects(self):
        self.assertTrue(m.login_url(URL,'10.0.0.2'))
        self.assertFalse(m.login_url(URL,'10.17.207.25'))
        self.assertFalse(m.login_url(URL.replace('172.18.18.60','example.com'),'10.0.0.2'))
        self.assertFalse(m.login_url(m.PORTAL+'/eportal/success.jsp?userIndex=old','10.0.0.2'))
        self.assertEqual(m.redirect_from(b'<script>location.href="http://test/?x=1&amp;y=2"</script>'),'http://test/?x=1&y=2')

    def test_reject_encryption_downgrade(self):
        with self.assertRaises(m.SafeError):
            m.rsa_password('fake','',dict(FIXTURES['page'],passwordEncrypt='false'))

    def test_online_falls_back_to_current_token(self):
        client=m.Portal(ADAPTER)
        with patch.object(client,'api',side_effect=[{'result':'wait'}, {'result':'success','userIp':'10.0.0.2'}]), patch.object(client,'request',return_value=(302,m.PORTAL+'/eportal/success.jsp?userIndex=current-test-token',b'')):
            self.assertTrue(client.online()[0])


if __name__=='__main__': unittest.main(verbosity=2)
