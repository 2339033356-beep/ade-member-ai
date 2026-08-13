#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
学员 AI 系统网站 - 后端（零依赖，标准库实现；可选 Postgres 持久化）
- 服务静态 index.html
- /api/register、/api/login：账号注册登录，签发会话 token
- /api/diagnose /position /content /templates /monetize：五大 AI 接口（Bearer 鉴权 + 每日配额限流）
- /api/schemes：学员方案存档（按用户隔离）
- ai_member.db（SQLite）/ DATABASE_URL（Postgres）二选一：
    设了 DATABASE_URL 就走 Postgres（数据持久化，重部署不丢）；没设就退回本地 SQLite。
- AI_API_KEY / AI_BASE_URL / AI_MODEL 仅在后端；未配置时返回内置示例(mock)
安全：API Key 绝不出现在任何响应里；密码用 pbkdf2 加盐哈希；会话 token 随机。
"""
import json
import os
import hashlib
import secrets
import urllib.request
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from datetime import datetime, timedelta

PORT = int(os.environ.get("PORT", "8000"))
API_KEY = os.environ.get("AI_API_KEY", "")
BASE_URL = os.environ.get("AI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
MODEL = os.environ.get("AI_MODEL", "gpt-4o-mini")
DEFAULT_QUOTA = int(os.environ.get("DAILY_QUOTA", "30"))      # 每位学员每日调用上限
SESSION_DAYS = int(os.environ.get("SESSION_DAYS", "7"))
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")               # 管理后台 token（阿德自己设）
ALLOW_REGISTER = os.environ.get("ALLOW_REGISTER", "false").lower() == "true"  # 默认关闭公开注册
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "")          # 启动时自动建管理员账号
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
NEWRANK_KEY = os.environ.get("NEWRANK_KEY", "")   # 新榜 API 密钥（爆款模板库数据源；空则降级示例模板）

# ---------- 数据库：Postgres / SQLite 双模式 ----------
DATABASE_URL = os.environ.get("DATABASE_URL", "")
USE_PG = bool(DATABASE_URL)

if USE_PG:
    import psycopg
    def db_conn():
        # dict_row 让查询结果支持 row["field"] 取值，与 SQLite 行为一致
        return psycopg.connect(DATABASE_URL, row_factory=psycopg.rows.dict_row, connect_timeout=20)
else:
    import sqlite3
    DB = os.environ.get("DB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "ai_member.db"))
    def db_conn():
        conn = sqlite3.connect(DB)
        conn.row_factory = sqlite3.Row
        return conn

def q(sql):
    """把 SQLite 的 ? 占位符换成 Postgres 的 %s；SQLite 模式原样返回。"""
    return sql.replace("?", "%s") if USE_PG else sql

def insert_id(conn, sql, params):
    """插入并返回自增主键 id（两种数据库兼容）。"""
    if USE_PG:
        cur = conn.execute(q(sql) + " RETURNING id", params)
        return cur.fetchone()["id"]
    cur = conn.execute(sql, params)
    return cur.lastrowid

PROMPTS = {
    "diagnose": (
        "你是一个真实做过自媒体、踩过不少坑的博主，现在像朋友一样帮另一个普通人看：他适合从哪个赛道起步。"
        "根据学员的职业、兴趣、每天能投入的时间、手头有的资源，给 2-3 个他最该先试的赛道。"
        "别看他'有没有光环'，就看他真实生活里有没有能分享的东西。"
        "只挑能建立信任、能长期输出的赛道，别光看流量大不大。"
        "对每个推荐说清楚：赛道名、为什么适合他（结合他的真实情况，说人话）、起步难度(低/中/高)、以后能不能变现(低/中/高)。"
    ),
    "position": (
        "你是一个从普通人做起来的自媒体博主，特别擅长把'看着普通的经历'讲成'有特色的人设'。"
        "基于学员的赛道、他真实经历、他擅长的，还有他自己觉得'是缺点'的那些特质，帮他生成 2-3 个细分配位。"
        "关键：把他眼里的'缺点'翻成'别人学不来的特色'。比如嘴笨→真实不做作，普通→最有共鸣。"
        "每个定位给：人设标签、一句账号简介（像他本人会说的）、3 个内容方向、为什么这个定位能立住。"
        "\n\n【输出第一条硬性要求】在正文最开头，必须单独输出一行定位一句话，格式严格如下："
        "定位一句话：我是<身份/人设>，我要帮<目标人群>解决<具体问题>，用<你的方法/打法>。"
        "例如：定位一句话：我是精打细算的二胎妈妈，我要帮普通家庭妈妈解决'钱不多但想给娃最好'的育儿焦虑，用真实省钱踩坑+一周餐谱实测的方法。"
        "这一行必须真实、具体、不空泛，学员会直接拿它去第 4 步拆爆款，所以别写虚话。"
    ),
    "content": (
        "你是一个真实在更文更视频的博主，帮学员写一条他明天就能发的内容（短视频口播脚本或图文）。"
        "基于他的赛道和定位，写出：标题、开篇怎么勾住人、正文 3-5 个点、结尾怎么引导互动。"
        "最重要的是'真'——说具体的事，别讲空道理，要像他本人会发的那样，不是培训机构教的模板腔。"
    ),
    "templates": (
        "你是一个在赛道里混过、知道哪些模板真能跑出来的博主。学员说一个赛道，你给他 3-5 个这个赛道里被验证过的爆款内容模板。"
        "每个模板：模板名（好记的）、结构（开头怎么铺→中间怎么讲→结尾怎么收）、一个他能直接套的选题例子。"
        "要具体，能照着抄，别空谈'人设''定位'。"
    ),
    "monetize": (
        "你是一个账号从 0 做起、慢慢跑通变现的博主，跟学员说人话：他现在这个阶段，该咋赚钱、什么时候开始、别踩哪些坑。"
        "按账号阶段（起号期/成长期/稳定期）和赛道，给合适的变现路径。"
        "每条：变现方式、具体怎么做、大概能赚多少（给个区间，别说死）、啥时候开始最合适。"
        "最后给一句节奏提醒：先干嘛后干嘛。"
    ),
}

FORMAT_TIP = (
    "\n\n【输出格式与语气要求】"
    "语气：像一个有经验的真实博主在跟朋友说话——直接、口语、有真实感。可以用'其实''说实话''我发现''慢慢发现'这类词。"
    "少用'建议您''综上所述''值得注意的是'这类书面腔、顾问腔。切忌成功学、切忌画大饼、切忌'普通人逆袭''月入过万'这种话术。"
    "排版：标题只用行首的 ## 或 ### 表示层级，标题文字里不要再写 # 号；用 - 做要点列表，每条尽量短（1-2 句）；关键句用 **加粗**。"
    "分段透气，别一大坨；信息要实，别凑字数。"
)

# 爆款模板库：提炼结构 + 按模板出稿 专用提示词
REFINE_PROMPT = (
    "你是一个专门拆解爆款内容的操盘手。下面是一条真实爆款文章。"
    "请把它拆成可复用的'结构骨架'——不要照搬它的具体事实，只提炼'为什么它爆'的套路。"
    "严格按以下 5 点输出，每点一两句、点到为止：\n"
    "1. 钩子类型：它开头用什么方式勾住人（反差/痛点/好奇/身份认同…）\n"
    "2. 叙事结构：整体怎么铺（几段、每段干什么）\n"
    "3. 情绪点：在哪几个位置制造了情绪波动\n"
    "4. 金句位：记忆点/金句大概出现在哪\n"
    "5. 结尾引导：怎么引导互动或关注\n"
)
GEN_PROMPT = (
    "你是一个真实在更文更视频的博主，擅长'借爆款结构、写自己的真实内容'。"
    "下面给了一条爆款的'结构骨架'，以及学员自己的定位和他的具体情况。"
    "请先把骨架吃透，再把学员的真实情况填进去，写成一条他明天就能发的{format}。"
    "硬要求：\n"
    "- 结构节奏沿用爆款骨架（钩子→叙事→情绪点→金句→引导），但内容 100% 是学员自己的真实情况，严禁照搬原爆款的事实/人名/数据\n"
    "- 说具体的事，别讲空道理，像他本人会发的那样，不是培训机构教的模板腔\n"
    "- '真'字当头，可以带他的真实情绪和细节\n"
)
GEN_FORMAT_TIP = (
    "\n\n【输出格式与语气要求】"
    "语气：像一个有经验的真实博主在跟朋友说话——直接、口语、有真实感。可以用'其实''说实话''我发现''慢慢发现'。"
    "少用'建议您''综上所述''值得注意的是'这类书面腔、顾问腔。切忌成功学、画大饼、'普通人逆袭''月入过万'话术。"
    "排版：标题只用行首 ## 或 ### 表示层级，标题文字里不要再写 # 号；用 - 做要点；关键句 **加粗**。分段透气，别一大坨。"
)

# 爆款教练（v7 核心模块）：学员手动贴爆款原文 → AI 拆真实结构 → 按结构挖经历 → 出稿
# 思路源自「IP爆款教练」MASTER_PROMPT，去新榜 API 依赖，站内闭环，无需外部数据源
BAOKUAN_STEP1_PROMPT = """你是一位极简高效的「IP爆款内容教练」。学员贴来一条真实爆款原文，并给了自己的定位。请快速把它拆成他马上能用的写作框架。全程简体中文，不要寒暄和鼓励废话，直接出结构。

只输出下面两块内容，总字数控制在 650 字以内：

【标题（框架）】
1. 照抄这条爆款的标题。
2. 一句话点出它为什么能火、暗含的框架逻辑（如"数字清单+身份共鸣""痛点+反转"）。
3. 可改方向：基于学员的定位，给 1 个他可以把标题改成"更贴合自己"的方向（沿用原标题公式，把人群/数字/痛点换成他自己的，不另起炉灶）。

【逐段最简切入】
按文章的小标题（没有就按自然段临时起一个小标题），逐段列出，一段不落：
- 小标题：原文这一段在讲什么（一句话）。
- 最简切入：学员要用自己的真实料写这一段，最省事从哪个角度开头、写什么内容，用一两句话讲清，具体到"从你XX经历里的XX瞬间写起"。
- 可替换方向（括号里）：判断这段经历学员大概率有没有——如果他可能没这段经历，给 2-3 个同框架下的替代写法方向（基于他的定位，用 / 分隔），让他能直接换；如果他大概率有，写"（你大概率有，直接用）"。

示例：
1. 小标题：刚生完娃那笔糊涂账（可替换方向：如果你没算过账→换成"第一次发现产假工资缩水"/"老公说养娃不贵我翻了账单"）
   最简切入：从你第一次发现"产假工资比预想少这么多"的那个瞬间写起，算一笔你自己的真实账。"""

BAOKUAN_STEP2_PROMPT = """你是一位极简高效的「IP爆款内容教练」。学员已经拿到爆款的【标题框架 + 逐段最简切入】，并据此写了每段的真实料（可能只是几句话）。现在请你把这些料，按框架扩写成一篇完整的、完全是学员自己血肉的文章。全程简体中文。

做法：
- 严格沿用上一步的【标题】和【逐段最简切入】的段落顺序，一段不落。
- 把学员提供的每段料，扩写成自然流畅的正文；料不够的地方标注「此处需补充：XXX」，绝不替学员虚构经历/数字/对话。
- 保留学员真实语气，不要培训机构腔、不要成功学、不要画大饼。
- 文末给 2 个可替换标题建议（沿用原爆款标题公式，换成学员主题）。

【铁律】学员没提供的真实细节绝不编造。"""

# 内容方向库（v8 新增）：定位确定后，一键生成 30 条可发的内容方向，每条带「微信搜索标签」
# 学员拿标签去微信搜一搜找同主题爆款，找到后到「爆款教练」贴进来拆框架
CONTENT_PLAN_PROMPT = """你是一个帮普通人做自媒体内容规划的教练。学员已经确定了定位/人设，请你基于这个定位，生成 30 条他接下来 30-100 天可以持续发的内容方向（选题）。

要求：
- 覆盖他定位下的不同角度：亲身经历 / 观点输出 / 避坑提醒 / 方法教学 / 情绪共鸣 / 互动提问 / 节点热点 / 好物测评…，不要全是同一种。
- 每条给 2-4 个「微信搜索标签」——这是短关键词，学员会拿去微信「搜一搜」里找同主题的爆款文章/视频。标签要具体、像普通人会搜的词（如「辅食机 智商税」「二胎 花销」「普通妈妈 委屈」），别太长、别写成句子。
- 每条一句话说明这个方向的角度/为什么值得做。
- 全部基于他的真实定位，别飘到不相关赛道。
- 用简体中文，口语、真实，别空谈。

只输出 JSON，不要输出任何其他文字。格式严格如下：
[{"title":"选题标题","tags":["标签1","标签2"],"angle":"这句话为什么值得做"}, ... 共 30 条 ...]"""

# 成稿润色器（v8 新增）：爆款框架 + 学员草稿 → AI 修正错别字/病句、理顺语句、保留真实语气
POLISH_PROMPT = """你是一个帮学员润色稿子的编辑。学员已经拿到一条爆款的「结构框架」，并据此写出了自己的草稿初稿。

请你在框架的节奏下，把这份草稿润色成一篇可以发布的成稿：
- 修正错别字、标点错误、病句，理顺不通顺的句子；
- 保持学员的真实语气和原意，不替他虚构任何经历、数字、对话、人名；
- 框架要求的段落顺序和情绪节点尽量保留（标题/钩子/情绪点/金句/引导）；
- 保留学员自己的真实细节（时间/地点/对话/数字/情绪），只改表达、不改事实；
- 像他本人会发的那样，不要培训机构腔、不要成功学、不要画大饼。

直接输出润色后的完整成稿（可用 Markdown，标题用 ## 表示层级），不要输出任何解释、不要输出「修改说明」「以下是润色稿」之类前缀。"""

# ---------- 定位罗盘润色 ----------
LUOPAN_POLISH_PROMPT = """你是一位顶尖的个人品牌文案写手，擅长把零散的原始素材打磨成一段有节奏、有张力、让人记住的「品牌定位话术」。

学员给了你他的四要素原始素材（可能口语化、啰嗦、编号混乱），请你把它重写成一段**流畅、专业、有力量感**的中文品牌介绍。

【风格参考——你要写出这种质感】
我是阿德，00 后三本出身，大二入局自媒体，不靠资源、不靠背景，纯实操把自媒体变现做到 100 万。
此前任职头部千万级自媒体公司，深耕 IP 发售体系，多场联合操盘活动累计总 GMV 突破 500 万。
2026 年 3 月正式离职全职做自由 IP，专注自媒体私域变现赛道，打通 AI 工具、内容创作、公域流量、私域成交、IP 产品全链路闭环，线下线上累计赋能 10000 + 学员拿到变现结果。

【写作原则】
1. 用第三人称或第一人称都可以，选更有力量的那个；
2. 把数字和结果前置或嵌入关键位置（"100万""500万GMV""10000+学员"）；
3. 去掉口语废话（"我觉得""其实""然后"），每句话都要有信息量；
4. 分 3-4 段，每段一个主题（出身/经历 → 能力证明 → 现在做的事 → 赋能成果）；
5. 结尾要有「全链路」「闭环」「赋能」这类有体量的词收住；
6. **绝对不能编造**学员没说的事实和数字，但可以重新组织语序让同样的事实听起来更厉害；
7. 控制总字数在 150-250 字之间。

【输出格式】只输出纯文本，不要 JSON、不要 markdown 标记、不要任何前缀说明。直接输出那段话本身。"""

LUOPAN_SITE_PASSWORD = os.environ.get("LUOPAN_SITE_PASSWORD", "ade2026")  # 定位罗盘站密码，用于轻量鉴权


def parse_plan_json(text):
    """从 AI 返回里尽量抽出 JSON 数组（兼容前后有废话的情况）。失败返回 None。"""
    import re
    s = (text or "").strip()
    try:
        arr = json.loads(s)
        if isinstance(arr, list):
            return arr
    except Exception:
        pass
    m = re.search(r"\[.*\]", s, re.S)
    if m:
        try:
            arr = json.loads(m.group(0))
            if isinstance(arr, list):
                return arr
        except Exception:
            pass
    return None

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
        "oneliner": "我是精打细算的二胎妈妈，我要帮普通家庭妈妈解决'钱不多但想给娃最好'的育儿焦虑，用真实省钱踩坑+一周餐谱实测的方法。",
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
    "content_plan": {
        "mode": "mock",
        "plans": [
            {"title": "我花800块买的辅食机，第3天就吃灰了", "tags": ["辅食机 智商税", "二胎 花销", "育儿 踩坑"], "angle": "真实踩坑最有共鸣，开启避坑系列"},
            {"title": "普通二胎家庭，一个月菜钱到底要多少", "tags": ["二胎 花销", "家庭餐费", "省钱 日常"], "angle": "用真实数字建立信任，引发同阶层对账单"},
            {"title": "月薪8k，我是怎么给娃存下第一笔钱的", "tags": ["普通家庭 攒钱", "给孩子存钱", "月薪8k"], "angle": "结果见证+方法，适合做爆款钩子"},
            {"title": "那些年，我妈让我别买的母婴用品", "tags": ["母婴 避坑", "婆婆 带娃", "育儿 矛盾"], "angle": "代际冲突话题自带情绪流量"},
            {"title": "周末带娃不花钱，我们去了这三个地方", "tags": ["免费 遛娃", "周末 去哪", "亲子 日常"], "angle": "实用+情绪，易被收藏"},
            {"title": "二胎之后，我和老公第一次吵架是因为钱", "tags": ["二胎 矛盾", "夫妻 钱", "真实 婚姻"], "angle": "真实婚姻细节，强共鸣"},
            {"title": "别再囤货了，这5样母婴品我劝你现用现买", "tags": ["母婴 囤货", "理智 消费", "新手 妈妈"], "angle": "反直觉观点，引发讨论"},
            {"title": "幼儿园门口听到的三句话，让我破防了", "tags": ["幼儿园 日常", "妈妈 情绪", "普通 委屈"], "angle": "场景还原+情绪点"},
            {"title": "我用「等一周」原则，半年省下一辆车钱", "tags": ["消费 克制", "省钱 方法", "二胎 妈妈"], "angle": "方法教学+夸张结果钩子"},
            {"title": "娃生病那晚，我在急诊室学到的三件事", "tags": ["娃 生病", "急诊 经历", "妈妈 成长"], "angle": "危机叙事+干货"},
            {"title": "普通妈妈的自媒体第一篇，就这样发了", "tags": ["新手 自媒体", "普通 妈妈", "起步 真实"], "angle": "陪伴式记录，降低门槛"},
            {"title": "婆婆带娃和妈妈带娃，差别有多大", "tags": ["婆婆 带娃", "带娃 方式", "家庭 关系"], "angle": "对比体，易引战讨论"},
            {"title": "我把娃的童年照片做成了一本书", "tags": ["记录 童年", "亲子 仪式", "妈妈 用心"], "angle": "温暖+方法，适合种草"},
            {"title": "二胎家庭的「老大专属时间」，太重要了", "tags": ["二胎 老尴", "平衡 爱", "育儿 心理"], "angle": "观点输出，引发反思"},
            {"title": "超市临期区，是我家的宝藏角落", "tags": ["临期 食品", "超市 省钱", "生活 智慧"], "angle": "反差感+实用"},
            {"title": "娃的入园焦虑，被我用一招化解了", "tags": ["入园 焦虑", "分离 恐惧", "育儿 方法"], "angle": "方法教学+结果"},
            {"title": "普通家庭的仪式感，不花钱也够浓", "tags": ["家庭 仪式感", "不花钱", "普通 幸福"], "angle": "情绪共鸣+反转"},
            {"title": "我退了99%的母婴群，世界清净了", "tags": ["母婴 群", "信息 焦虑", "做减法"], "angle": "反共识观点"},
            {"title": "娃第一次说「妈妈我爱你」，我当场哭了", "tags": ["娃 暖心", "妈妈 泪目", "真实 瞬间"], "angle": "情绪金句位"},
            {"title": "二胎后我重新找了份兼职，不为钱", "tags": ["二胎 妈妈", "兼职 意义", "自我 价值"], "angle": "自我成长话题"},
            {"title": "别给娃报班了，先带他认识菜市场", "tags": ["不报班", "生活 教育", "接地气"], "angle": "反内卷观点"},
            {"title": "我家的「犯错不挨骂」实验，第30天", "tags": ["育儿 实验", "不吼 娃", "耐心 妈妈"], "angle": "连载体+结果"},
            {"title": "老公带娃的一天，比我上班还累", "tags": ["老公 带娃", "丧偶 式育儿", "真实 吐槽"], "angle": "吐槽体自带流量"},
            {"title": "普通妈妈的朋友圈，也有高光时刻", "tags": ["妈妈 朋友圈", "普通 闪光", "自我 记录"], "angle": "身份认同"},
            {"title": "我用旧衣服给娃改了个小书包", "tags": ["旧物 改造", "手工 妈妈", "省钱 创意"], "angle": "动手+省钱"},
            {"title": "娃的压岁钱，我这样存下了", "tags": ["压岁钱", "给娃存钱", "理财 启蒙"], "angle": "节点热点（过年）"},
            {"title": "二胎家庭的早餐，10分钟搞定", "tags": ["二胎 早餐", "快手 食谱", "妈妈 实用"], "angle": "实用收藏"},
            {"title": "我和娃的「睡前三句话」，坚持一年了", "tags": ["睡前 仪式", "亲子 沟通", "长期 习惯"], "angle": "方法+坚持结果"},
            {"title": "普通妈妈也会被网暴？我经历了一次", "tags": ["宝妈 网暴", "真实 遭遇", "心理 边界"], "angle": "冲突叙事"},
            {"title": "带娃去公园，我戒掉了手机", "tags": ["带娃 专注", "戒 手机", "陪伴 质量"], "angle": "自律+反思"},
            {"title": "二胎后，我终于学会了喊累", "tags": ["妈妈 喊累", "自我 关怀", "真实 疲惫"], "angle": "情绪出口，强共鸣"},
        ],
    },
    "polish": {
        "mode": "mock",
        "text": "（演示·未配置真实 AI 时的示例）\n\n## 我花800块买的辅食机，第3天就吃灰了\n\n说实话，当初看直播间那段演示，我心里一长草就下单了。结果收到货才发现，娃根本不吃泥糊——他就要抓着啃。那台机器现在躺在柜子顶上，落了一层灰。\n\n后来我想通了：普通家庭带娃，最贵的不是买不起，是买了用不上。先借朋友的、先试一周，再决定要不要花钱，能省下大半冤枉钱。\n\n你家有这种「买来就后悔」的东西吗？评论区聊聊，我整理了一份「母婴避坑清单」发你。",
    },
}

# 爆款教练降级示例（未配置 AI 时返回，保证流程可演示）
MOCK_BAOKUAN_STEP1 = """（演示·未配置真实 AI 时的示例）

【标题（框架）】
1. 我花3000块给娃踩的5个母婴智商税
2. 框架逻辑：数字清单 + 痛点直击——用"花了多少钱"制造痛感，用"5个"给明确预期。
3. 可改方向：把"3000块/5个"换成你自己的真实数字和坑点，如"我当妈第一年踩的3个坑"或"工资8k怎么给娃凑出第一桶金"。

【逐段最简切入】
1. 小标题：开头·我当初也信了（可替换方向：如果你没冲动买过→换成"听母婴群安利差点下单"/"闺蜜踩坑我围观"）
   最简切入：从你第一次兴冲冲想下单某母婴"神器"、后来发现是智商税的那个瞬间写起，带一句当时的心理。
2. 小标题：坑1·最不值当的XXX（可替换方向：如果你没花大钱→换成"最鸡肋的赠品"/"退不掉的那单"）
   最简切入：挑你踩过最贵的一个坑，算一笔真实账（花多少、买啥、为啥后悔），一两句就能开。
3. 小标题：坑2~坑5·我也替你试了（你大概率有，直接用）
   最简切入：把剩下几个坑各用一句话带出，每个都写你自己的真实替代品。
4. 小标题：结尾·我整理的避坑清单（可替换方向：如果你没清单→换成"评论区问我"/"置顶一篇合集"）
   最简切入：从"评论区想要的话我发你"这个动作写起，顺手点出你愿意分享的那份清单。"""

MOCK_BAOKUAN_STEP2 = """（演示·未配置真实 AI 时的示例）

【我的稿】（按上面框架扩写）
标题：我花3000块给娃踩的5个母婴智商税
开头·我当初也信了：说实话，我刚当妈那会儿……（此处需补充：你下单的第一个"神器"是什么）
坑1：最不值当的XXX——我花XXX块买了XXX，结果发现……
坑2~坑5：……
结尾：评论区想要避坑清单的，我整理了一份"新手妈妈别乱买"，私我发你。

【可替换标题】
1. 普通妈妈别再交这5种冤枉钱（身份共鸣+痛点直击）
2. 这些母婴品，我替你先踩了雷（结果见证+身份共鸣）

【铁律】未提供真实细节处已标注「此处需补充」，绝不编造。"""

AI_HANDLERS = {
    "diagnose": lambda r: f"职业：{r.get('job','')}\n兴趣：{r.get('interest','')}\n每日可投入：{r.get('time','')}\n已有资源：{r.get('resource','')}",
    "position": lambda r: f"赛道：{r.get('track','')}\n个人经历：{r.get('experience','')}\n优势：{r.get('strength','')}\n看似缺点的特质：{r.get('flaw','')}",
    "content": lambda r: f"定位：{r.get('position','')}\n想做的选题方向：{r.get('topic','')}\n形式：{r.get('format','短视频口播脚本')}",
    "templates": lambda r: f"赛道：{r.get('track','')}",
    "monetize": lambda r: f"账号阶段：{r.get('stage','')}\n赛道：{r.get('track','')}",
}

# 建表语句：两种数据库各自的写法
SQLITE_DDL = """
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
"""

PG_DDL = """
CREATE TABLE IF NOT EXISTS users(
  id SERIAL PRIMARY KEY,
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
  id SERIAL PRIMARY KEY,
  user_id INTEGER NOT NULL,
  kind TEXT NOT NULL,
  title TEXT,
  content TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_schemes_user ON schemes(user_id);
"""


def init_db():
    c = db_conn()
    ddl = PG_DDL if USE_PG else SQLITE_DDL
    for stmt in ddl.split(";"):
        s = stmt.strip()
        if s:
            c.execute(s)
    c.commit()
    c.close()
    # 启动时根据环境变量自动建/升级管理员账号
    if ADMIN_USERNAME and ADMIN_PASSWORD:
        c = db_conn()
        row = c.execute(q("SELECT id,role FROM users WHERE username=?"), (ADMIN_USERNAME,)).fetchone()
        if not row:
            h, salt = hash_pw(ADMIN_PASSWORD)
            now = datetime.now()
            c.execute(
                q("INSERT INTO users(username,pw_hash,pw_salt,role,created_at,daily_quota,quota_date,daily_count) "
                  "VALUES(?,?,?,'admin',?,?,?,0)"),
                (ADMIN_USERNAME, h, salt, now.isoformat(), 999, now.strftime("%Y-%m-%d")))
            c.commit()
            print(f"[INIT] 自动创建管理员账号：{ADMIN_USERNAME}", flush=True)
        elif row["role"] != "admin":
            c.execute(q("UPDATE users SET role='admin' WHERE id=?"), (row["id"],))
            c.commit()
            print(f"[INIT] 升级 {ADMIN_USERNAME} 为管理员", flush=True)
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
    row = c.execute(q("SELECT * FROM sessions WHERE token=?"), (token,)).fetchone()
    if not row:
        c.close()
        return None
    if datetime.fromisoformat(row["expires_at"]) < datetime.now():
        c.execute(q("DELETE FROM sessions WHERE token=?"), (token,))
        c.commit()
        c.close()
        return None
    u = c.execute(q("SELECT * FROM users WHERE id=?"), (row["user_id"],)).fetchone()
    c.close()
    return dict(u) if u else None


def issue_token(uid, username):
    token = secrets.token_hex(32)
    now = datetime.now()
    exp = now + timedelta(days=SESSION_DAYS)
    c = db_conn()
    c.execute(q("INSERT INTO sessions(token,user_id,created_at,expires_at) VALUES(?,?,?,?)"),
              (token, uid, now.isoformat(), exp.isoformat()))
    c.commit()
    c.close()
    return 200, {"token": token, "user": {"username": username, "role": "member"}}


def check_and_inc_quota(user_id):
    """校验并扣减当日配额。返回 (allow, user_dict_after)。"""
    today = datetime.now().strftime("%Y-%m-%d")
    c = db_conn()
    u = c.execute(q("SELECT * FROM users WHERE id=?"), (user_id,)).fetchone()
    d = dict(u)
    if d["quota_date"] != today:
        c.execute(q("UPDATE users SET quota_date=?, daily_count=0 WHERE id=?"), (today, user_id))
        d["quota_date"] = today
        d["daily_count"] = 0
    if d["daily_count"] >= d["daily_quota"]:
        c.close()
        return False, d
    c.execute(q("UPDATE users SET daily_count=daily_count+1 WHERE id=?"), (user_id,))
    c.commit()
    c.close()
    d["daily_count"] += 1
    return True, d


def call_llm(system, user, max_tokens=None):
    if not API_KEY:
        return None
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.7,
    }
    if max_tokens:
        payload["max_tokens"] = max_tokens
    payload = json.dumps(payload).encode("utf-8")
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


def extract_position_oneliner(text):
    """从定位生成结果里抽取『定位一句话：…』那一行，供前端自动带入第 3/4 步。"""
    if not text:
        return ""
    import re
    m = re.search(r"定位一句话[：:]\s*(.+)", text)
    if m:
        return m.group(1).strip().strip("。").strip()
    return ""


# ---------- 新榜爆款数据源 ----------
NEWRANK_TYPE_MAP = [
    ("育儿|母婴|宝妈|娃|带娃|宝宝", "育儿"),
    ("职场|上班|工作|副业|成长", "职场"),
    ("情感|恋爱|婚姻|关系|老公|老婆", "情感"),
    ("创业|赚钱|理财|财商|致富", "创业"),
    ("美食|吃|菜谱|做饭|烘焙", "美食"),
    ("旅游|旅行|出游|风景", "旅游"),
    ("教育|学习|考试|孩子|学生|启蒙", "教育"),
    ("健康|健身|养生|减肥", "健康"),
    ("科技|数码|AI|人工智能|互联网|手机", "科技"),
    ("财经|投资|股票|基金|房产", "财经"),
    ("美妆|穿搭|时尚|护肤", "时尚"),
    ("家居|装修|收纳|整理", "房产"),
]
def track_to_newrank_type(track):
    t = track or ""
    for kw, tp in NEWRANK_TYPE_MAP:
        for k in kw.split("|"):
            if k and k in t:
                return tp
    return "文化"

def fetch_newrank_hot(track):
    """拉该赛道对应的新榜类别当日热门文章。返回 list[{title,summary,url,read_num,content}]，失败/未配置返回 None"""
    if not NEWRANK_KEY:
        return None
    ntype = track_to_newrank_type(track)
    url = "https://api.newrank.cn/api/sync/weixin/data/hot/day_content"
    body = urllib.parse.urlencode({
        "type": ntype,
        "date": datetime.now().strftime("%Y-%m-%d"),
        "size": "10",
    }).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={
        "Content-Type": "application/x-www-form-urlencoded;charset=utf-8",
        "Key": NEWRANK_KEY,
    })
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            j = json.loads(resp.read().decode("utf-8"))
        items = (j.get("data") or []) if isinstance(j, dict) else []
        out = []
        for it in items[:10]:
            if not isinstance(it, dict):
                continue
            content = it.get("content") or it.get("summary") or ""
            out.append({
                "title": it.get("title", "") or it.get("content_title", ""),
                "summary": (content[:180] if content else ""),
                "url": it.get("url", "") or it.get("article_url", ""),
                "read_num": it.get("read_num") or it.get("read_count") or it.get("hot_value") or "",
                "content": content,
            })
        return out
    except Exception as e:
        print("[NEWRANK_ERR]", e, flush=True)
        return None


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
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Site-Password")
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
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Site-Password")
        self.end_headers()

    def do_GET(self):
        if self.path == "/api/health":
            self._api_health()
        elif self.path == "/api/me":
            self._api_me()
        elif self.path == "/api/schemes":
            self._api_list_schemes()
        elif self.path == "/api/admin/users":
            self._api_admin_list_users()
        elif self.path.startswith("/api/"):
            self._send_json({"error": "method not allowed"}, 405)
        else:
            self._serve_static()

    def do_POST(self):
        path = self.path
        if not path.startswith("/api/"):
            return self._send_json({"error": "not found"}, 404)
        if path == "/api/register":
            return self._api_register(self._read_json())
        if path == "/api/login":
            return self._api_login(self._read_json())
        if path == "/api/schemes":
            return self._api_save_scheme(self._read_json())
        if path == "/api/admin/users":
            return self._api_admin_create_user(self._read_json())
        if path.startswith("/api/admin/users/"):
            rest = path[len("/api/admin/users/"):]
            parts = rest.split("/")
            if len(parts) == 2 and parts[1] == "reset":
                return self._api_admin_reset_password(parts[0], self._read_json())
            if len(parts) == 2 and parts[1] == "quota":
                return self._api_admin_set_quota(parts[0], self._read_json())
            return self._send_json({"error": "unknown admin endpoint"}, 404)
        endpoint = path[len("/api/"):]
        if endpoint in AI_HANDLERS:
            return self._api_ai(endpoint, self._read_json())
        if endpoint == "hot-templates":
            return self._api_hot_templates(self._read_json())
        if endpoint == "refine-template":
            return self._api_refine(self._read_json())
        if endpoint == "generate-from-template":
            return self._api_gen_template(self._read_json())
        if endpoint == "baokuan-step1":
            return self._api_baokuan_step1(self._read_json())
        if endpoint == "baokuan-step2":
            return self._api_baokuan_step2(self._read_json())
        if endpoint == "content-plan":
            return self._api_content_plan(self._read_json())
        if endpoint == "polish":
            return self._api_polish(self._read_json())
        if endpoint == "luopan-polish":
            return self._api_luopan_polish(self._read_json())
        self._send_json({"error": "unknown endpoint"}, 404)

    def do_DELETE(self):
        path = self.path
        if path.startswith("/api/schemes/"):
            sid = path[len("/api/schemes/"):]
            return self._api_del_scheme(sid)
        if path.startswith("/api/admin/users/"):
            uid = path[len("/api/admin/users/"):]
            return self._api_admin_delete_user(uid)
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
        if not ALLOW_REGISTER:
            return self._send_json({"error": "暂不开放注册，请联系阿德获取账号"}, 403)
        username = (req.get("username") or "").strip()
        password = req.get("password") or ""
        if not username or not password:
            return self._send_json({"error": "用户名和密码必填"}, 400)
        if len(password) < 6:
            return self._send_json({"error": "密码至少 6 位"}, 400)
        c = db_conn()
        if c.execute(q("SELECT id FROM users WHERE username=?"), (username,)).fetchone():
            c.close()
            return self._send_json({"error": "用户名已存在"}, 409)
        h, salt = hash_pw(password)
        now = datetime.now()
        uid = insert_id(c,
            "INSERT INTO users(username,pw_hash,pw_salt,role,created_at,daily_quota,quota_date,daily_count) "
            "VALUES(?,?,?,'member',?,?,?,0)",
            (username, h, salt, now.isoformat(), DEFAULT_QUOTA, now.strftime("%Y-%m-%d")))
        c.commit()
        c.close()
        code, body = issue_token(uid, username)
        self._send_json(body, code)

    def _api_login(self, req):
        username = (req.get("username") or "").strip()
        password = req.get("password") or ""
        c = db_conn()
        u = c.execute(q("SELECT * FROM users WHERE username=?"), (username,)).fetchone()
        c.close()
        if not u or not hash_pw(password, u["pw_salt"])[0] == u["pw_hash"]:
            print(f"[LOGIN_FAIL] ip={self.client_address[0]} user='{username}' exists={u is not None}", flush=True)
            return self._send_json({"error": "用户名或密码错误"}, 401)
        code, body = issue_token(u["id"], username)
        self._send_json(body, code)

    def _api_health(self):
        """自检端点：浏览器直接打开 /api/health 就能看到配置是否生效（不泄露任何密钥）"""
        info = {
            "ok": True,
            "版本": "v8.4",
            "数据库模式": "Postgres（持久化·重部署不丢账号）" if USE_PG else "SQLite（临时·重部署会丢账号）",
            "持久化": USE_PG,
            "AI已配置": bool(API_KEY),
            "AI模型": MODEL if API_KEY else "未配置（走演示模式）",
            "管理员后台已启用": bool(ADMIN_TOKEN),
            "开放注册": ALLOW_REGISTER,
            "爆款教练模式": "站内闭环·学员手动贴爆款（无需外部API）",
            "定位一句话": "第2步定位→自动带入第3/4步（我是谁/帮谁/解决什么问题/用什么方法）",
            "成稿润色器": "框架+草稿→修正错别字病句",
        }
        try:
            c = db_conn()
            cur = c.execute("SELECT COUNT(*) AS n FROM users")
            row = cur.fetchone()
            info["账号总数"] = row["n"] if row else 0
            c.close()
            info["数据库连接"] = "正常"
        except Exception as e:
            info["ok"] = False
            info["数据库连接"] = "失败：%s" % str(e)[:200]
        self._send_json(info)

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
        ai = call_llm(PROMPTS[endpoint] + FORMAT_TIP, AI_HANDLERS[endpoint](req))
        meta = {"used": d["daily_count"], "quota": d["daily_quota"]}
        if ai:
            resp = {"mode": "live", "text": ai, **meta}
            if endpoint == "position":
                ol = extract_position_oneliner(ai)
                if ol:
                    resp["oneliner"] = ol
            self._send_json(resp)
        else:
            self._send_json({**MOCK[endpoint], **meta})

    # ---------- 爆款模板库（新榜数据源 + AI 提炼/出稿） ----------
    def _require_quota(self):
        """鉴权 + 扣配额，返回 (user, d) 或发送错误并返回 None"""
        user = auth_user(self)
        if not user:
            self._send_json({"error": "请先登录", "needLogin": True}, 401)
            return None
        allow, d = check_and_inc_quota(user["id"])
        if not allow:
            self._send_json({
                "error": "今日调用额度已用尽，明天重置",
                "used": d["daily_count"], "quota": d["daily_quota"], "quotaExceeded": True,
            }, 429)
            return None
        return user, d

    def _api_hot_templates(self, req):
        user = auth_user(self)
        if not user:
            return self._send_json({"error": "请先登录", "needLogin": True}, 401)
        track = (req.get("track") or "").strip()
        items = fetch_newrank_hot(track)
        if items is None:
            # 降级：示例模板（演示用，提示未配置真实数据源）
            items = [
                {"title": "我花3000块踩的5个母婴智商税", "summary": "（示例）开头晒真实踩坑，列3-5个坑，给替代方案，结尾送清单", "url": "", "read_num": "", "content": "示例爆款正文：开头晒一个真实踩坑，列3-5个同类坑并给替代方案，结尾送清单。"},
                {"title": "普通妈妈的一周餐谱花销实录", "summary": "（示例）连续7天记录餐谱花销，展示数据，复盘可复用方法", "url": "", "read_num": "", "content": "示例爆款正文：连续7天记录某件事，展示数据/变化，复盘可复用方法。"},
                {"title": "两款婴儿车我替你扛了30天", "summary": "（示例）A vs B 实测，给结论，说明适用人群", "url": "", "read_num": "", "content": "示例爆款正文：A vs B 实测，给出结论，说明适用人群。"},
            ]
            return self._send_json({"templates": items, "source": "mock", "needKey": True,
                                    "message": "当前为示例模板；在环境变量配置 NEWRANK_KEY 后拉取真实爆款。"})
        return self._send_json({"templates": items, "source": "newrank", "type": track_to_newrank_type(track)})

    def _api_refine(self, req):
        r = self._require_quota()
        if not r:
            return
        user, d = r
        content = (req.get("content") or "").strip()
        if not content:
            return self._send_json({"error": "缺少爆款内容"}, 400)
        structure = call_llm(REFINE_PROMPT, "爆款内容：\n" + content[:4000])
        meta = {"used": d["daily_count"], "quota": d["daily_quota"]}
        if structure and not structure.startswith("[AI调用失败]"):
            self._send_json({"mode": "live", "structure": structure, **meta})
        else:
            self._send_json({"mode": "mock", "structure": "（演示）钩子：晒真实踩坑；结构：列坑→替代方案→送清单；情绪点：共鸣'我也踩过'；金句位：结尾；引导：送资料。", **meta})

    def _api_gen_template(self, req):
        r = self._require_quota()
        if not r:
            return
        user, d = r
        structure = (req.get("structure") or "").strip()
        position = (req.get("position") or "").strip()
        detail = (req.get("detail") or "").strip()
        fmt = (req.get("format") or "文章").strip()
        if not structure or not detail:
            return self._send_json({"error": "请先提炼模板结构并填写你的具体情况"}, 400)
        user_msg = (
            f"爆款结构骨架：\n{structure}\n\n"
            f"学员定位：{position}\n\n"
            f"学员的具体情况（要填进模板的真实素材）：\n{detail}"
        )
        text = call_llm(GEN_PROMPT.format(format=fmt) + GEN_FORMAT_TIP, user_msg)
        meta = {"used": d["daily_count"], "quota": d["daily_quota"]}
        if text and not text.startswith("[AI调用失败]"):
            self._send_json({"mode": "live", "text": text, **meta})
        else:
            self._send_json({"mode": "mock", "text": "（演示）基于模板结构 + 你的定位写出的成稿占位。配置真实 AI 后生成可用内容。", **meta})

    # ---------- 爆款教练（v7：学员手动贴爆款原文，站内闭环，无需外部 API） ----------
    def _api_baokuan_step1(self, req):
        r = self._require_quota()
        if not r:
            return
        user, d = r
        position = (req.get("position") or "").strip()
        hot = (req.get("hot_content") or "").strip()
        fmt = (req.get("format") or "文章").strip()
        if not hot:
            return self._send_json({"error": "请先粘贴一条你搜到的爆款原文"}, 400)
        user_msg = (
            f"学员定位：{position or '（未填，按爆款本身推断）'}\n"
            f"产出形式：{fmt}\n\n"
            f"==== 学员粘贴的爆款原文 ====\n{hot[:5000]}"
        )
        text = call_llm(BAOKUAN_STEP1_PROMPT, user_msg, max_tokens=1000)
        meta = {"used": d["daily_count"], "quota": d["daily_quota"]}
        if text and not text.startswith("[AI调用失败]"):
            self._send_json({"mode": "live", "text": text, **meta})
        else:
            self._send_json({"mode": "mock", "text": MOCK_BAOKUAN_STEP1, **meta})

    def _api_baokuan_step2(self, req):
        r = self._require_quota()
        if not r:
            return
        user, d = r
        position = (req.get("position") or "").strip()
        hot = (req.get("hot_content") or "").strip()
        fmt = (req.get("format") or "文章").strip()
        structure = (req.get("structure") or "").strip()
        answers = (req.get("answers") or "").strip()
        if not answers:
            return self._send_json({"error": "请先按问题清单填写你的真实经历"}, 400)
        user_msg = (
            f"学员定位：{position or '（未填）'}\n"
            f"产出形式：{fmt}\n\n"
            f"==== 爆款原文 ====\n{hot[:3000]}\n\n"
            f"==== 上一步生成的爆款结构拆解 ====\n{structure}\n\n"
            f"==== 学员按问题清单填写的真实素材 ====\n{answers}"
        )
        text = call_llm(BAOKUAN_STEP2_PROMPT, user_msg)
        meta = {"used": d["daily_count"], "quota": d["daily_quota"]}
        if text and not text.startswith("[AI调用失败]"):
            self._send_json({"mode": "live", "text": text, **meta})
        else:
            self._send_json({"mode": "mock", "text": MOCK_BAOKUAN_STEP2, **meta})

    # ---------- 内容方向库（v8：定位 → 30 条内容方向 + 微信搜索标签） ----------
    def _api_content_plan(self, req):
        r = self._require_quota()
        if not r:
            return
        user, d = r
        position = (req.get("position") or "").strip()
        if not position:
            return self._send_json({"error": "请先填写你的定位/人设（或先完成第 2 步定位）"}, 400)
        text = call_llm(CONTENT_PLAN_PROMPT, "学员定位/人设：" + position)
        meta = {"used": d["daily_count"], "quota": d["daily_quota"]}
        if text and not text.startswith("[AI调用失败]"):
            plans = parse_plan_json(text)
            if plans:
                return self._send_json({"mode": "live", "plans": plans, **meta})
            # AI 没按 JSON 返回：降级成纯文本展示（仍扣了配额，但能看）
            return self._send_json({"mode": "live", "plans": None, "raw": text, **meta})
        return self._send_json({"mode": "mock", "plans": MOCK["content_plan"]["plans"], **meta})

    # ---------- 成稿润色器（v8：爆款框架 + 学员草稿 → 修正错别字病句） ----------
    def _api_polish(self, req):
        r = self._require_quota()
        if not r:
            return
        user, d = r
        framework = (req.get("framework") or "").strip()
        draft = (req.get("draft") or "").strip()
        if not draft:
            return self._send_json({"error": "请先在「我的草稿初稿」里写点内容"}, 400)
        user_msg = (
            f"==== 爆款结构框架（参考节奏，可空）====\n{framework}\n\n"
            f"==== 学员草稿初稿（请润色：改错别字/病句，保留真实语气）====\n{draft}"
        )
        text = call_llm(POLISH_PROMPT, user_msg)
        meta = {"used": d["daily_count"], "quota": d["daily_quota"]}
        if text and not text.startswith("[AI调用失败]"):
            return self._send_json({"mode": "live", "text": text, **meta})
        return self._send_json({"mode": "mock", "text": MOCK["polish"]["text"], **meta})

    # ---------- 定位罗盘润色（外部站代理，不需登录鉴权） ----------
    def _api_luopan_polish(self, req):
        """定位罗盘站的 AI 润色接口。通过 X-Site-Password 头做轻量密码校验，不消耗学员配额。"""
        # 密码校验
        pwd = self.headers.get("X-Site-Password", "")
        if pwd != LUOPAN_SITE_PASSWORD:
            return self._send_json({"error": "站点密码错误"}, 403)
        # 取原始素材
        who = (req.get("who") or "").strip()
        audience = (req.get("audience") or req.get("problem", "")).strip()
        problem = (req.get("problem") or req.get("val", "")).strip()
        method = (req.get("method") or "").strip()
        if not who and not audience and not problem:
            return self._send_json({"error": "请提供定位素材"}, 400)
        user_msg = f"""【学员原始素材】
我是谁：{who}
帮哪几类人：{audience}
解决什么问题：{problem}
用什么方法（产品形式）：{method}

请根据以上素材，输出一段有节奏、有张力的品牌定位话术。"""
        text = call_llm(LUOPAN_POLISH_PROMPT, user_msg)
        if not text or text.startswith("[AI调用失败]"):
            return self._send_json({"error": "AI 润色失败，请稍后重试或使用原始版"}, 503)
        # 返回纯文本
        return self._send_json({"text": text.strip()})

    # ---------- 方案存档 ----------
    def _api_list_schemes(self):
        user = auth_user(self)
        if not user:
            return self._send_json({"error": "未登录"}, 401)
        c = db_conn()
        rows = c.execute(
            q("SELECT id,kind,title,content,created_at FROM schemes WHERE user_id=? ORDER BY id DESC"),
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
        sid = insert_id(c,
            "INSERT INTO schemes(user_id,kind,title,content,created_at) VALUES(?,?,?,?,?)",
            (user["id"], kind, title, text, datetime.now().isoformat()))
        c.commit()
        c.close()
        self._send_json({"ok": True, "id": sid})

    def _api_del_scheme(self, sid):
        user = auth_user(self)
        if not user:
            return self._send_json({"error": "未登录"}, 401)
        c = db_conn()
        c.execute(q("DELETE FROM schemes WHERE id=? AND user_id=?"), (sid, user["id"]))
        c.commit()
        c.close()
        self._send_json({"ok": True})

    # ---------- 管理后台（需 ADMIN_TOKEN） ----------
    def _check_admin(self):
        if not ADMIN_TOKEN:
            return self._send_json({"error": "管理员 token 未配置"}, 403)
        if self.headers.get("X-Admin-Token", "") != ADMIN_TOKEN:
            return self._send_json({"error": "管理员 token 不正确"}, 403)
        return None

    def _api_admin_list_users(self):
        err = self._check_admin()
        if err: return err
        c = db_conn()
        rows = c.execute("SELECT id,username,role,created_at,daily_quota,quota_date,daily_count FROM users ORDER BY id").fetchall()
        c.close()
        today = datetime.now().strftime("%Y-%m-%d")
        users = [{
            "id": r["id"], "username": r["username"], "role": r["role"],
            "created_at": r["created_at"], "daily_quota": r["daily_quota"],
            "used_today": 0 if r["quota_date"] != today else r["daily_count"],
        } for r in rows]
        self._send_json({"users": users, "count": len(users)})

    def _api_admin_create_user(self, req):
        err = self._check_admin()
        if err: return err
        username = (req.get("username") or "").strip()
        password = req.get("password") or ""
        try: daily_quota = int(req.get("daily_quota", DEFAULT_QUOTA))
        except: daily_quota = DEFAULT_QUOTA
        if not username:
            return self._send_json({"error": "用户名必填"}, 400)
        if len(password) < 6:
            return self._send_json({"error": "密码至少 6 位"}, 400)
        c = db_conn()
        if c.execute(q("SELECT id FROM users WHERE username=?"), (username,)).fetchone():
            c.close()
            return self._send_json({"error": "用户名已存在"}, 409)
        h, salt = hash_pw(password)
        now = datetime.now()
        uid = insert_id(c,
            "INSERT INTO users(username,pw_hash,pw_salt,role,created_at,daily_quota,quota_date,daily_count) "
            "VALUES(?,?,?,'member',?,?,?,0)",
            (username, h, salt, now.isoformat(), daily_quota, now.strftime("%Y-%m-%d")))
        c.commit()
        c.close()
        self._send_json({"id": uid, "username": username, "daily_quota": daily_quota})

    def _api_admin_reset_password(self, uid_str, req):
        err = self._check_admin()
        if err: return err
        password = req.get("password") or ""
        if len(password) < 6:
            return self._send_json({"error": "新密码至少 6 位"}, 400)
        try: uid = int(uid_str)
        except: return self._send_json({"error": "无效 uid"}, 400)
        c = db_conn()
        if not c.execute(q("SELECT id,username FROM users WHERE id=?"), (uid,)).fetchone():
            c.close(); return self._send_json({"error": "账号不存在"}, 404)
        h, salt = hash_pw(password)
        c.execute(q("UPDATE users SET pw_hash=?, pw_salt=? WHERE id=?"), (h, salt, uid))
        # 重置后让该用户所有会话失效（强制重登）
        c.execute(q("DELETE FROM sessions WHERE user_id=?"), (uid,))
        c.commit()
        row = c.execute(q("SELECT username FROM users WHERE id=?"), (uid,)).fetchone()
        c.close()
        self._send_json({"ok": True, "username": row["username"], "new_password": password})

    def _api_admin_set_quota(self, uid_str, req):
        err = self._check_admin()
        if err: return err
        try:
            uid = int(uid_str)
            qn = int(req.get("daily_quota", DEFAULT_QUOTA))
        except: return self._send_json({"error": "参数错误"}, 400)
        if qn < 1 or qn > 9999:
            return self._send_json({"error": "配额需在 1-9999"}, 400)
        c = db_conn()
        if not c.execute(q("SELECT id FROM users WHERE id=?"), (uid,)).fetchone():
            c.close(); return self._send_json({"error": "账号不存在"}, 404)
        c.execute(q("UPDATE users SET daily_quota=? WHERE id=?"), (qn, uid))
        c.commit()
        c.close()
        self._send_json({"ok": True, "daily_quota": qn})

    def _api_admin_delete_user(self, uid_str):
        err = self._check_admin()
        if err: return err
        try: uid = int(uid_str)
        except: return self._send_json({"error": "无效 uid"}, 400)
        c = db_conn()
        u = c.execute(q("SELECT username,role FROM users WHERE id=?"), (uid,)).fetchone()
        if not u:
            c.close(); return self._send_json({"error": "账号不存在"}, 404)
        if u["role"] == "admin":
            c.close(); return self._send_json({"error": "不能删除管理员"}, 403)
        c.execute(q("DELETE FROM users WHERE id=?"), (uid,))
        c.execute(q("DELETE FROM sessions WHERE user_id=?"), (uid,))
        c.execute(q("DELETE FROM schemes WHERE user_id=?"), (uid,))
        c.commit()
        c.close()
        self._send_json({"ok": True, "deleted": u["username"]})

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    init_db()
    flag = "已配置" if API_KEY else "未配置→示例数据"
    reg = "开" if ALLOW_REGISTER else "关（仅管理员可创建）"
    admin_init = ADMIN_USERNAME if (ADMIN_USERNAME and ADMIN_PASSWORD) else "未配置"
    db_mode = "Postgres(持久化)" if USE_PG else "SQLite(本地)"
    print(f"学员 AI 系统运行中：http://0.0.0.0:{PORT}", flush=True)
    print(f"  数据库={db_mode} | AI_KEY={flag} | 公开注册={reg} | 管理员账号={admin_init}", flush=True)
    print(f"  管理面板入口：网站 URL 后加 ?admin={'<ADMIN_TOKEN>' if ADMIN_TOKEN else '未配置'}", flush=True)
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
