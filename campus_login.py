"""Campus ePortal auto-login. Python 3.10+, standard library, Windows only.

Protocol matched to this portal's AuthInterFace.js/login_bch.js/security.js.
Never logs out; never posts credentials to a redirect target.
"""
from __future__ import annotations

import argparse
import contextlib
import ctypes
from ctypes import wintypes
import datetime as dt
import hashlib
import html
import http.client
import ipaddress
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import queue
import re
import socket
import subprocess
import sys
import threading
import time
from urllib.parse import quote, parse_qs, urljoin, urlsplit

PORTAL = "http://172.18.18.60:8080"
HOST = "172.18.18.60"
DEFAULT_GUID = ""  # First setup chooses a connected physical adapter; saved choices stay explicit.
VERSION = "1.3.1"
CONTRIBUTOR = "HenricWu"
CONTACT_EMAIL = "haoyangwu@hust.edu.cn"
# ProgramData avoids packaged-app AppData virtualization: GUI and Scheduler
# must see exactly the same files, including when launched from Codex/MSIX.
DATA = Path(os.environ.get("ProgramData", "C:/ProgramData")) / "CampusAutoLogin" / "data" / os.environ.get("USERNAME", "default")
CONFIG = DATA / "settings.json"
STATE = DATA / "status.json"
DEFAULTS = {"version": 2, "enabled": False, "interface_guid": DEFAULT_GUID,
            "service": "", "secret": "", "secret_scope": "machine"}


class SafeError(Exception):
    """Only fixed, non-sensitive error messages may be placed in this exception."""


def powershell(script: str):
    p = subprocess.run(["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive",
                        "-Command", "[Console]::OutputEncoding=[Text.UTF8Encoding]::new();"
                        "$ErrorActionPreference='Stop';" + script],
                       capture_output=True, creationflags=0x08000000, timeout=25)
    if p.returncode:
        raise SafeError("无法读取 Windows 网络信息，请检查网卡状态。")
    return p.stdout.decode("utf-8-sig").strip()


def adapters():
    s = powershell("@(Get-NetAdapter -Physical | ForEach-Object {"
                   "$a=$_; $ips=@(Get-NetIPAddress -InterfaceIndex $a.ifIndex "
                   "-AddressFamily IPv4 -ErrorAction SilentlyContinue | Where-Object "
                   "{ $_.IPAddress -notlike '169.254.*' } | Select-Object -ExpandProperty IPAddress);"
                   "[pscustomobject]@{name=$a.Name;guid=[string]$a.InterfaceGuid;"
                   "index=$a.ifIndex;up=($a.Status -eq 'Up');ips=$ips}"
                   "}) | ConvertTo-Json -Compress -Depth 4")
    values = json.loads(s or "[]")
    return values if isinstance(values, list) else [values]


def get_adapter(guid):
    available = adapters()
    if not guid:
        connected = [a for a in available if a["up"] and a["ips"]]
        if connected:
            return connected[0]
        raise SafeError("未检测到已连接的网卡，请连接校园网后重试。")
    for a in available:
        if a["guid"].strip("{}").lower() == guid.strip("{}").lower():
            if a["up"] and a["ips"]:
                return a
            raise SafeError("校园网网卡未连接，等待网线连接或电脑唤醒。")
    raise SafeError("没有找到已保存的校园网网卡，请在设置中重新选择。")


class Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def crypt(data: bytes, decrypt=False) -> bytes:
    buf = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    src = Blob(len(data), buf)
    dst = Blob()
    lib = ctypes.WinDLL("crypt32", use_last_error=True)
    fn = lib.CryptUnprotectData if decrypt else lib.CryptProtectData
    fn.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                   ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    fn.restype = wintypes.BOOL
    # Machine-bound DPAPI plus restricted directory ACL lets SYSTEM reconnect
    # before interactive logon, which is necessary for unattended remote access.
    flags = 1 if decrypt else 1 | 4  # UI_FORBIDDEN | LOCAL_MACHINE
    if not fn(ctypes.byref(src), None, None, None, None, flags, ctypes.byref(dst)):
        raise SafeError("Windows 无法解密本机保存的账号，请重新填写并保存。")
    try:
        return ctypes.string_at(dst.pbData, dst.cbData)
    finally:
        local_free = ctypes.WinDLL("kernel32").LocalFree
        local_free.argtypes = [ctypes.c_void_p]
        local_free.restype = ctypes.c_void_p
        local_free(dst.pbData)


def read_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return dict(default)


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)


@contextlib.contextmanager
def single_instance():
    import msvcrt
    # A filesystem byte lock is shared by the user's desktop and SYSTEM session 0.
    # Local named mutexes would be separate in those sessions.
    DATA.mkdir(parents=True, exist_ok=True)
    with (DATA / "worker.lock").open("a+b") as handle:
        if os.fstat(handle.fileno()).st_size == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            acquired = True
        except OSError:
            acquired = False
        try:
            yield acquired
        finally:
            if acquired:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def decode(data):
    # Portal pages use GBK; its JSON API normally uses UTF-8.
    for enc in ("utf-8-sig", "gb18030"):
        try:
            return data.decode(enc)
        except UnicodeError:
            pass
    return data.decode("utf-8", "replace")


def is_portal(url):
    p = urlsplit(url)
    return (p.scheme == "http" and p.hostname == HOST and p.port in (8080, 8081)
            and not p.username and not p.password)


def login_url(url, local_ip):
    if not is_portal(url):
        return False
    p = urlsplit(url)
    qs = parse_qs(p.query)
    if not p.path.endswith("/index.jsp") or not qs or "userIndex" in qs:
        return False
    if not set(qs).intersection({"wlanuserip", "userip", "ip", "wlanacname", "wlanacip", "mac", "nasip"}):
        return False
    for key in ("wlanuserip", "userip", "ip"):
        if key in qs:
            try:
                given = str(ipaddress.ip_address(qs[key][0]))
            except ValueError:
                continue  # Some gateways encode this value.
            if given != local_ip:
                return False
    return True


def redirect_from(body):
    text = html.unescape(decode(body))
    patterns = [r'(?:window\.|top\.|document\.)?location(?:\.href)?\s*=\s*[\x22\x27]([^\x22\x27]+)',
                r'location\.(?:replace|assign)\(\s*[\x22\x27]([^\x22\x27]+)',
                r'(?i)url\s*=\s*(http://[^\s\x22\x27<>]+)']
    for pattern in patterns:
        m = re.search(pattern, text)
        if m:
            return m[1].replace("\\/", "/")
    return None


class BoundHTTP(http.client.HTTPConnection):
    def __init__(self, host, port, adapter):
        super().__init__(host, port, timeout=5)
        self.adapter = adapter

    def connect(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.settimeout(self.timeout)
            # IP_UNICAST_IF pins traffic to the selected Windows interface.
            sock.setsockopt(socket.IPPROTO_IP, 31, socket.htonl(int(self.adapter["index"])))
            sock.bind((self.adapter["ips"][0], 0))
            sock.connect((self.host, self.port))
            self.sock = sock
        except Exception:
            sock.close()
            raise


class Portal:
    def __init__(self, adapter):
        self.adapter = adapter

    def request(self, url, body=None):
        p = urlsplit(url)
        if p.scheme != "http" or p.username or p.password:
            raise SafeError("检测到不支持的校园网跳转地址。")
        if body is not None and not is_portal(url):
            raise SafeError("登录接口地址不匹配，已停止提交账号。")
        c = BoundHTTP(p.hostname, p.port or 80, self.adapter)
        try:
            c.request("POST" if body is not None else "GET",
                      p.path + ("?" + p.query if p.query else "") or "/", body=body,
                      headers={"User-Agent": "CampusAutoLogin/1.0", "Cache-Control": "no-cache",
                               "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"})
            r = c.getresponse()
            data = r.read(1024 * 1024 + 1)
            if len(data) > 1024 * 1024:
                raise SafeError("校园网页面返回异常，请稍后重试。")
            return r.status, r.getheader("Location"), data
        finally:
            c.close()

    def api(self, method, fields=None, raw_body=None):
        if method not in {"getOnlineUserInfo", "pageInfo", "login"}:
            raise SafeError("不支持的接口。")
        body = raw_body if raw_body is not None else encode_form(fields or {}, 1)
        status, _, data = self.request(PORTAL + "/eportal/InterFace.do?method=" + method, body)
        if status != 200:
            raise SafeError("校园网接口暂时不可用，稍后自动重试。")
        try:
            value = json.loads(decode(data))
            if not isinstance(value, dict):
                raise ValueError()
            return value
        except (ValueError, UnicodeError):
            raise SafeError("校园网接口返回格式异常，稍后自动重试。") from None

    def online(self):
        info = self.api("getOnlineUserInfo", {"userIndex": ""})
        valid = info.get("result") == "success" and info.get("userIp") == self.adapter["ips"][0]
        if not valid:
            # This deployment can initially return 'wait' for an empty token.
            # The portal's own redirect supplies the current token without logging out.
            url = PORTAL + "/"
            for _ in range(4):
                status, location, body = self.request(url)
                target = location if status in (301, 302, 303, 307, 308) else redirect_from(body)
                if not target:
                    break
                target = urljoin(url, target)
                if not is_portal(target):
                    break
                token = parse_qs(urlsplit(target).query).get("userIndex", [""])[0]
                if token:
                    info = self.api("getOnlineUserInfo", {"userIndex": token})
                    valid = info.get("result") == "success" and info.get("userIp") == self.adapter["ips"][0]
                    break
                if login_url(target, self.adapter["ips"][0]):
                    break
                url = target
        return valid, info

    def discover(self):
        # Fetch fresh gateway parameters for every login. No stale query is reused.
        for start in (PORTAL + "/", "http://1.1.1.1/", "http://www.msftconnecttest.com/connecttest.txt"):
            try:
                url = start
                for _ in range(5):
                    if login_url(url, self.adapter["ips"][0]):
                        return url
                    status, location, body = self.request(url)
                    target = location if status in (301, 302, 303, 307, 308) else redirect_from(body)
                    if not target:
                        break
                    target = urljoin(url, target)
                    if not is_portal(target):
                        break
                    url = target
            except (OSError, http.client.HTTPException, SafeError):
                continue
        return None


def encode_form(fields, levels):
    result = []
    for key, value in fields.items():
        v = str(value)
        for _ in range(levels):
            v = quote(v, safe="~!*'()-._")  # JavaScript encodeURIComponent
        result.append(key + "=" + v)
    return "&".join(result).encode("ascii")


def rsa_password(password, query, page):
    if str(page.get("passwordEncrypt", "")).lower() != "true":
        raise SafeError("校园网密码加密配置已变化，自动登录已停止。")
    try:
        exponent = int(page["publicKeyExponent"], 16)
        modulus = int(page["publicKeyModulus"], 16)
        if modulus.bit_length() < 1024 or exponent < 3:
            raise ValueError()
    except (KeyError, ValueError, TypeError):
        raise SafeError("校园网未返回有效的密码加密参数。") from None
    match = re.search(r"(?:^|&)mac=([^&]+)", query, re.I)
    mac = match[1] if match else "111111111"
    # This site's login_bch.js encrypts reverse(password + '>' + raw MAC).
    b = (password + ">" + mac).encode("utf-16-le", "surrogatepass")
    units = [int.from_bytes(b[i:i+2], "little") for i in range(0, len(b), 2)][::-1]
    chunk = 2 * ((modulus.bit_length() - 1) // 16)
    units += [0] * ((-len(units)) % chunk)
    blocks = []
    for offset in range(0, len(units), chunk):
        limbs = [units[offset+k] + (units[offset+k+1] << 8) for k in range(0, chunk, 2)]
        block = sum(v << (16*i) for i, v in enumerate(limbs))
        if any(v > 65535 for v in limbs):
            # Preserve the site's legacy JS uint32 carry behavior for Unicode.
            # After the first square, all limbs are normal 16-bit values.
            while len(limbs) > 1 and not limbs[-1]:
                limbs.pop()
            product = [0] * (2 * len(limbs) + 1)
            for i, y in enumerate(limbs):
                carry = 0
                for j, x in enumerate(limbs):
                    uv = product[i+j] + x*y + carry
                    product[i+j] = uv & 65535
                    carry = (uv & 0xffffffff) >> 16
                product[i+len(limbs)] = carry
            square = sum(v << (16*i) for i, v in enumerate(product))
            encrypted = ((block if exponent & 1 else 1) * pow(square, exponent >> 1, modulus)) % modulus
        else:
            encrypted = pow(block, exponent, modulus)
        value = format(encrypted, "x")
        blocks.append(value.zfill(((len(value)+3)//4)*4))
    return " ".join(blocks)


def login_body(secret, service, url, page):
    query = urlsplit(url).query
    return encode_form({"userId": secret["username"],
                        "password": rsa_password(secret["password"], query, page),
                        "service": service, "queryString": query, "operatorPwd": "",
                        "operatorUserId": "", "validcode": "", "passwordEncrypt": "true"}, 2)


def make_logger():
    DATA.mkdir(parents=True, exist_ok=True)
    log = logging.getLogger("campus")
    if not log.handlers:
        h = RotatingFileHandler(DATA / "activity.log", maxBytes=256000, backupCount=2, encoding="utf-8")
        h.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        log.addHandler(h)
        log.setLevel(logging.INFO)
    return log


def report(state, code, message, **updates):
    changed = state.get("code") != code
    state.update(updates, code=code, message=message,
                 checked_at=dt.datetime.now().astimezone().isoformat(timespec="seconds"))
    write_json(STATE, state)
    if changed:
        make_logger().info("%s %s", code, message)
    return dict(state)


def run_cycle(diagnostic=False, adapter_provider=get_adapter, portal_factory=Portal, manual=False):
    with single_instance() as acquired:
        if not acquired:
            return {"message": "后台正在检测，请稍后查看状态。"}
        config = read_json(CONFIG, DEFAULTS)
        state = read_json(STATE, {})
        if not diagnostic and not manual and not config.get("enabled"):
            return report(state, "PAUSED", "自动登录已暂停。")
        if not diagnostic and not config.get("secret"):
            return report(state, "NEEDS_SETUP", "请打开设置，填写校园网账号密码后启用。")
        try:
            adapter = adapter_provider(config["interface_guid"])
            client = portal_factory(adapter)
            online, _ = client.online()
            if online:
                return report(state, "ONLINE", "校园网认证在线。", failures=0, retry_after=0, blocked=False)
            url = client.discover()
            if not url:
                return report(state, "WAIT_NETWORK", "尚未获得校园网登录入口；等待网络恢复或认证页面跳转。")
            if diagnostic:
                return report(state, "LOGIN_REQUIRED", "校园网要求重新登录，已找到新的认证入口。")
            if state.get("blocked"):
                return report(state, "NEEDS_ATTENTION", "认证服务器提示账号或密码有误，请检查后重新保存设置。")
            if time.time() < state.get("retry_after", 0):
                return report(state, "BACKOFF", "上次登录未成功，等待下一次重试。")
            page = client.api("pageInfo", {"queryString": urlsplit(url).query})
            if page.get("validCodeUrl"):
                return report(state, "CAPTCHA", "校园网要求验证码，请先在网页完成验证。")
            # Reload immediately before authentication in case settings changed.
            config = read_json(CONFIG, DEFAULTS)
            if (not manual and not config.get("enabled")) or not config.get("secret"):
                return report(state, "PAUSED", "自动登录已暂停。")
            secret = json.loads(crypt(bytes.fromhex(config["secret"]), decrypt=True))
            body = login_body(secret, config.get("service", ""), url, page)
            # Persist attempt time before POST, so interrupted/time-out requests are not flooded.
            report(state, "LOGIN_ATTEMPT", "正在提交校园网登录。", retry_after=time.time()+300)
            reply = client.api("login", raw_body=body)
            del secret, body
            if reply.get("result") == "success":
                # A successful POST alone is not confirmation of the new session.
                verified, _ = client.online()
                if verified:
                    return report(state, "RECONNECTED", "已自动登录，校园网认证已恢复。",
                                  failures=0, blocked=False, retry_after=0,
                                  last_login=dt.datetime.now().astimezone().isoformat(timespec="seconds"))
                return report(state, "VERIFY_PENDING", "登录接口已接受，等待校园网确认在线状态。")
            failures = state.get("failures", 0) + 1
            message = str(reply.get("message", "")).lower()
            bad_account = any(t in message for t in ("密码错误", "密码不正确", "用户不存在", "口令错误", "password error", "invalid password"))
            blocked = bad_account
            delay_minutes = (5, 15, 60, 180, 360)[min(failures-1, 4)]
            return report(state, "NEEDS_ATTENTION" if blocked else "LOGIN_FAILED",
                          "认证失败，请检查账号、密码和费用后重新保存设置。" if blocked else
                          f"校园网暂未接受登录，将在 {delay_minutes} 分钟后自动重试。",
                          failures=failures, blocked=blocked,
                          retry_after=time.time() + delay_minutes*60)
        except SafeError as e:
            return report(state, "WAITING", str(e))
        except (OSError, http.client.HTTPException):
            return report(state, "WAIT_NETWORK", "暂时无法连接校园网认证服务器，稍后自动重试。")
        except Exception:
            # Raw exceptions may contain request bodies or tokens; never log them.
            return report(state, "ERROR", "本次检测发生异常，请检查设置；后台稍后重试。")


def gui():
    import tkinter as tk
    from tkinter import ttk, messagebox
    from tkinter.font import Font

    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("CampusConnect.AutoLogin.1")
    except OSError:
        pass
    # Render text and the seal at physical resolution instead of Windows bitmap stretching.
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        ctypes.windll.user32.SetProcessDPIAware()
    root = tk.Tk()
    root.title("华中科技大学校园网自动登录")
    scale = min(max(1.0, root.winfo_fpixels("1i") / 96),
                max(1.0, (root.winfo_screenheight() - 100) / 810))
    root.tk.call("tk", "scaling", scale * 96 / 72)
    root.geometry(f"{round(1020*scale)}x{round(810*scale)}")
    root.minsize(round(980*scale), round(790*scale))
    colors = {"bg": "#FFFFFF", "card": "#FFFFFF", "ink": "#202429",
              "muted": "#737980", "line": "#E7E9EC", "blue": "#24292F",
              "blue_hover": "#3B4148", "blue_soft": "#F3F4F5",
              "green": "#178568", "green_soft": "#EFF8F4",
              "amber": "#A97624", "amber_soft": "#FBF6EC", "field": "#FAFAFB"}
    font = "Microsoft YaHei UI"
    root.configure(bg=colors["bg"])
    asset_dir = Path(__file__).resolve().parent
    if (asset_dir / "campus-icon.ico").exists():
        root.iconbitmap(str(asset_dir / "campus-icon.ico"))
    root.option_add("*Font", (font, 10))
    root.option_add("*TCombobox*Listbox.font", (font, 10))
    root.option_add("*TCombobox*Listbox.background", "white")
    root.option_add("*TCombobox*Listbox.selectBackground", colors["blue_soft"])
    root.option_add("*TCombobox*Listbox.selectForeground", colors["blue"])
    style = ttk.Style()
    style.theme_use("clam")
    style.configure("Modern.TCombobox", fieldbackground=colors["field"], background=colors["field"],
                    foreground=colors["ink"], arrowcolor=colors["muted"], bordercolor=colors["line"],
                    lightcolor=colors["line"], darkcolor=colors["line"], padding=(12, 9), arrowsize=14, borderwidth=1)
    style.map("Modern.TCombobox", fieldbackground=[("readonly", colors["field"])],
              selectbackground=[("readonly", colors["field"])], selectforeground=[("readonly", colors["ink"])],
              bordercolor=[("focus", colors["blue"])])

    def rounded(canvas, x1, y1, x2, y2, radius, **kwargs):
        return canvas.create_polygon(x1+radius,y1,x2-radius,y1,x2,y1,x2,y1+radius,
                                     x2,y2-radius,x2,y2,x2-radius,y2,x1+radius,y2,
                                     x1,y2,x1,y2-radius,x1,y1+radius,x1,y1,
                                     smooth=True, splinesteps=30, **kwargs)

    def symbol(c, kind, x, y, color, size=16):
        s=size/20
        def line(*points):
            c.create_line(*[v for i in range(0,len(points),2) for v in (x+points[i]*s,y+points[i+1]*s)],
                          fill=color,width=1.6*s,capstyle="round",joinstyle="round")
        def oval(a,b,d,e):
            c.create_oval(x+a*s,y+b*s,x+d*s,y+e*s,outline=color,width=1.5*s)
        def rect(a,b,d,e):
            rounded(c,x+a*s,y+b*s,x+d*s,y+e*s,2*s,fill="",outline=color,width=1.5*s)
        if kind=="check": line(-7,0,-2,5,7,-5)
        elif kind=="refresh":
            c.create_arc(x-7*s,y-7*s,x+7*s,y+7*s,start=45,extent=285,style="arc",outline=color,width=1.6*s)
            line(6,-8,7,-3,2,-3)
        elif kind=="pause": line(-3,-6,-3,6); line(3,-6,3,6)
        elif kind=="play": line(-4,-6,5,0,-4,6,-4,-6)
        elif kind=="log":
            rect(-6,-8,6,8); line(-3,-3,3,-3); line(-3,1,3,1); line(-3,5,1,5)
        elif kind=="user":
            oval(-3,-8,3,-2)
            c.create_arc(x-7*s,y-1*s,x+7*s,y+13*s,start=0,extent=180,style="arc",outline=color,width=1.5*s)
        elif kind=="lock":
            rect(-6,-1,6,8)
            c.create_arc(x-4*s,y-8*s,x+4*s,y+3*s,start=0,extent=180,style="arc",outline=color,width=1.5*s)
            line(-4,-3,-4,-1); line(4,-3,4,-1); line(0,3,0,5)
        elif kind=="network":
            rect(-7,-7,7,3); line(0,3,0,8); line(-5,8,5,8)
        elif kind=="globe":
            oval(-8,-8,8,8); oval(-3,-8,3,8); line(-8,0,8,0)

    class Card(tk.Canvas):
        def __init__(self, parent, padding=24, **kwargs):
            super().__init__(parent, bg=colors["bg"], highlightthickness=0, bd=0, **kwargs)
            self.padding = padding
            self.inner = tk.Frame(self, bg="white")
            self.window = self.create_window(padding, padding, anchor="nw", window=self.inner)
            self.bind("<Configure>", self.resize)

        def resize(self, e):
            self.delete("card")
            rounded(self, 1, 1, e.width-1, e.height-1, 16, fill="white", outline=colors["line"], tags="card")
            self.tag_lower("card")
            self.itemconfigure(self.window, width=max(1,e.width-2*self.padding), height=max(1,e.height-2*self.padding))

    class Button(tk.Canvas):
        def __init__(self, parent, text, command, width=118, primary=False, quiet=False, icon=None):
            super().__init__(parent, width=width, height=43, bd=0, highlightthickness=0,
                             bg=parent.cget("bg"), cursor="hand2", takefocus=True)
            self.text, self.command = text, command
            self.primary, self.quiet, self.hover, self.enabled = primary, quiet, False, True
            self.icon = icon
            self.bind("<Configure>", lambda e: self.draw())
            self.bind("<Enter>", lambda e: self.enter(True))
            self.bind("<Leave>", lambda e: self.enter(False))
            self.bind("<Button-1>", lambda e: self.invoke())
            self.bind("<Return>", lambda e: self.invoke())
            self.bind("<space>", lambda e: self.invoke())
            self.bind("<FocusIn>", lambda e: self.draw())
            self.bind("<FocusOut>", lambda e: self.draw())

        def enter(self, value):
            self.hover = value
            self.draw()

        def draw(self):
            self.delete("all")
            fill = (colors["blue_hover"] if self.hover else colors["blue"]) if self.primary else ("#F5F6F7" if self.hover else "white")
            fg = "white" if self.primary else colors["ink"]
            if self.quiet:
                fill, fg = (colors["blue_soft"] if self.hover else "white"), colors["muted"]
            if not self.enabled:
                fill, fg = "#F3F4F5", "#A0A5AB"
            border = colors["blue"] if self.focus_get() == self else (fill if self.primary or self.quiet else colors["line"])
            rounded(self, 1, 1, max(2,self.winfo_width()-1), 42, 9, fill=fill, outline=border)
            f=(font,10,"bold" if self.primary else "normal")
            if self.icon:
                text_width=Font(root,font=f).measure(self.text)
                start=(self.winfo_width()-text_width-24)/2
                symbol(self,self.icon,start+8,21,fg)
                self.create_text(start+24,21,text=self.text,fill=fg,font=f,anchor="w")
            else:
                self.create_text(self.winfo_width()/2,21,text=self.text,fill=fg,font=f)

        def invoke(self):
            if self.enabled:
                self.command()

        def set_enabled(self, value):
            self.enabled = value
            self.configure(cursor="hand2" if value else "arrow")
            self.draw()

    class Badge(tk.Canvas):
        def __init__(self,parent):
            super().__init__(parent,width=181,height=36,bg=parent.cget("bg"),highlightthickness=0)
            self.bind("<Configure>",lambda e:self.show(*self.value))
            self.value=("正在读取状态",colors["blue_soft"],colors["blue"])
        def show(self,text,bg,fg):
            self.value=(text,bg,fg)
            self.delete("all")
            rounded(self,0,0,180,35,17,fill=bg,outline="")
            self.create_oval(17,15,23,21,fill=fg,outline="")
            self.create_text(34,18,text=text,fill=fg,anchor="w",font=(font,10))

    class Switch(tk.Canvas):
        def __init__(self,parent,command):
            super().__init__(parent,width=48,height=29,bg=parent.cget("bg"),highlightthickness=0,
                             cursor="hand2",takefocus=True)
            self.on=False
            self.command=command
            self.bind("<Button-1>",lambda e:self.command())
            self.bind("<space>",lambda e:self.command())
            self.bind("<Return>",lambda e:self.command())
            self.bind("<FocusIn>",lambda e:self.show(self.on))
            self.bind("<FocusOut>",lambda e:self.show(self.on))
        def show(self,on):
            self.on=on
            self.delete("all")
            border=colors["blue"] if self.focus_get()==self else ""
            rounded(self,1,2,47,27,13,fill=colors["green"] if on else "#CDD1D5",outline=border)
            x=33 if on else 15
            self.create_oval(x-9,5,x+9,23,fill="white",outline="")

    def label(parent, text=None, size=10, fg=None, bold=False, **kwargs):
        return tk.Label(parent, text=text, bg=parent.cget("bg"), fg=fg or colors["ink"],
                        font=(font, size, "bold" if bold else "normal"), bd=0, **kwargs)

    outer = tk.Frame(root, bg=colors["bg"], padx=32, pady=23)
    outer.pack(fill="both", expand=True)
    header = tk.Frame(outer, bg="white",padx=0,pady=8)
    header.pack(fill="x", pady=(0, 15))
    seal_width = min((144,180,216,288,360,432), key=lambda size:abs(size-144*scale))
    seal_file = asset_dir / f"hust-seal-{seal_width}.png"
    icon = tk.Canvas(header, width=seal_width+20, height=round(seal_width*0.75)+20,
                     bg="white", highlightthickness=0)
    icon.pack(side="left", padx=(0,23))
    if seal_file.exists():
        root.header_icon=tk.PhotoImage(file=str(seal_file))
        icon.configure(height=root.header_icon.height()+20)
        icon.create_image(10,10,image=root.header_icon,anchor="nw")
    else:
        symbol(icon,"network",25,27,colors["ink"],28)
    heading = tk.Frame(header, bg="white")
    heading.pack(side="left")
    label(heading,"HUST CONNECT",size=9,fg=colors["muted"]).pack(anchor="w",pady=(0,7))
    label(heading, "华中科技大学校园网自动登录", size=20,bold=True).pack(anchor="w")
    label(heading, "一键连接校园网，让远程访问保持在线",size=10,fg=colors["muted"]).pack(anchor="w",pady=(10,0))
    pill = Badge(header)
    pill.pack(side="right", anchor="center")
    tk.Frame(outer,bg=colors["line"],height=1).pack(fill="x",pady=(0,24))

    body = tk.Frame(outer, bg=colors["bg"])
    body.pack(fill="both", expand=True)
    body.columnconfigure(0, weight=5, minsize=495)
    body.columnconfigure(1, weight=3, minsize=325)
    body.rowconfigure(0, weight=1)
    left_card = Card(body)
    left_card.grid(row=0,column=0,sticky="nsew",padx=(0,22))
    right_card = Card(body)
    right_card.grid(row=0,column=1,sticky="nsew")
    left, right = left_card.inner, right_card.inner
    label(left,"账号与连接",size=15,bold=True).pack(anchor="w")
    label(left,"填写一次账号密码，即可一键登录与自动重连。",fg=colors["muted"]).pack(anchor="w",pady=(6,17))

    conf = read_json(CONFIG, DEFAULTS)
    username, password, service = tk.StringVar(), tk.StringVar(), tk.StringVar(value=conf.get("service", ""))
    adapter_var = tk.StringVar()
    chosen = []
    events = queue.Queue()
    busy = [False]
    buttons = []

    def field_heading(title,kind):
        row=tk.Frame(left,bg="white")
        row.pack(fill="x",pady=(0,7))
        glyph=tk.Canvas(row,width=21,height=20,bg="white",highlightthickness=0)
        glyph.pack(side="left",padx=(0,5))
        symbol(glyph,kind,10,10,colors["muted"],16)
        label(row,title,size=10).pack(side="left")

    def field(title, var, masked=False, field_icon="user"):
        field_heading(title,field_icon)
        border = tk.Frame(left,bg=colors["field"],highlightthickness=1,
                          highlightbackground=colors["line"],height=41)
        border.pack(fill="x",pady=(0,15))
        border.pack_propagate(False)
        entry = tk.Entry(border,textvariable=var,show="●" if masked else "",relief="flat",bd=0,
                         bg=colors["field"],fg=colors["ink"],insertbackground=colors["blue"],
                         selectbackground=colors["blue_soft"],selectforeground=colors["ink"],font=(font,11))
        entry.pack(side="left",fill="both",expand=True,padx=(12,8),pady=8)
        entry.bind("<FocusIn>",lambda e:border.configure(highlightbackground=colors["blue"]))
        entry.bind("<FocusOut>",lambda e:border.configure(highlightbackground=colors["line"]))
        if masked:
            shown = [False]
            toggle = tk.Label(border,text="显示",font=(font,9),fg=colors["blue"],bg=colors["field"],
                              cursor="hand2",padx=12,takefocus=True)
            toggle.pack(side="right",fill="y")
            def reveal(event=None):
                shown[0] = not shown[0]
                entry.configure(show="" if shown[0] else "●")
                toggle.configure(text="隐藏" if shown[0] else "显示")
            toggle.bind("<Button-1>",reveal)
            toggle.bind("<Return>",reveal)
        return entry

    field("校园网账号",username)
    password_entry = field("校园网密码",password,True,"lock")
    field_heading("校园网网卡","network")
    combo = ttk.Combobox(left,textvariable=adapter_var,state="readonly",style="Modern.TCombobox",font=(font,10))
    combo.pack(fill="x",pady=(0,15))
    field("服务名称  ·  可选",service,field_icon="globe")
    label(left,"通常留空；需要选运营商时，填写网页对应名称。",size=9,fg=colors["muted"]).pack(anchor="w")
    actions = tk.Frame(left,bg="white")
    actions.pack(side="bottom",fill="x",pady=(16,0))

    # Reserve controls first so they remain visible at the minimum window size.
    links=tk.Frame(right,bg="white")
    links.pack(side="bottom",fill="x",pady=(12,0))
    status_top = tk.Frame(right,bg="white")
    status_top.pack(fill="x")
    label(status_top,"连接状态",size=13,bold=True).pack(side="left")
    status_dot = label(status_top,"●",size=13,fg=colors["muted"])
    status_dot.pack(side="right")
    status_icon = tk.Canvas(right,width=68,height=70,bg="white",highlightthickness=0)
    status_icon.pack(anchor="w",pady=(18,8))
    connection = label(right,"正在检测",size=21,bold=True)
    connection.pack(anchor="w")
    detail_var = tk.StringVar(value="正在读取网卡与校园网状态…")
    detail = label(right,size=10,fg=colors["muted"],textvariable=detail_var,justify="left",anchor="nw",wraplength=260,height=2)
    detail.pack(fill="x",pady=(10,14))
    tk.Frame(right,bg=colors["line"],height=1).pack(fill="x",pady=(0,15))
    values = {}
    for title, key, value in (("检测频率","interval","每 1 分钟"),("最近检测","last","—"),("当前网卡","adapter","读取中")):
        row=tk.Frame(right,bg="white")
        row.pack(fill="x",pady=6)
        label(row,title,fg=colors["muted"]).pack(side="left")
        values[key]=label(row,value)
        values[key].pack(side="right")
    tip=tk.Frame(right,bg=colors["blue_soft"],padx=14,pady=12)
    tip.pack(fill="x",pady=(17,0))
    auto_row=tk.Frame(tip,bg=colors["blue_soft"])
    auto_row.pack(fill="x")
    label(auto_row,"每分钟自动检测",size=11,fg=colors["blue"],bold=True).pack(side="left")
    auto_switch=Switch(auto_row,lambda:toggle_auto())
    auto_switch.pack(side="right")
    label(tip,"掉线后自动登录，锁屏和远程断开后\n仍在后台运行。",size=9,fg=colors["muted"],justify="left").pack(anchor="w",pady=(7,0))

    footer=tk.Frame(outer,bg=colors["bg"])
    footer.pack(fill="x",pady=(18,0))
    lock=tk.Canvas(footer,width=19,height=22,bg=colors["bg"],highlightthickness=0)
    lock.pack(side="left",padx=(0,7))
    lock.create_arc(5,2,14,13,start=0,extent=180,outline=colors["muted"],width=1.4,style="arc")
    rounded(lock,3,9,16,19,3,fill="",outline=colors["muted"],width=1.2)
    label(footer,"凭据仅保存在本机，使用 Windows 加密保护",size=9,fg=colors["muted"]).pack(side="left")
    label(footer,f"HUST CONNECT  /  {VERSION}",size=8,fg="#92989E").pack(side="right")
    credits=tk.Frame(outer,bg=colors["bg"])
    credits.pack(fill="x",pady=(7,0))
    label(credits,f"贡献者  {CONTRIBUTOR}",size=9,fg=colors["blue"],bold=True).pack(side="left")
    email_label=label(credits,CONTACT_EMAIL,size=9,fg=colors["muted"],cursor="hand2",takefocus=True)
    email_label.pack(side="left",padx=(16,0))
    def copy_email(event=None):
        root.clipboard_clear()
        root.clipboard_append(CONTACT_EMAIL)
        email_label.configure(text="邮箱已复制")
        root.after(1800,lambda:email_label.configure(text=CONTACT_EMAIL))
    email_label.bind("<Button-1>",copy_email)
    email_label.bind("<Return>",copy_email)
    label(credits,"个人项目 · 非学校官方软件",size=8,fg="#92989E").pack(side="right")

    def draw_status(online, caution=False):
        status_icon.delete("all")
        color = colors["green"] if online else colors["amber"] if caution else colors["blue"]
        tint = colors["green_soft"] if online else colors["amber_soft"] if caution else colors["blue_soft"]
        status_icon.create_oval(0,0,65,65,fill=tint,outline="")
        if online:
            status_icon.create_oval(18,18,47,47,outline=color,width=2)
            status_icon.create_line(25,33,31,39,41,27,fill=color,width=2.5,capstyle="round",joinstyle="round")
        elif caution:
            status_icon.create_line(33,20,33,36,fill=color,width=3,capstyle="round")
            status_icon.create_oval(31,42,35,46,fill=color,outline="")
        else:
            status_icon.create_arc(17,17,49,49,start=35,extent=285,outline=color,width=2.5,style="arc")
            status_icon.create_line(41,15,45,22,37,23,fill=color,width=2.5,joinstyle="round")
        status_dot.configure(fg=color)

    def render_state(state=None):
        c=read_json(CONFIG,DEFAULTS)
        state=state if state is not None else read_json(STATE,{})
        enabled=c.get("enabled",False)
        pill.show("每分钟检测中" if enabled else "自动检测已暂停",
                  colors["green_soft"] if enabled else colors["blue_soft"],
                  colors["green"] if enabled else colors["muted"])
        auto_switch.show(enabled)
        values["interval"].configure(text="每 1 分钟" if enabled else "已暂停")
        code=state.get("code","")
        online=code in {"ONLINE","RECONNECTED"}
        caution=code in {"NEEDS_ATTENTION","LOGIN_FAILED","CAPTCHA","ERROR"}
        titles={"ONLINE":"校园网在线","RECONNECTED":"连接已恢复","PAUSED":"自动登录已暂停",
                "LOGIN_REQUIRED":"需要重新登录","LOGIN_ATTEMPT":"正在重新登录","VERIFY_PENDING":"正在确认连接",
                "NEEDS_ATTENTION":"需要处理","CAPTCHA":"需要网页验证","LOGIN_FAILED":"登录未成功",
                "BACKOFF":"等待重新尝试","WAIT_NETWORK":"等待校园网","WAITING":"等待连接","ERROR":"检测遇到问题"}
        connection.configure(text=titles.get(code,"准备就绪" if chosen else "正在读取"))
        detail_var.set(state.get("message") or ("填写账号和密码，保存后启用自动登录。" if not enabled else "后台会每分钟检查一次校园网认证。"))
        draw_status(online,caution)
        stamp=state.get("checked_at","")
        values["last"].configure(text=stamp[11:19] if len(stamp)>19 else "—")
        if combo.current()>=0:
            values["adapter"].configure(text=chosen[combo.current()]["name"])

    def refresh():
        try:
            if not busy[0]:
                render_state()
        except (OSError,ValueError):
            pass
        root.after(3000,refresh)

    def background(fn,callback):
        if busy[0]:
            return
        busy[0]=True
        for b in buttons:
            b.set_enabled(False)
        def worker():
            try:
                value=fn()
            except SafeError as e:
                value={"error":str(e)}
            except Exception:
                value={"error":"操作未完成，请检查网卡和设置后重试。"}
            events.put((callback,value))
        threading.Thread(target=worker,daemon=True).start()

    def poll():
        try:
            while True:
                callback,value=events.get_nowait()
                busy[0]=False
                for b in buttons:
                    b.set_enabled(True)
                if isinstance(value,dict) and "error" in value:
                    detail_var.set(value["error"])
                    connection.configure(text="暂时无法检测")
                    draw_status(False,True)
                else:
                    callback(value)
        except queue.Empty:
            pass
        root.after(120,poll)

    def check():
        if busy[0]:
            return
        detail_var.set("正在连接校园网认证服务器…")
        connection.configure(text="正在检测连接")
        draw_status(False)
        background(lambda:run_cycle(diagnostic=True),render_state)

    def save(login_after=False):
        if busy[0]:
            return
        u,p=username.get().strip(),password.get()
        if not u or not p or combo.current()<0:
            messagebox.showinfo("请补全设置","请填写校园网账号、密码，并选择校园网网卡。",parent=root)
            return
        try:
            with single_instance() as acquired:
                if not acquired:
                    raise SafeError("后台正在检测，请稍后再次保存。")
                existing=read_json(CONFIG,DEFAULTS)
                # The manual action is independent of the periodic monitoring switch.
                enabled=existing.get("enabled",False) if existing.get("secret") else True
                c=dict(DEFAULTS,enabled=enabled,interface_guid=chosen[combo.current()]["guid"],
                       service=service.get().strip(),secret=crypt(json.dumps({"username":u,"password":p}).encode()).hex())
                write_json(CONFIG,c)
                # Saving corrected settings clears earlier authentication backoff.
                write_json(STATE,{})
            if login_after:
                detail_var.set("正在检查认证；需要登录时立即提交。")
                connection.configure(text="正在一键登录")
                draw_status(False)
                background(lambda:run_cycle(manual=True),render_state)
            else:
                render_state()
                detail_var.set("账号和网络设置已加密保存。")
        except SafeError as e:
            messagebox.showerror("保存失败",str(e),parent=root)

    def toggle_auto():
        if busy[0]:
            return
        try:
            with single_instance() as acquired:
                if not acquired:
                    raise SafeError("后台正在检测，请稍后再次暂停。")
                c=read_json(CONFIG,DEFAULTS)
                if not c.get("secret"):
                    raise SafeError("请填写账号密码后点击“一键登录”，再开启每分钟检测。")
                c["enabled"]=not c.get("enabled",False)
                write_json(CONFIG,c)
            render_state()
        except SafeError as e:
            messagebox.showinfo("请稍后",str(e),parent=root)

    login_btn=Button(actions,"一键登录",lambda:save(login_after=True),width=194,primary=True,icon="play")
    login_btn.pack(side="left",padx=(0,12))
    save_btn=Button(actions,"保存设置",save,width=130,icon="check")
    save_btn.pack(side="left")
    check_btn=Button(links,"仅检测连接",check,width=137,quiet=True,icon="refresh")
    check_btn.pack(side="left")
    log_btn=Button(links,"查看日志",lambda:os.startfile(str(DATA)),width=109,quiet=True,icon="log")
    log_btn.pack(side="right")
    buttons.extend([login_btn,save_btn,check_btn])
    combo.bind("<<ComboboxSelected>>",lambda e:values["adapter"].configure(text=chosen[combo.current()]["name"]))

    def initial():
        result={"adapters":adapters()}
        try:
            if conf.get("secret"):
                result["secret"]=json.loads(crypt(bytes.fromhex(conf["secret"]),True))
            else:
                a=next(a for a in result["adapters"] if a["ips"] and a["up"] and
                       (not conf["interface_guid"] or a["guid"].lower()==conf["interface_guid"].lower()))
                ok,info=Portal(a).online()
                if ok:
                    result["username"]=info.get("userId","")
        except Exception:
            pass
        return result

    def loaded(result):
        chosen.extend(result["adapters"])
        combo["values"]=[a["name"]+("  ·  已连接" if a["up"] else "  ·  未连接") for a in chosen]
        for i,a in enumerate(chosen):
            if a["guid"].lower()==conf["interface_guid"].lower():
                combo.current(i)
                break
        if not conf["interface_guid"]:
            for i,a in enumerate(chosen):
                if a["up"] and a["ips"]:
                    combo.current(i)
                    break
        if "secret" in result:
            username.set(result["secret"]["username"])
            password.set(result["secret"]["password"])
        elif result.get("username"):
            username.set(result["username"])
        render_state()

    DATA.mkdir(parents=True,exist_ok=True)
    draw_status(False)
    background(initial,loaded)
    poll()
    refresh()
    root.mainloop()


def main():
    global DATA, CONFIG, STATE
    parser = argparse.ArgumentParser()
    parser.add_argument("--gui", action="store_true")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--diagnose", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--data-dir")
    parser.add_argument("--migrate-credentials", action="store_true")
    args = parser.parse_args()
    if args.data_dir:
        DATA = Path(args.data_dir).resolve()
        CONFIG, STATE = DATA / "settings.json", DATA / "status.json"
    DATA.mkdir(parents=True, exist_ok=True)
    if args.migrate_credentials:
        with single_instance() as acquired:
            if not acquired:
                raise SafeError("后台正在检测，请稍后重试迁移。")
            config = read_json(CONFIG, DEFAULTS)
            if config.get("secret") and config.get("secret_scope") != "machine":
                plaintext = crypt(bytes.fromhex(config["secret"]), decrypt=True)
                config.update(version=2, secret=crypt(plaintext).hex(), secret_scope="machine")
                del plaintext
                write_json(CONFIG, config)
        result = {"configured": bool(config.get("secret")), "scope": config.get("secret_scope")}
    elif args.status:
        result = read_json(STATE, {"message": "尚未检测。"})
    elif args.once or args.diagnose:
        result = run_cycle(diagnostic=args.diagnose)
    else:
        gui()
        return
    if sys.stdout:
        print(json.dumps(result, ensure_ascii=True))


if __name__ == "__main__":
    main()
