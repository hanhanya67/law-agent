# -*- coding: utf-8 -*-
"""全局配置。所有可调项集中在这里，改行为不用翻代码。"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
STATIC_DIR = BASE_DIR / "static"


def _load_env() -> None:
    """极简 .env 加载，避免多一个 python-dotenv 依赖。"""
    p = BASE_DIR / ".env"
    if not p.exists():
        return
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env()

# ---- 大模型 ----
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "").strip()
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-flash")
LLM_ENABLED = bool(DEEPSEEK_API_KEY)

# 降级之后隔多久再试一次在线调用（秒）；设 0 表示永不自动恢复、只能重启。
# 为什么需要：`LLM.online` 一旦置 False 就没有任何路径把它置回 True，
# 于是余额充上了、key 换好了，进程也不会自己去试——用户看到的是「一直不回放」。
# 代价是 key 真坏时每 N 秒白花一次失败调用；402 类很快回来，超时类慢一点。
LLM_RECOVER_AFTER = int(os.getenv("LLM_RECOVER_AFTER", "600"))

# ---- 推理模型行为（2026-10 实测后新增）----
# deepseek-flash / deepseek-v4-pro 都是推理模型：先吐 reasoning_content，
# 再吐 content，两者**共享同一个 max_tokens 预算**。这带来两个必须处理的后果：
#   1. 预算给小了，思考吃满、正文为空，finish_reason=length，而且是静默失败；
#   2. effort=max 档实测 4000 token 全被思考吃掉，正文 0 字——所以这里禁掉。
# 关闭思考：thinking={"type":"disabled"}（经 extra_body 透传，OpenAI 兼容端点认这个）
# 注意 enable_thinking 无效、reasoning_effort=low 只是少想一点不等于关闭。

# auto 跟随模型默认（推荐，实测生成质量与速度最平衡）
# off  关闭思考，快一倍但复杂判断会退化（意图分类实测会判错，别用在分类上）
LLM_THINKING = os.getenv("LLM_THINKING", "auto").strip().lower()
# 留空 = 不传，用服务端默认档。可选 low / high。max 已在代码里硬禁。
LLM_EFFORT = os.getenv("LLM_EFFORT", "").strip().lower()

# 各环节 token 预算。依据 2026-10-23 实测的「思考占比」定的：
#   意图     思考 128 / 正文 4     → 400
#   决策     思考 281 / 正文 167   → 1600（要容纳多轮 tool_calls）
#   生成     思考 449 / 正文 780   → 4000（长答案还会更多）
# 全部留了 2~3 倍余量，宁可多给也别截断——截断是静默的，很难发现。
TOK_INTENT = int(os.getenv("TOK_INTENT", "400"))
TOK_DECOMPOSE = int(os.getenv("TOK_DECOMPOSE", "800"))
TOK_SLOTS = int(os.getenv("TOK_SLOTS", "1200"))
TOK_DECIDE = int(os.getenv("TOK_DECIDE", "1600"))
TOK_JUDGE = int(os.getenv("TOK_JUDGE", "1200"))
TOK_GENERATE = int(os.getenv("TOK_GENERATE", "4000"))
TOK_VERIFY = int(os.getenv("TOK_VERIFY", "4000"))
TOK_ASK = int(os.getenv("TOK_ASK", "400"))
# 读图输出的是「字段 + 每字段一句 quote」，比纯抽槽位长：
# 8 个字段各带一句原文，再加 unreadable 列表，给 1200 不够会截断成半截 JSON。
TOK_VISION = int(os.getenv("TOK_VISION", "1600"))

# ---- 判例与联网（v117 建库 / v118 补全联网）----
# 判例库本体（data/cases.jsonl）始终离线可用，不受下面任何开关影响。
#
# CASE_SEARCH_ADAPTER：外部检索的「允许开关」。默认 0（纯本地、离线可用）。
#   设 1 之后还要两件事同时成立才真发请求：netprobe 检测到网络可用，
#   且调用方显式要（SSE / 接口带 external=1，或前端点「联网补充线索」）。
#   所以「自动检测网络」不等于「自动联网」；不带 key 的默认包放在有网机器上
#   也不会悄悄往外部发请求。检索源由 LAW_CASE_SEARCH_URL 配置，
#   或通过 websearch.register() 注入自己的适配器。
CASE_SEARCH_ADAPTER = os.getenv("CASE_SEARCH_ADAPTER", "").strip() == "1"

# ---- 网络自动检测（v118）----
# auto（默认）后台探一次 TCP、结果缓存 LAW_NET_TTL 秒；online / offline 为
# 强制模式，不探测，给演示与回归用。探测目标默认取 DEEPSEEK_BASE_URL 的
# 主机 —— 在线能力本来就要连它，探不相干的公网 IP 会造出假阳性。
# 探测只握手不发包，绝不把提问内容带上网络；任何异常一律按离线。
LAW_NET_MODE = os.getenv("LAW_NET_MODE", "auto").strip().lower()
LAW_NET_PROBE_HOST = os.getenv("LAW_NET_PROBE_HOST", "").strip()
LAW_NET_PROBE_PORT = int(os.getenv("LAW_NET_PROBE_PORT", "443"))
LAW_NET_TIMEOUT_MS = int(os.getenv("LAW_NET_TIMEOUT_MS", "800"))
LAW_NET_TTL = int(os.getenv("LAW_NET_TTL", "300"))
# 外部检索源：URL 模板，{q} 会被 URL 编码后的提问替换。只认
# {"items":[{"title","url","snippet"}]} 这种 JSON，不猜网页 DOM。
# 留空时不会发出请求，来源标签会明说「未配置检索源」。
LAW_CASE_SEARCH_URL = os.getenv("LAW_CASE_SEARCH_URL", "").strip()
LAW_CASE_SEARCH_TIMEOUT_MS = int(os.getenv("LAW_CASE_SEARCH_TIMEOUT_MS", "2500"))

# 正文为空且被截断时，自动把预算翻倍重试一次。上限防止失控。
TOK_RETRY_CAP = int(os.getenv("TOK_RETRY_CAP", "8000"))

# ---- 答案自检策略 ----
# 实测教训：把「逐条核对答案里的引用」交给推理模型，单次能烧 3199 个思考
# token、耗时 29 秒，还经常把预算吃光。而这个检查本质上是字符串比对——
# 引用条号在不在证据里、废止条文有没有提示——规则几毫秒就能做完。
# 更关键的是：规则报出的「引用不在证据中」是确定性结论，不需要模型复核。
#   rule        纯规则（默认）。抓得住最主要的失分项「无依据引用」，且零成本
#   rule_first  规则先跑，报错时再叫模型复核一遍（更严，但每条多花 30 秒）
#   llm         每次都叫模型（最慢，只在排查语义问题时用）
VERIFY_MODE = os.getenv("VERIFY_MODE", "rule").strip().lower()

# ---- 检索 ----
# bm25     : 零依赖，开箱即跑（基础版默认）
# embedding: 需要本地 Qwen3-Embedding 模型，后续版本接入
RETRIEVAL_BACKEND = os.getenv("RETRIEVAL_BACKEND", "bm25")
TOP_K = int(os.getenv("RETRIEVAL_TOP_K", "6"))

# ---- Agent 预算（写死，不交给模型判断）----
MAX_STEPS = int(os.getenv("MAX_AGENT_STEPS", "8"))
MAX_SECONDS = int(os.getenv("MAX_AGENT_SECONDS", "60"))
MAX_TOKENS = int(os.getenv("MAX_AGENT_TOKENS", "60000"))

# ---- 存储 ----
# 允许用环境变量指向另一个库，是为了测试能开一个临时库跑完整链路，
# 而不是往真实库里塞一堆 test_ 开头的账号。
DB_PATH = Path(os.getenv("LAW_DB_PATH") or (DATA_DIR / "law.db"))
# 正式语料优先：scripts/import_law_text.py 产出的 statutes.jsonl 存在就用它，
# 否则退回 17 条示例语料。这样导入前后都不用改配置，也不会因为忘了切而跑空库。
_FULL_CORPUS = DATA_DIR / "statutes.jsonl"
_SEED_CORPUS = DATA_DIR / "seed_statutes.jsonl"
# LAW_CORPUS_PATH 是为了两件事：测试要在**不动真实语料**的前提下模拟「正式语料
# 缺失」（否则得把 1.4MB 的 statutes.jsonl 挪走再挪回来，中途被打断就没了），
# 以及有人把语料放在别的盘。平时不设，行为与以前完全一致。
_CORPUS_ENV = os.getenv("LAW_CORPUS_PATH")
if _CORPUS_ENV:
    CORPUS_PATH = Path(_CORPUS_ENV)
else:
    CORPUS_PATH = _FULL_CORPUS if _FULL_CORPUS.exists() else _SEED_CORPUS
# 「当前用的是不是那 17 条示例」。判断放在配置层，是为了让路由和测试都读同一个
# 结论——按路径比而不是按名字比：名字能改，路径是实际加载的那个文件。
CORPUS_IS_SEED = CORPUS_PATH == _SEED_CORPUS

# ---- 可插拔法律内核（v119）----
# 接入内核（消费者权益/食品安全）的精选条文。和正式语料**分文件**是刻意的：
# 主索引 `LawIndex(CORPUS_PATH)` 一个字节都不变，劳动法内核的 BM25 / IDF
# 因此完全不受新增文档影响（那些第 1 名掉到 97 名的历史问题不会再回来）。
# 只有切换内核时才额外挂载这个文件，并按 law_short 过滤后单独建索引。
# v120：消保法 63 条、食安法 154 条已从 gov.cn 换成**官方全文**（抓取时做条号
# 1→N 连续性校验）；道交法仍只有 4 条关键条文摘录。哪个是全文、哪个是摘录，
# note 与交付说明里都必须说实话——把 4 条摘录说成全文是新的假话。
DOMAIN_CORPUS_PATH = DATA_DIR / "domain_statutes.jsonl"

# ---- 运行模式提示 ----
MODE_NOTE = "LLM 在线" if LLM_ENABLED else "LLM 未配置（回放模式）"

# ---- 检索领域加权 ----
# 民法典 1260 条 + 合同法 428 条属于通用民事法，一旦入语料就把劳动法条文
# 从检索前列挤出去（实测：问「自愿放弃社保的承诺书有效吗」，劳动合同法第 26 条
# 从第 1 名掉到第 97 名，被民法典的「合同无效」类条文顶掉）。
# 给泛民事法乘一个小于 1 的系数。实测 0.55 时 top-10 命中率 73.2% → 78.0%，
# 同时「合同法第114条还有效吗」这类版本题仍稳定在前 5 名。
# 调到 0.4 就开始伤版本题（v01 掉到第 18 名），所以 0.55 是拐点。
GENERIC_LAWS = {"民法典", "合同法"}
GENERIC_BOOST = float(os.getenv("GENERIC_BOOST", "0.55"))

# ---- 认证与账号 ----
# 为什么自己写而不引 bcrypt / passlib / PyJWT：
#   这个项目从第一天起就是「只装 pydantic 就能跑」，连 openai SDK 都因为
#   jiter 的 .pyd 被 AppLocker 拦而换成了标准库 urllib。认证这块需要的
#   三样东西标准库都有——hashlib.pbkdf2_hmac 做慢哈希、secrets 生成令牌、
#   hmac.compare_digest 做恒定时间比较，没有任何理由为它们多装一个包。
#
# 密码哈希迭代次数：OWASP 2023 对 PBKDF2-HMAC-SHA256 的建议是 60 万次，
# 但那是在服务端 CPU 密集场景。本机实测 26 万次约 90ms，60 万次约 210ms；
# 登录是低频操作，210ms 完全可接受，所以这里默认取 60 万，不留余量。
PBKDF2_ITER = int(os.getenv("PBKDF2_ITER", "600000"))

# 令牌有效期。法律咨询不是银行，7 天太久（借出去的电脑），2 小时太烦
# （问到一半要重登）。取 3 天，且每次请求不滑动续期——续期会让「管理员
# 封号」的生效时间取决于用户活跃度，反而不好解释。
TOKEN_TTL_HOURS = int(os.getenv("TOKEN_TTL_HOURS", "72"))
TOKEN_NAME = os.getenv("TOKEN_NAME", "law_token")
# CSRF 双提交用的 cookie。**故意不 HttpOnly**：前端必须能读出来
# 塞进请求头，否则双提交无从比对。安全性来自「跨站读不到同源 cookie」，
# 不来自这个 cookie 保密。
CSRF_NAME = os.getenv("CSRF_NAME", "law_csrf")

# 开放注册。默认开：交付/演示时希望别人能自己进来试。
# 关掉之后只能由管理员在后台建号，这两个开关不冲突——ALLOW_REGISTER 只
# 控制 /api/auth/register 这个公开端点。
ALLOW_REGISTER = os.getenv("ALLOW_REGISTER", "1") not in ("0", "false", "False", "")

# 注册限流：同 IP 每小时最多几个号。开放注册必然招灌水，一张表灌满之后
# 管理员面板翻页都翻不动，所以宁可一开始就卡住。
REG_PER_IP_HOUR = int(os.getenv("REG_PER_IP_HOUR", "3"))

# 登录失败锁定。两个维度分开：同账号连错 5 次锁 15 分钟（防定向爆破），
# 同 IP 10 分钟错 8 次锁 10 分钟（防撞库）。任一触发就拒。
LOGIN_FAIL_ACCOUNT = int(os.getenv("LOGIN_FAIL_ACCOUNT", "5"))
LOGIN_LOCK_MIN = int(os.getenv("LOGIN_LOCK_MIN", "15"))
LOGIN_FAIL_IP = int(os.getenv("LOGIN_FAIL_IP", "8"))
LOGIN_IP_WINDOW_MIN = int(os.getenv("LOGIN_IP_WINDOW_MIN", "10"))

# 密码长度下限。上限也要有——PBKDF2 对超长输入不是免费的，
# 有人拿 1MB 字符串打登录接口就是一次低成本 DoS。
PWD_MIN = int(os.getenv("PWD_MIN", "8"))
PWD_MAX = int(os.getenv("PWD_MAX", "128"))

# 首个管理员的账号。首次启动时若库里一个 admin 都没有，用它建号，
# 密码取环境变量；没配就随机生成并打印到控制台（只打印一次）。
# 不写死默认密码：写死了就等于交付物里带一个后门。
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "").strip()

# Cookie 是否只在 HTTPS 下发送。本地 http://127.0.0.1 必须为 0，
# 否则浏览器直接不保存 cookie，表现为「登录成功但立刻又退出登录」——
# 这个坑很难从现象反推原因。部署到 https 时再改成 1。
SECURE_COOKIE = os.getenv("SECURE_COOKIE", "0") not in ("0", "false", "False", "")

# 是否信任 X-Forwarded-For。默认不信任：本地直连时这个头可以随便伪造，
# 信了它等于把登录限流拱手让出来——攻击者每次换一个 XFF 就绕过了。
# 只有明确部署在 nginx/网关后面时才打开。
TRUST_PROXY = os.getenv("TRUST_PROXY", "0") not in ("0", "false", "False", "")
