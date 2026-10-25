"""中文权威信源库：给检索到的来源分层打分，并生成定向/排除检索参数。

为什么需要它：
  元宝搜索标准版**不返回** authority_level（权威度得分）字段——那是旗舰版独有的。
  所以只能自己按域名给来源做背景分层。这个标签只描述来源背景，不等于页面内容或当前说法已被证实。

分层思路借鉴开源项目 source_tier_classifier.py（alirezarezvani/claude-skills）：
  https://github.com/alirezarezvani/claude-skills/blob/main/research/dossier/skills/dossier/scripts/source_tier_classifier.py
  它把 URL 分为 primary（政府/学术/法院）、secondary（主流媒体）、
  tertiary（博客/论坛/自媒体）三层，纯域名模式匹配、不调大模型。
  但它的域名表全是美国源（FDA/SEC/NYT），对中国信源无用，所以这里重建中文表。

官方 Site 参数限制（必须遵守，否则参数无效）：
  - 指定网址搜索：每次只能指定**一个**域名，如 Site=gov.cn
  - 指定网址过滤：每次最多排除**五个**域名，如 Site=exclude:a.com|b.com
  见 https://cloud.tencent.com/document/product/1806/121811

"领域 → 权威信源"映射的设计依据：
  用户的问题是"千行百业"的——社会的、经济的、娱乐的、法律的、医学的都有，
  而且可能是全球性的。同一个搜索引擎，问医学问题和问法律问题该找的信源完全不同。
  这里按领域给出：① 该信什么源（展示用，让用户看到调查员的"渠道清单"）
  ② 该从哪几个角度查（喂给拆解提示词，让子问题更准）
  ③ 用于补搜索的定向域名；历史实测站点与新增候选的验证状态见各条注释

  参考的公开信源清单（均为英文源，域名表以美国/国际站点为主，故只借鉴分类方式）：
   - alirezarezvani/claude-skills 的 source_tier_classifier.py（分层思路）
   - vibhasdutta/loop-engineer 的 knowledge-sources/（33 个领域分类，含 21 政府/22 财经/23 法律/24 医学）
     https://github.com/vibhasdutta/loop-engineer/tree/main/platforms/antigravity/agents/knowledge-sources
   - mahirkurt/cureoprivate 的 evidentia connector-registry.md（医学信源登记表）
     https://github.com/mahirkurt/cureoprivate/blob/main/plugins/evidentia/skills/medical-research/references/connector-registry.md
   - Mr-robot0-0/Legendary_OSINT 的 gov-legal.md（政府与法律记录）
     https://github.com/Mr-robot0-0/Legendary_OSINT/blob/main/docs/gov-legal.md
"""

from urllib.parse import urlsplit

# —— 一级来源背景：政府、主管部门、官方辟谣平台、央媒、国际权威机构与顶级学术期刊 ——
# 匹配域名仅说明来源背景；不代表具体页面内容或当前说法已经核实。
PRIMARY_EXACT = {
    # 中央政府与主管部门
    "gov.cn", "nhc.gov.cn",                        # 国家卫健委
    "ndrc.gov.cn", "mofcom.gov.cn", "samr.gov.cn",         # 发改委/商务部/市场监管
    "nmpa.gov.cn", "cdc.gov.cn", "chinacdc.cn",        # 药监/疾控
    "moa.gov.cn", "stats.gov.cn", "miit.gov.cn",
    "cma.gov.cn",                                  # 中国气象局（实测高频）
    # 官方辟谣平台
    "piyao.org.cn",                                # 中国互联网联合辟谣平台
    "piyao.kepuchina.cn",                          # 科普中国辟谣
    "piyao.wfswwxb.com",                           # 已登记的地方辟谣平台域名
    "chinafi.cn",
    # 央媒（含各自客户端/子站，实测高频）
    "news.cn", "xinhuanet.com", "news.xinhuanet.com",       # 新华社/新华网
    "people.com.cn", "paper.people.com.cn",                # 人民网/人民日报
    "peopleapp.com",                               # 人民日报客户端（实测 5 次/8 份日志）
    "cctv.com", "cctv.cn", "tv.cctv.com", "news.cctv.com",
    "cntv.cn", "news.cntv.cn",                     # 央视网
    "chinadaily.com.cn", "china.com.cn",           # 中国日报/中国网
    # 科普与权威科普机构
    "kepuchina.cn",                                # 科普中国（中国科协）
    "cae.cn", "cas.cn",                            # 工程院/科学院
    # 境外官方卫生机构
    "chp.gov.hk",                                  # 香港卫生防护中心
    # 浏览器产品官方文档：只表示官方来源背景，不证明页面内容或当前说法。
    "support.mozilla.org", "developer.mozilla.org", "support.google.com",
    # 盲文/无障碍与科学机构：只表示机构来源背景，不证明页面内容或当前说法。
    "rnib.org.uk", "loc.gov", "noaa.gov",
    # 国际权威机构与顶级期刊
    "who.int",
    "nature.com", "science.org", "nejm.org",
    "thelancet.com", "jamanetwork.com", "pnas.org",
    "bmj.com", "cell.com", "plos.org",
    "pubmed.ncbi.nlm.nih.gov",
}

# 一级信源后缀：只保留政府/军事等明确的官方后缀。
# .edu.cn 不在这里：高校域名下可能是个人主页或用户内容，不能整域自动提权。
PRIMARY_SUFFIX = [
    ".gov.cn",   # 中国政府网站
    ".gov",      # 美国等政府网站
    ".mil.cn",
]

# 用户问答路径：同一域名下，这些路径是患者/网友提问，不是编辑部内容。
# 这条规则同时适用于一级和二级来源；域名背景不能覆盖页面路径信号。
UGC_PATH_HINTS = ("/question/", "/questions/", "/ask/", "/qa/", "/bbs/", "/thread/",
                  "/forum/", "/forums/", "/community/", "/communities/",
                  "/tieba/", "/post/", "/t/")

# —— 二级信源：省级党媒、地方门户、专业健康媒体、知名行业媒体 ——
SECONDARY_EXACT = {
    "stdaily.com",            # 科技日报
    "39.net",                 # 39健康网
    "dxy.com",                # 丁香园
    "gmw.cn",                 # 光明网
    "ce.cn",                  # 中国经济网
    "cnr.cn",                 # 央广网
    "yicai.com",              # 第一财经
    "caixin.com",             # 财新
    "thepaper.cn",            # 澎湃新闻
    "jfdaily.com",            # 解放日报
    "bjnews.com.cn",          # 新京报
    "nfnews.com",             # 南方+
    "chinanews.com.cn", "chinanews.com",   # 中国新闻社（.com 是实测高频主站）
    "cnwomen.com.cn",         # 中国妇女网（全国妇联）
    "huanqiu.com",            # 环球网
    "youth.cn",               # 中国青年网（共青团中央）
    "dayi.org.cn",            # 中国医药信息查询平台（国家药监局信息中心）
    "xuexi.cn",               # 学习强国
    "health.cn",              # 健康报网
    "fashion.people.com.cn",
    # 一手机构与运营方（对自家规则/产品/数据的官方说明；不证明第三方说法）
    "networkrail.co.uk",                 # 英国铁路网公司
    "networkrailmediacentre.co.uk",      # 其新闻中心
    "alstom.com",                        # 阿尔斯通（轨道供电系统制造商）
    "cfs.gov.hk",                        # 香港食物安全中心
    "info.chineseshipping.com.cn",       # 中国航务周刊
    "dhl.com",                           # DHL 官方（自家运输规则）
    "emiratesline.com",                  # Emirates Shipping Line 官方
    "jsnews.jschina.com.cn",             # 中国江苏网（新华报业）
}

# —— 三级信源：聚合平台、自媒体、UGC 社区、健康营销站（内容质量参差，需谨慎）——
TERTIARY_EXACT = {
    "toutiao.com",                       # 今日头条（含自媒体号）
    "baijiahao.baidu.com",               # 百家号
    "sohu.com",                          # 搜狐号
    "163.com", "dy.163.com",             # 网易号
    "zhuanlan.zhihu.com", "zhihu.com",   # 知乎
    "weibo.com",                         # 微博
    "mp.weixin.qq.com",                  # 微信公众号
    "xiaohongshu.com",                   # 小红书
    "bilibili.com",                      # B站
    "quanmin.baidu.com",                 # 百度动态
    "qq.com", "new.qq.com",              # 腾讯新闻
    "sina.cn", "sina.com.cn",            # 新浪网
    "wikipedia.org", "wikimedia.org",    # 维基百科（可参考，非权威）
    # 健康/医疗营销站：实测高频（民福康 8 次、博禾医生 8 次、好在医生、复禾健康）
    # 这些站内容看着像医学科普，实为引流软文，不能与疾控中心同等看待
    "mfk.com",                           # 民福康
    "bohe.cn",                           # 博禾医生
    "haozai120.com",                     # 好在医生
    "fh21.com.cn",                       # 复禾健康
    "familydoctor.com.cn",               # 家庭医生在线
    "jianke.com",                        # 健客网
    "xywy.com",                          # 寻医问药
    # 百科与博客：内容可被用户编辑或属个人经验，不能当独立可靠来源
    "baike.baidu.com", "baike.sogou.com",
    "csdn.net",
    # 百行业实测中出现的低质/内容农场型站点（SEO 采集、营销站、聚合站）
    "bkzx.cn", "guofenmi.net", "lvbad.com", "lwjxa.com",
    "ymt.com", "jzx7788.com", "dutyglobal.com",
    "checkweather.io", "britishtravelguides.com", "iwaspoisoned.com",
    "umu.cn",
}

# —— 内容农场黑名单：实测抓到过的低质站点，检索时应排除 ——
FARM_DOMAINS = {
    "sangqia.com",      # 实测：搜"阿斯巴甜 致癌 研究"时混进来的内容农场
    "hackcn.de",        # 实测：同上，镜像/采集站
    "bkweek.com",       # 实测：搜"无糖可乐"时混入
}

TIER_PRIMARY = "primary"
TIER_SECONDARY = "secondary"
TIER_TERTIARY = "tertiary"
TIER_UNKNOWN = "unknown"
TIER_FARM = "farm"

TIER_LABEL = {
    TIER_PRIMARY: "权威来源背景",
    TIER_SECONDARY: "较可信来源背景",
    TIER_TERTIARY: "需谨慎来源背景",
    TIER_UNKNOWN: "未知来源背景",
    TIER_FARM: "内容农场域名（域名背景）",
}

TIER_SCORE = {
    TIER_PRIMARY: 90,
    TIER_SECONDARY: 70,
    TIER_TERTIARY: 30,
    TIER_UNKNOWN: 40,
    TIER_FARM: 0,
}


def _url_parts(url: str):
    value = (url or "").strip()
    if value and "://" not in value and not value.startswith("//"):
        value = "//" + value
    try:
        return urlsplit(value)
    except ValueError:
        return urlsplit("")


def _host_of(url: str) -> str:
    host = (_url_parts(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def classify(url: str, site: str = "") -> dict:
    """按来源域名背景分层；域名标签不证明页面内容或当前说法。

    返回 {"tier":..., "score":0-100, "label":..., "reason":...}
    只解析 URL，不打开网页。
    """
    host = _host_of(url)
    path = _url_parts(url).path.lower()

    # 内容农场按完整域名标签边界匹配，不从站点显示名推定。
    if any(host == farm or host.endswith("." + farm) for farm in FARM_DOMAINS):
        return {"tier": TIER_FARM, "score": TIER_SCORE[TIER_FARM],
                "label": TIER_LABEL[TIER_FARM], "reason": f"{host} 在内容农场域名名单里"}

    tier = None
    reason = ""

    # 一级：明确登记域名，按域名边界匹配
    for domain in PRIMARY_EXACT:
        if host == domain or host.endswith("." + domain):
            tier = TIER_PRIMARY
            reason = f"{host} 与已登记的机构/官方来源域名匹配"
            break

    # 一级：政府与军事域名后缀（不含高校通用域名）
    if tier is None:
        for suffix in PRIMARY_SUFFIX:
            if host.endswith(suffix):
                tier = TIER_PRIMARY
                reason = f"{host} 以 {suffix} 结尾"
                break

    # 二级：明确登记域名，按域名边界匹配
    if tier is None:
        for domain in SECONDARY_EXACT:
            if host == domain or host.endswith("." + domain):
                tier = TIER_SECONDARY
                reason = f"{host} 与已登记的专业媒体/机构域名匹配"
                break

    # 三级：聚合平台/自媒体/UGC
    if tier is None:
        for domain in TERTIARY_EXACT:
            if host == domain or host.endswith("." + domain):
                tier = TIER_TERTIARY
                reason = f"{host} 与已登记的聚合平台/UGC 域名匹配"
                break

    # UGC 路径信号优先于一级/二级域名背景。
    if tier in (TIER_PRIMARY, TIER_SECONDARY):
        for hint in UGC_PATH_HINTS:
            if hint in path.rstrip("/") + "/":
                tier = TIER_TERTIARY
                reason = f"{host} 的 {hint} 路径属于 UGC，域名背景不代表该页面内容"
                break

    if tier is None:
        tier = TIER_UNKNOWN
        reason = f"{host} 不在信源库里，来源背景未知"

    return {"tier": tier, "score": TIER_SCORE[tier],
            "label": TIER_LABEL[tier], "reason": reason}


def build_exclude_filter() -> str:
    """生成 Site 排除参数，把已知内容农场挡在检索结果外。

    官方限制：每次最多排除 5 个域名，所以只取黑名单前 5 个。
    """
    farms = sorted(FARM_DOMAINS)[:5]
    return "exclude:" + "|".join(farms) if farms else ""


def tier_summary(sources) -> dict:
    """统计一批来源的层级分布，供报告展示和置信度参考。"""
    counts = {}
    for s in sources:
        info = classify(getattr(s, "url", ""), getattr(s, "site", ""))
        counts[info["tier"]] = counts.get(info["tier"], 0) + 1
    return counts


# ===== 领域 → 权威信源映射 =====
#
# 每条记录四个字段：
#   label     领域名，给人看的
#   keywords  关键词表，用来判断用户的问题属于哪个领域（中文按子串匹配）
#   angles    该领域应当分别去查的几个角度，喂给"问题拆解"提示词
#   cn        中国信源（展示用）
#   global_   国际信源（展示用）
#   backfill  定向检索候选域名，按优先级排序；列入不保证能召回相关内容
#
# 历史站点曾逐个拿真实问题打元宝搜索验证；新增候选的未实测状态见条目注释。
# 历史实测 2026-10-24：who.int / gov.cn / nhc.gov.cn / nmpa.gov.cn / stats.gov.cn /
# court.gov.cn / ce.cn / health.cn / cas.cn 在当时的问题集下均返回 0 条，
# 所以它们只进"展示清单"，不进"补齐清单"；该结果不代表所有问题的召回能力。
# 所有机构清单和域名标签仅表示来源背景，不能据此证实当前说法。

DOMAIN_SOURCE_MAP = {
    "medical": {
        "label": "医学与健康",
        "keywords": [
            "医", "药", "病", "症", "癌", "肿瘤", "疫苗", "接种", "手术", "治疗",
            "临床", "医院", "医生", "健康", "营养", "饮食", "喝水", "睡眠", "失眠",
            "减肥", "肥胖", "维生素", "抗生素", "病毒", "细菌", "感染", "传染",
            "疫情", "体检", "血压", "血糖", "血脂", "胆固醇", "心脏", "肝脏",
            "骨质疏松", "近视", "牙齿", "皮肤", "过敏", "抑郁", "焦虑", "孕妇",
            "婴儿", "寿命", "致癌", "排毒", "养生", "偏方", "中医", "保健",
            "补品", "添加剂", "防腐剂", "隔夜", "副作用", "处方", "确诊",
            "慢性", "免疫", "抗体", "发育", "致癌物", "发炎", "糖尿",
        ],
        "angles": [
            "国家卫健委、药监局、疾控中心等主管部门的官方口径",
            "临床研究、诊疗指南、系统评价的结论怎么说",
            "权威科普平台与官方辟谣平台怎么解释",
        ],
        "cn": [
            "nhc.gov.cn 国家卫生健康委",
            "nmpa.gov.cn 国家药品监督管理局",
            "chinacdc.cn 中国疾病预防控制中心",
            "piyao.org.cn 中国互联网联合辟谣平台",
            "kepuchina.cn 科普中国",
            "dxy.com 丁香园（专业医学科普）",
        ],
        "global_": [
            "who.int 世界卫生组织（WHO）",
            "pubmed.ncbi.nlm.nih.gov PubMed 生物医学文献库",
            "cochranelibrary.com 科克伦系统评价库",
            "fda.gov 美国食品药品监督管理局",
            "ema.europa.eu 欧洲药品管理局",
            "nejm.org / thelancet.com《新英格兰医学杂志》《柳叶刀》",
        ],
        "backfill": ["kepuchina.cn", "piyao.org.cn", "pubmed.ncbi.nlm.nih.gov", "dxy.com"],
    },
    "legal": {
        "label": "法律与权益",
        "keywords": [
            "法律", "法规", "条例", "违法", "合法", "权利", "义务", "合同",
            "条款", "起诉", "诉讼", "法院", "法官", "律师", "判决", "判例",
            "仲裁", "赔偿", "罚款", "处罚", "侵权", "诽谤", "隐私", "版权",
            "知识产权", "专利", "商标", "继承", "遗嘱", "婚姻", "离婚", "抚养",
            "赡养", "劳动", "工伤", "辞退", "裁员", "消费者权益", "退货", "退款",
            "诈骗", "盗窃", "刑事责任", "民法典", "刑法", "官司", "胜诉", "上诉",
            "取证", "举证", "拘留", "量刑", "无罪", "有罪",
        ],
        "angles": [
            "现行法律条文与最高法司法解释怎么规定",
            "官方普法平台与政法媒体的解读",
            "公开判决案例里同类情况怎么判",
        ],
        "cn": [
            "gov.cn 中国政府网（法律法规库）",
            "court.gov.cn 最高人民法院",
            "people.com.cn 人民网（政法频道）",
            "news.cn 新华网（法治频道）",
            "12348.cn 中国法律服务网",
        ],
        "global_": [
            "courtlistener.com 美国判例全文库",
            "law.cornell.edu 康奈尔大学法律信息研究所",
            "eur-lex.europa.eu 欧盟法律数据库",
            "bailii.org 英国判例库",
            "wipo.int 世界知识产权组织",
        ],
        "backfill": ["people.com.cn", "news.cn"],
    },
    "finance": {
        "label": "经济与财经",
        "keywords": [
            "经济", "财经", "金融", "股票", "股价", "股市", "基金", "债券",
            "利率", "汇率", "通胀", "通货膨胀", "gdp", "cpi", "pmi", "央行",
            "降息", "加息", "关税", "贸易", "出口", "进口", "外贸", "财报",
            "营收", "利润", "亏损", "上市", "ipo", "退市", "市值", "估值",
            "投资", "理财", "保险", "房贷", "首付", "贷款", "存款", "比特币",
            "加密货币", "涨价", "降价", "油价", "金价", "房价", "楼市", "税收",
            "减税", "退税", "补贴", "工资", "薪资", "就业", "失业", "招聘",
            "破产", "债务", "收益率", "分红", "股东", "收入", "物价", "成本",
        ],
        "angles": [
            "国家统计局、央行、财政部等发布的官方数据",
            "主流财经媒体与官方通讯社的报道口径",
            "国际组织（世界银行/IMF/世贸组织）的统计口径",
        ],
        "cn": [
            "stats.gov.cn 国家统计局",
            "pbc.gov.cn 中国人民银行",
            "mof.gov.cn 财政部",
            "mofcom.gov.cn 商务部",
            "news.cn 新华网财经",
            "yicai.com 第一财经",
            "ce.cn 中国经济网",
        ],
        "global_": [
            "worldbank.org 世界银行",
            "imf.org 国际货币基金组织",
            "wto.org 世界贸易组织",
            "sec.gov/edgar 美国证监会上市公司数据库",
            "fred.stlouisfed.org 美联储经济数据库",
            "oecd.org 经合组织",
        ],
        "backfill": ["news.cn", "people.com.cn"],
    },
    "society": {
        "label": "社会与政务",
        "keywords": [
            "社会", "政务", "政策", "政府", "国家", "中央", "国务院", "部委",
            "公告", "通知", "发布", "人口", "出生率", "老龄化", "生育", "二胎",
            "三胎", "教育", "高考", "中考", "录取", "学费", "医保", "社保",
            "养老", "退休", "公积金", "保障房", "拆迁", "征地", "户籍", "户口",
            "居住证", "身份证", "护照", "签证", "移民", "难民", "灾害", "地震",
            "洪水", "台风", "事故", "救援", "慈善", "捐款", "社区", "农村",
            "农业", "粮食", "耕地", "环保", "污染", "碳排放", "统计", "普查",
        ],
        "angles": [
            "国务院部委与地方政府的官方发布口径",
            "官方通讯社与党媒的报道",
            "官方统计与普查数据",
        ],
        "cn": [
            "gov.cn 中国政府网",
            "stats.gov.cn 国家统计局",
            "news.cn 新华网",
            "people.com.cn 人民网",
            "chinadaily.com.cn 中国日报",
        ],
        "global_": [
            "un.org 联合国",
            "who.int 世界卫生组织",
            "unhcr.org 联合国难民署",
            "data.un.org 联合国数据门户",
            "worldbank.org 世界银行",
        ],
        "backfill": ["news.cn", "people.com.cn"],
    },
    "science": {
        "label": "科学与学术",
        "keywords": [
            "科学", "研究", "论文", "实验", "数据", "发现", "证明", "证伪",
            "理论", "假设", "学术", "期刊", "nature", "science", "院士",
            "实验室", "观测", "天文", "宇宙", "太空", "火箭", "卫星", "物理",
            "化学", "生物", "进化", "基因", "dna", "量子", "相对论", "数学",
            "猜想", "算法", "人工智能", "大模型", "神经网络", "气候变化",
        ],
        "angles": [
            "同行评议期刊与预印本平台的原始论文",
            "权威科研机构（中科院/各国国立实验室）的发布",
            "国际科学组织的共识声明",
        ],
        "cn": [
            "cas.cn 中国科学院",
            "cae.cn 中国工程院",
            "news.cn 新华网科技",
            "stdaily.com 科技日报",
        ],
        "global_": [
            "pubmed.ncbi.nlm.nih.gov PubMed",
            "nature.com / science.org《自然》《科学》",
            "noaa.gov 美国国家海洋和大气管理局（来源背景，不证明当前说法）",
            "arxiv.org 预印本平台",
            "nasa.gov 美国宇航局",
            "esa.int 欧洲空间局",
            "ipcc.ch 联合国政府间气候变化专门委员会",
        ],
        "backfill": ["pubmed.ncbi.nlm.nih.gov", "news.cn"],
    },
    "biology": {
        "label": "生物与自然",
        "keywords": [
            # 动物
            "动物", "熊猫", "老虎", "狮子", "大象", "鲸鱼", "海豚", "章鱼", "蜘蛛",
            "昆虫", "蜜蜂", "蚂蚁", "蝴蝶", "蜻蜓", "萤火虫", "蛇", "鳄鱼", "恐龙",
            "鸟类", "候鸟", "企鹅", "猫", "狗", "宠物", "仓鼠", "兔子", "马", "牛",
            "猪", "鸡", "鸭", "鱼", "鲨鱼", "珊瑚", "水母", "蝙蝠", "青蛙", "乌龟",
            # 植物
            "植物", "花", "树", "叶子", "草", "仙人掌", "向日葵", "含羞草", "银杏",
            "水杉", "红豆杉", "蘑菇", "真菌", "苔藓", "蕨类", "竹子", "水稻", "小麦",
            "玉米", "大豆", "咖啡", "茶", "多肉", "盆栽", "光合作用", "授粉", "种子",
            # 自然与生态
            "自然", "生态", "环境", "物种", "灭绝", "保护", "野生动物", "栖息地",
            "演化", "进化论", "达尔文", "分类", "属", "科", "纲", "寄生", "共生",
            "食物链", "迁徙", "冬眠", "繁殖", "受精", "细胞", "微生物", "细菌",
            "病毒", "疫苗", "抗生素", "过敏原", "毒", "无毒", "药用", "濒危",
        ],
        "angles": [
            "权威百科与专业数据库（中国大百科全书/物种志/WoRMS 等）的条目",
            "科研机构与高校的科普发布（中科院/国家自然科学基金委/各国博物馆）",
            "国际保护组织与专业学会的公开资料（IUCN/WWF 等）",
        ],
        "cn": [
            "cas.cn 中国科学院",
            "stdaily.com 科技日报",
            "kepuchina.cn 科普中国",
            "piyao.org.cn 中国互联网联合辟谣平台",
            "chinajournal.net.cn 中国知网百科",
        ],
        "global_": [
            "pubmed.ncbi.nlm.nih.gov PubMed",
            "nature.com《自然》",
            "sciencedirect.com ScienceDirect",
            "iucnredlist.org 世界自然保护联盟红色名录",
            "britannica.com 大英百科",
            "nationalgeographic.com 国家地理",
        ],
        "backfill": ["kepuchina.cn", "piyao.org.cn", "news.cn"],
    },
    "entertainment": {
        "label": "娱乐与文化",
        "keywords": [
            "电影", "票房", "电视剧", "综艺", "明星", "演员", "歌手", "演唱会",
            "专辑", "歌曲", "榜单", "收视率", "网红", "主播", "直播", "带货",
            "选秀", "偶像", "男团", "女团", "导演", "编剧", "影帝", "影后",
            "奖项", "电影节", "动漫", "二次元", "手游", "电竞", "番剧", "追星",
            "粉丝", "八卦", "绯闻", "上映", "预告片", "路演", "综艺",
        ],
        "angles": [
            "官方票房/收视统计平台的数据",
            "主流媒体的文娱报道",
            "出品方、院线、平台的官方公告",
        ],
        "cn": [
            "news.cn 新华网文娱",
            "people.com.cn 人民网文娱",
            "cctv.com 央视网",
            "piaofang.maoyan.com 猫眼专业版票房数据",
        ],
        "global_": [
            "boxofficemojo.com 全球票房数据",
            "variety.com / hollywoodreporter.com《综艺》《好莱坞报道》",
            "imdb.com 影视数据库",
            "grammy.com / billboard.com 音乐奖项与榜单",
        ],
        "backfill": ["news.cn", "people.com.cn"],
    },
    "tech": {
        "label": "科技与数码",
        "keywords": [
            "手机", "电脑", "芯片", "处理器", "cpu", "gpu", "显卡", "内存",
            "硬盘", "屏幕", "电池", "续航", "充电", "数码", "评测", "跑分",
            "无痕", "浏览器", "https", "隐私模式",
            "5g", "6g", "wifi", "蓝牙", "北斗", "导航", "新能源", "电动车",
            "汽车", "自动驾驶", "机器人", "无人机", "vr", "ar", "区块链",
            "开源", "编程", "漏洞", "黑客", "固件", "操作系统", "系统更新",
        ],
        "angles": [
            "厂商与监管机构的官方发布",
            "主流科技媒体与官方通讯社的报道",
            "标准化组织与开源社区的文档",
        ],
        "cn": [
            "miit.gov.cn 工业和信息化部",
            "news.cn 新华网科技",
            "stdaily.com 科技日报",
            "cctv.com 央视网科技",
        ],
        "global_": [
            "ieee.org 电气电子工程师学会",
            "iso.org 国际标准化组织",
            "etsi.org 欧洲电信标准协会",
            "nist.gov 美国国家标准与技术研究院",
            "ietf.org 互联网工程任务组",
            "support.mozilla.org Mozilla 浏览器官方支持文档（来源背景，不证明当前说法）",
            "developer.mozilla.org Mozilla 开发者文档（来源背景，不证明当前说法）",
            "support.google.com Google 官方支持文档（来源背景，不证明当前说法）",
        ],
        # support.mozilla.org 是候选补齐站点；未承诺已实测召回效果。
        "backfill": ["support.mozilla.org", "news.cn", "people.com.cn"],
    },
    "accessibility": {
        "label": "无障碍与盲文",
        "keywords": [
            "盲文", "点字", "braille", "盲人", "视障", "视力障碍", "无障碍",
            "屏幕阅读器", "盲文显示器", "盲文书", "盲文点", "触觉阅读",
        ],
        "angles": [
            "盲文与视障服务机构的说明及资源",
            "图书馆的盲文馆藏、目录和无障碍服务资料",
            "区分机构介绍、标准说明与具体页面/个案内容",
        ],
        "cn": [],
        "global_": [
            "rnib.org.uk 英国皇家盲人协会（来源背景，不证明当前说法）",
            "loc.gov 美国国会图书馆盲文与有声书服务（来源背景，不证明当前说法）",
        ],
        # 作为盲文领域的候选定向站点；本地未验证检索召回。
        "backfill": ["rnib.org.uk", "loc.gov"],
    },
    "life": {
        "label": "食品与日常生活",
        "keywords": [
            "食品安全", "食物", "食品", "饮料", "保质期", "转基因", "有机",
            "绿色食品", "农药", "化肥", "激素", "瘦肉精", "地沟油", "染色",
            "硫磺", "甲醛", "塑化剂", "辐射", "电磁炉", "微波炉", "冰箱",
            "空调", "电热毯", "充电宝", "仙人掌", "绿萝", "植物", "洗碗",
            "洗洁精", "抹布", "筷子", "砧板", "塑料", "不粘锅", "净水器",
            "空气净化器", "加湿器", "基站", "wifi辐射", "手机辐射",
            "隔夜水", "千滚水", "纯净水", "矿泉水", "苏打水", "气泡水",
        ],
        "angles": [
            "官方辟谣平台对该说法的直接回应",
            "市场监管与食品安全主管部门的口径",
            "权威科普与专业媒体的实测/解释",
        ],
        "cn": [
            "piyao.org.cn 中国互联网联合辟谣平台",
            "samr.gov.cn 国家市场监督管理总局",
            "news.cn 新华网",
            "kepuchina.cn 科普中国",
        ],
        "global_": [
            "who.int 世界卫生组织",
            "fda.gov 美国食药监局",
            "efsa.europa.eu 欧洲食品安全局",
            "cfs.gov.hk 香港食物安全中心",
        ],
        "backfill": ["kepuchina.cn", "piyao.org.cn", "news.cn"],
    },
}

MAX_DETECT_DOMAINS = 3      # 一次最多识别几个领域
MAX_BACKFILL_DOMAINS = 2    # 一次调查最多对几个领域做定向补齐
MAX_BACKFILL_SITES = 2      # 每个领域最多试几个定向站点


def detect_domain(query: str, top_n: int = MAX_DETECT_DOMAINS) -> list:
    """判断一个问题属于哪些领域，按命中关键词数排序返回领域 key。

    返回例如 ["medical", "life"]。命中不了任何领域时返回空列表。
    """
    q = (query or "").lower()
    if not q:
        return []
    scored = []
    for key, d in DOMAIN_SOURCE_MAP.items():
        hits = sum(1 for k in d["keywords"] if k.lower() in q)
        if hits:
            scored.append((key, hits))
    scored.sort(key=lambda x: -x[1])
    return [k for k, _ in scored[:top_n]]


def domain_info(key: str) -> dict:
    """取一个领域的完整信息；key 不存在时返回空壳。"""
    return DOMAIN_SOURCE_MAP.get(key) or {
        "label": "通用", "angles": [], "cn": [], "global_": [], "backfill": [],
    }


def backfill_plan(domains) -> list:
    """生成定向补齐计划：[(领域key, 站点域名), ...]

    每个领域按 backfill 列表顺序取前 MAX_BACKFILL_SITES 个站点。
    """
    plan = []
    for key in (domains or [])[:MAX_BACKFILL_DOMAINS]:
        for site in domain_info(key)["backfill"][:MAX_BACKFILL_SITES]:
            plan.append((key, site))
    return plan


def domain_summary(domains) -> list:
    """给报告/接口用：把领域 key 列表转成 [{key,label,cn,global_}]。"""
    out = []
    for key in domains or []:
        d = domain_info(key)
        out.append({"key": key, "label": d["label"],
                    "cn": d["cn"], "global_": d["global_"]})
    return out
