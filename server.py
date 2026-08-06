#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
学员 AI 系统网站 - 后端（零依赖，标准库实现）
- 服务静态 index.html
- /api/register、/api/login：账号注册登录，签发会话 token
- /api/diagnose /position /content /templates /monetize：五大 AI 接口（Bearer 鉴权 + 每日配额限流）
- /api/schemes：学员方案存档（服务端 SQLite，按用户隔离）
- AI_API_KEY / AI_BASE_URL / AI_MODEL 仅在后端；未配置时返回内置示例(mock)
安全：API Key 绝不出现在任何响应里；密码用 pbkdf2 加盐哈希；会话 token 随机。
"""
import json
import os
import sqlite3
import hashlib
import secrets
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from datetime import datetime, timedelta

PORT = int(os.environ.get("PORT", "8000"))
API_KEY = os.environ.get("AI_API_KEY", "")
BASE_URL = os.environ.get("AI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
MODEL = os.environ.get("AI_MODEL", "gpt-4o-mini")
DB = os.environ.get("DB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "ai_member.db"))
DEFAULT_QUOTA = int(os.environ.get("DAILY_QUOTA", "30"))      # 每位学员每日调用上限
SESSION_DAYS = int(os.environ.get("SESSION_DAYS", "7"))

PROMPTS = {
    "diagnose": (
        "你是一个帮助普通人的自媒体赛道诊断顾问。"
        "根据学员的职业、兴趣、每日可投入时间、已有资源，推荐 2-3 个最适合起步的赛道。"
        "优先推荐能建立信任、可持续输出的赛道，而不是单纯流量大的赛道。"
        "每个推荐给出：赛道名、匹配理由、起步难度(低/中/高)、变现潜力(低/中/高)。用中文结构化输出。"
    ),
    "position": (
        "你是自媒体定位专家。基于学员的赛道、个人经历、优势，以及看似'缺点'的特质，"
        "生成 2-3 个细分配位方案，把平凡甚至缺点转化为特色。"
        "每个方案包含：人设标签、一句话账号简介、3 个内容方向、为什么这个定位成立。用中文。"
    ),
    "content": (
        "你是自媒体内容创作助手。基于学员的赛道与定位，生成一篇可直接使用的内容"
        "(短视频口播脚本或图文)。包含：标题、开篇钩子、正文结构(3-5 点)、结尾行动召唤。"
        "风格口语、真实、不鸡汤、不成功学。用中文。"
    ),
    "templates": (
        "你是自媒体爆款模板专家。学员给出赛道，你推荐 3-5 个该赛道已验证的爆款内容模板。"
        "每个模板包含：模板名、结构(开头→中间→结尾怎么铺)、一个可套用的选题示例。"
        "模板要具体可操作，不要空泛。用中文。"
    ),
    "monetize": (
        "你是自媒体商业化顾问。根据学员的账号阶段（如起号期/成长期/稳定期）和赛道，"
        "给出适合的变现路径。每个路径包含：变现方式、具体做法、预期收益区间、适合开始的时机。"
        "并给一句整体节奏建议（先做什么后做什么）。用中文。"
    ),
}

MOCK = {
    "diagnose": {
        "mode": "mock",
        "recommend": [
            {"track": "省钱育儿（母婴细分）", "reason": "你是有娃的普通家庭妈妈，真实的花钱踩坑与省钱经验对同阶段妈妈有强信任感，且母婴变现路径成熟。", "difficulty": "低", "cash": "中高"},
            {"track": "普通家庭的一日生活", "reason": "把'普通'当成特色，记录真实家庭生活，容易引发同类人群共鸣，起步几乎零成本。", "difficulty": "低", "cash": "中"},
            {"track": "妈妈视角的消费决策", "reason": "结合你的采购经验做'避坑测评'，信任资产可迁移到带货，但需一定选品积累。", "difficulty": "中", "cash": "高"},
        ],
    },
    "position": {
        "mode": "mock",
        "options": [
            {"tag": "精打细算的二胎妈妈", "bio": "普通家庭月入有限，但把日子过得有滋有味——分享真实省钱育儿经。", "directions": ["月薪Xk怎么给孩子攒下第一桶金", "那些'智商税'母婴用品我替你踩过了", "一周家庭餐谱：好吃不贵"], "why": "把'钱不多'这个看似劣势变成'最懂普通人'的优势，同类妈妈天然信任。"},
            {"tag": "不完美的真实妈妈", "bio": "不装精致、不晒逆袭，记录一个普通妈妈怎么笨拙但认真地养娃。", "directions": ["今天我又搞砸了的一件小事", "娃生病那晚我做的事", "和老公分工带娃的真相"], "why": "'真实缺点'降低距离感，评论区会变成同温层，黏性极高。"},
        ],
    },
    "content": {
        "mode": "mock",
        "title": "月薪8k，我是怎么给娃存下第一笔钱的",
        "hook": "很多人以为省钱就是抠，其实普通人攒钱靠的是'顺序'——今天说三个我踩了两年才摸清的顺序。",
        "body": [
            "第一，先记'必经开销'不是记流水。奶粉尿布是逃不掉的，先锁定它，剩下的才是能动的。",
            "第二，大件用'等一周'原则。看中的推车、餐椅，放购物车晾七天，80%你就不想买了。",
            "第三，把省下的钱单独开个账户。哪怕每月300，视觉上的'在涨'比数字本身更有动力。",
        ],
        "cta": "你家的第一笔钱是怎么攒的？评论区聊聊，我整理了份'母婴避坑清单'发给你。",
    },
    "templates": {
        "mode": "mock",
        "track": "省钱育儿",
        "templates": [
            {"name": "避坑清单体", "structure": "开头晒一个真实踩坑→列 3-5 个同类坑→给替代方案→结尾送清单", "example": "我花3000块踩的5个母婴智商税"},
            {"name": "一周记录体", "structure": "连续7天记录某件事→展示数据/变化→复盘可复用的方法", "example": "普通妈妈的一周餐谱花销实录"},
            {"name": "对比测评体", "structure": "A vs B 实测→给出结论→说明适用人群", "example": "两款婴儿车我替你扛了30天"},
        ],
    },
    "monetize": {
        "mode": "mock",
        "stage": "起号期（0-1000 粉）",
        "paths": [
            {"type": "引流到私域", "desc": "用免费清单/资料包把粉丝导到微信，攒信任、攒名单", "cash": "低→中", "when": "立刻可做"},
            {"type": "带货（佣品类）", "desc": "选母婴刚需品做测评带货，无需囤货", "cash": "中", "when": "有1000粉后"},
            {"type": "低价知识产品", "desc": "把省钱方法论做成低价训练营/电子手册", "cash": "中高", "when": "有稳定内容后"},
        ],
        "note": "前3个月只做信任和私域积累，别急着卖课。",
    },
}

AI_HANDLERS = {
    "diagnose": lambda r: f"职业：{r.get('job','')}\n兴趣：{r.get('interest','')}\n每日可投入：{r.get('time','')}\n已有资源：{r.get('resource','')}",
    "position": lambda r: f"赛道：{r.get('track','')}\n个人经历：{r.get('experience','')}\n优势：{r.get('strength','')}\n看似缺点的特质：{r.get('flaw','')}",
    "content": lambda r: f"定位：{r.get('position','')}\n想做的选题方向：{r.get('topic','')}\n形式：{r.get('format','短视频口播脚本')}",
    "templates": lambda r: f"赛道：{r.get('track','')}",
    "monetize": lambda r: f"账号阶段：{r.get('stage','')}\n赛道：{r.get('track','')}",
}


def db_conn():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    c = db_conn()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS users(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      username TEXT UNIQUE NOT NULL,
      pw_hash TEXT NOT NULL,
      pw_salt TEXT NOT NULL,
      role TEXT NOT NULL DEFAULT 'member',
      created_at TEXT NOT NULL,
      daily_quota INTEGER NOT NULL DEFAULT 30,
      quota_date TEXT,
      daily_count INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS sessions(
      token TEXT PRIMARY KEY,
      user_id INTEGER NOT NULL,
      created_at TEXT NOT NULL,
      expires_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS schemes(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      user_id INTEGER NOT NULL,
      kind TEXT NOT NULL,
      title TEXT,
      content TEXT,
      created_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_schemes_user ON schemes(user_id);
    """)
    c.commit()
    c.close()


def hash_pw(password, salt=None):
    if salt is None:
        salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), 100000).hex()
    return h, salt


def auth_user(handler):
    auth = handler.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    token = auth[7:].strip()
    if not token:
        return None
    c = db_conn()
    row = c.execute("SELECT * FROM sessions WHERE token=?", (token,)).fetchone()
    if not row:
        c.close()
        return None
    if datetime.fromisoformat(row["expires_at"]) < datetime.now():
        c.execute("DELETE FROM sessions WHERE token=?", (token,))
        c.commit()
        c.close()
        return None
    u = c.execute("SELECT * FROM users WHERE id=?", (row["user_id"],)).fetchone()
    c.close()
    return dict(u) if u else None


def issue_token(uid, username):
    token = secrets.token_hex(32)
    now = datetime.now()
    exp = now + timedelta(days=SESSION_DAYS)
    c = db_conn()
    c.execute("INSERT INTO sessions(token,user_id,created_at,expires_at) VALUES(?,?,?,?)",
              (token, uid, now.isoformat(), exp.isoformat()))
    c.commit()
    c.close()
    return 200, {"token": token, "user": {"username": username, "role": "member"}}


def check_and_inc_quota(user_id):
    """校验并扣减当日配额。返回 (allow, user_dict_after)。"""
    today = datetime.now().strftime("%Y-%m-%d")
    c = db_conn()
    u = c.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    d = dict(u)
    if d["quota_date"] != today:
        c.execute("UPDATE users SET quota_date=?, daily_count=0 WHERE id=?", (today, user_id))
        d["quota_date"] = today
        d["daily_count"] = 0
    if d["daily_count"] >= d["daily_quota"]:
        c.close()
        return False, d
    c.execute("UPDATE users SET daily_count=daily_count+1 WHERE id=?", (user_id,))
    c.commit()
    c.close()
    d["daily_count"] += 1
    return True, d


def call_llm(system, user):
    if not API_KEY:
        return None
    payload = json.dumps({
        "model": MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.7,
    }).encode("utf-8")
    req = urllib.request.Request(
        BASE_URL + "/chat/completions",
        data=payload,
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + API_KEY},
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            data = json.loads(r.read())
        return data["choices"][0]["message"]["content"]
    except Exception as e:
        return "[AI调用失败] " + str(e)


def today_used(user):
    today = datetime.now().strftime("%Y-%m-%d")
    return 0 if user["quota_date"] != today else user["daily_count"]


class Handler(BaseHTTPRequestHandler):
    def _send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw)
        except Exception:
            return {}

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()

    def do_GET(self):
        if self.path == "/api/me":
            self._api_me()
        elif self.path == "/api/schemes":
            self._api_list_schemes()
        elif self.path.startswith("/api/"):
            self._send_json({"error": "method not allowed"}, 405)
        else:
            self._serve_static()

    def do_POST(self):
        if not self.path.startswith("/api/"):
            self._send_json({"error": "not found"}, 404)
            return
        endpoint = self.path[len("/api/"):]
        req = self._read_json()
        if endpoint == "register":
            return self._api_register(req)
        if endpoint == "login":
            return self._api_login(req)
        if endpoint == "schemes":
            return self._api_save_scheme(req)
        if endpoint in AI_HANDLERS:
            return self._api_ai(endpoint, req)
        self._send_json({"error": "unknown endpoint"}, 404)

    def do_DELETE(self):
        if self.path.startswith("/api/schemes/"):
            sid = self.path[len("/api/schemes/"):]
            return self._api_del_scheme(sid)
        self._send_json({"error": "not found"}, 404)

    # ---------- 静态页 ----------
    def _serve_static(self):
        try:
            with open("index.html", "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except Exception:
            self.send_response(404)
            self.end_headers()

    # ---------- 账号 ----------
    def _api_register(self, req):
        username = (req.get("username") or "").strip()
        password = req.get("password") or ""
        if not username or not password:
            return self._send_json({"error": "用户名和密码必填"}, 400)
        if len(password) < 6:
            return self._send_json({"error": "密码至少 6 位"}, 400)
        c = db_conn()
        if c.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone():
            c.close()
            return self._send_json({"error": "用户名已存在"}, 409)
        h, salt = hash_pw(password)
        now = datetime.now()
        cur = c.execute(
            "INSERT INTO users(username,pw_hash,pw_salt,role,created_at,daily_quota,quota_date,daily_count) "
            "VALUES(?,?,?,'member',?,?,?,0)",
            (username, h, salt, now.isoformat(), DEFAULT_QUOTA, now.strftime("%Y-%m-%d")))
        uid = cur.lastrowid
        c.commit()
        c.close()
        code, body = issue_token(uid, username)
        self._send_json(body, code)

    def _api_login(self, req):
        username = (req.get("username") or "").strip()
        password = req.get("password") or ""
        c = db_conn()
        u = c.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        c.close()
        if not u or not hash_pw(password, u["pw_salt"])[0] == u["pw_hash"]:
            return self._send_json({"error": "用户名或密码错误"}, 401)
        code, body = issue_token(u["id"], username)
        self._send_json(body, code)

    def _api_me(self, user=None):
        user = user or auth_user(self)
        if not user:
            return self._send_json({"error": "未登录"}, 401)
        used = today_used(user)
        remaining = user["daily_quota"] - used
        self._send_json({
            "user": {"username": user["username"], "role": user["role"]},
            "quota": user["daily_quota"], "used": used, "remaining": remaining,
        })

    # ---------- AI 接口（鉴权 + 限流） ----------
    def _api_ai(self, endpoint, req):
        user = auth_user(self)
        if not user:
            return self._send_json({"error": "请先登录", "needLogin": True}, 401)
        allow, d = check_and_inc_quota(user["id"])
        if not allow:
            return self._send_json({
                "error": "今日调用额度已用尽，明天重置",
                "used": d["daily_count"], "quota": d["daily_quota"], "quotaExceeded": True,
            }, 429)
        ai = call_llm(PROMPTS[endpoint], AI_HANDLERS[endpoint](req))
        meta = {"used": d["daily_count"], "quota": d["daily_quota"]}
        if ai:
            self._send_json({"mode": "live", "text": ai, **meta})
        else:
            self._send_json({**MOCK[endpoint], **meta})

    # ---------- 方案存档 ----------
    def _api_list_schemes(self):
        user = auth_user(self)
        if not user:
            return self._send_json({"error": "未登录"}, 401)
        c = db_conn()
        rows = c.execute(
            "SELECT id,kind,title,content,created_at FROM schemes WHERE user_id=? ORDER BY id DESC",
            (user["id"],)).fetchall()
        c.close()
        items = [dict(r) for r in rows]
        self._send_json({"items": items})

    def _api_save_scheme(self, req):
        user = auth_user(self)
        if not user:
            return self._send_json({"error": "未登录"}, 401)
        kind = (req.get("kind") or "").strip()
        title = (req.get("title") or "").strip()
        text = req.get("text") or ""
        if not kind or not text:
            return self._send_json({"error": "缺少内容"}, 400)
        c = db_conn()
        cur = c.execute(
            "INSERT INTO schemes(user_id,kind,title,content,created_at) VALUES(?,?,?,?,?)",
            (user["id"], kind, title, text, datetime.now().isoformat()))
        sid = cur.lastrowid
        c.commit()
        c.close()
        self._send_json({"ok": True, "id": sid})

    def _api_del_scheme(self, sid):
        user = auth_user(self)
        if not user:
            return self._send_json({"error": "未登录"}, 401)
        c = db_conn()
        c.execute("DELETE FROM schemes WHERE id=? AND user_id=?", (sid, user["id"]))
        c.commit()
        c.close()
        self._send_json({"ok": True})

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    init_db()
    print(f"学员 AI 系统运行中：http://localhost:{PORT}  (AI_API_KEY={'已配置' if API_KEY else '未配置→示例数据'}; 账号系统=开)")
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
