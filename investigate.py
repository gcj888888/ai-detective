"""有界多轮元宝检索。先保留原始主张，再用中性角度查支持与反证。"""
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from difflib import SequenceMatcher
from urllib.parse import urlsplit, parse_qsl, urlencode
import authority
import config
import detective
import prompts
import wsa_search

MAX_SUB_QUERIES = 3
MAX_BACKFILL_TOTAL = 2
SIM_TITLE_THRESHOLD = .75
MAX_SYNTHESIS_INPUT_CHARS = 60000
class InvestigateError(Exception):
    pass


def _norm_url(u):
    try:
        p = urlsplit((u or "").strip())
        host = (p.hostname or "").lower().removeprefix("www.")
        if not host:
            return ""
        if p.port and p.port not in (80,443):
            host += ":"+str(p.port)
        qs = [(k,v) for k,v in parse_qsl(p.query,keep_blank_values=True) if not k.lower().startswith("utm_") and k.lower() not in {"gclid","fbclid","spm"}]
        return host+(p.path or "/")+("?"+urlencode(sorted(qs)) if qs else "")
    except ValueError:
        return (u or "").strip()


def decompose(query, *args, domains=None):
    """模板拆步骤消除一次串行模型等待；查询中不预置真假结论。"""
    tech = any(x in query.lower() for x in ("无痕","浏览器","https","软件","隐私模式"))
    if tech:
        angles = [(query + " 官方产品文档 限制 说明", "", "核对厂商对功能边界的直接说明"), (query + " 适用条件 例外", "", "查找适用条件与反例")]
    else:
        angles = [(query + " 权威机构 解释 适用条件", "", "核对主管机构或原始资料"), (query + " 研究 证据 例外", "acad" if any(d in ("medical","science","biology") for d in (domains or [])) else "", "查找支持、反证与条件")]
    return [{"query":query,"industry":"","reason":"原始主张不定向检索，避免路由漏检"}] + [{"query":q[:200],"industry":ind,"reason":reason} for q,ind,reason in angles]


def run_rounds(plan, query, domains=None, deadline=None):
    def one(sub):
        begin = time.perf_counter()
        error = None
        try:
            items, version = wsa_search.search(sub["query"],secret_id=config.TENCENT_SECRET_ID,secret_key=config.TENCENT_SECRET_KEY,mode=config.WSA_MODE,cnt=config.WSA_CNT,industry=sub.get("industry",""),site=sub.get("site") or authority.build_exclude_filter(),timeout=min(8,max(1,deadline-time.perf_counter())) if deadline else 8)
        except wsa_search.WsaSearchError as exc:
            items,version,error = [],"",str(exc)
        return {**sub,"results":items,"hits":len(items),"elapsed_seconds":round(time.perf_counter()-begin,3),"error":error},version
    with ThreadPoolExecutor(max_workers=3) as pool:
        outcomes = list(pool.map(one,plan[:3]))
    rounds = [r for r,_ in outcomes]
    version = next((v for _,v in outcomes if v),"")
    found = [s for r in rounds for s in r["results"]]
    attempts = 0
    if not any(authority.classify(s.url)["tier"]==authority.TIER_PRIMARY for s in found):
        for key,site in authority.backfill_plan(domains or []):
            if attempts>=MAX_BACKFILL_TOTAL or (deadline and deadline-time.perf_counter()<3):
                break
            attempts += 1
            rd,ver = one({"query":query,"industry":"","site":site,"domain":key,"kind":"backfill","reason":"补查候选机构，是否直接支持主张仍需核验"})
            rounds.append(rd)
            version = ver or version
            found.extend(rd["results"])
            if any(authority.classify(s.url)["tier"]==authority.TIER_PRIMARY for s in rd["results"]):
                break
    all_sources = []
    for i,rd in enumerate(rounds,1):
        rd["no"] = i
        rd.setdefault("kind","plan")
        for s in rd["results"]:
            s._round = i
            s._no = len(all_sources)+1
            all_sources.append(s)
    detective._tag_authority(all_sources)
    return rounds,all_sources,version


def find_duplicates(sources):
    parent = list(range(len(sources)))
    def root(i):
        while parent[i]!=i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    for i,a in enumerate(sources):
        for j,b in enumerate(sources[:i]):
            same_url = bool(_norm_url(a.url)) and _norm_url(a.url)==_norm_url(b.url)
            same_title = bool(a.title and b.title) and a.title.strip()==b.title.strip()
            same_host = urlsplit(a.url).hostname==urlsplit(b.url).hostname
            similar = bool(a.title and b.title and same_host) and SequenceMatcher(None,a.title,b.title).ratio()>=SIM_TITLE_THRESHOLD
            if same_url or same_title or similar:
                parent[root(i)] = root(j)
    groups = {}
    for i in range(len(sources)):
        groups.setdefault(root(i),[]).append(i)
    return [{"nos":[sources[i]._no for i in ids],"kind":"同一网页的多次收录（可能含不同证据文本，不计为独立来源）" if len({_norm_url(sources[i].url) for i in ids})==1 else "标题相同或相似的疑似转载（未确认同源）"} for ids in groups.values() if len(ids)>1]


def investigate(query, progress=None):
    if config.USE_MOCK:
        result = detective.check(query, progress=progress)
        result.update(mode="investigate", rounds=[], steps=[], duplicates=[], domains=[], domain_summary=[])
        return result
    if detective.exact_arithmetic(query) is None:
        unavailable = detective.llm_openai.preflight()
        if unavailable:
            report = detective.unavailable_report(query,unavailable)
            report.update(mode="investigate",rounds=[],steps=[],duplicates=[],domains=[],domain_summary=[])
            detective._dump_debug(report)
            return report
    start = time.perf_counter()
    domains = authority.detect_domain(query)
    detective.emit(progress,"plan","正在拆分原始主张、机构说明和条件/反证检索角度",start)
    plan = decompose(query,domains=domains)
    timings = {"plan":round(time.perf_counter()-start,3)}
    detective.emit(progress,"search","正在并行执行三路元宝搜索，保留原始主张的不定向检索",start)
    t = time.perf_counter()
    rounds,sources,version = run_rounds(plan,query,domains,deadline=start+16)
    timings["search"] = round(time.perf_counter()-t,3)
    t = time.perf_counter()
    dup = find_duplicates(sources)
    timings["duplicate_check"] = round(time.perf_counter()-t,3)
    # 按完整证据文本去重，不能用截断后的前缀代替；原始编号及全部来源保持不变。
    selected,seen,material_meta = [],{},{}
    truncated_nos = []
    for s in sources:
        url = _norm_url(s.url)
        text = prompts.evidence_text(s)
        key = (url,text)
        duplicate_of = seen.get(key) if url else None
        if duplicate_of is None:
            selected.append(s)
            if url:
                seen[key] = s._no
            if len(text)>3500:
                truncated_nos.append(s._no)
        material_meta[s._no] = {"included_in_synthesis":duplicate_of is None,"duplicate_of":duplicate_of,"evidence_truncated":len(text)>3500}
    limit_notes = []
    if truncated_nos:
        limit_notes.append(f"来源{truncated_nos}的证据文本超过现有每条3500字符限制，综合仅能看到前3500字符；尾部可能含反证，原始材料完整保留，调查未完成。")
    synthesis_prompt = prompts.build_user_prompt(query,selected)+"\n【疑似重复线索】"+prompts.build_dup_block(dup)
    synthesis_prompt += "\n【材料使用说明】同一规范URL的不同证据文本均保留，不能据此确认来源相互独立；同页多条摘要不增加独立来源数量。仅引用搜索材料中实际列出的原始编号，不得引用被合并项的编号。"
    if limit_notes:
        synthesis_prompt += "\n【材料限制】"+"；".join(limit_notes)
    input_chars = len(prompts.SYSTEM_PROMPT)+len(synthesis_prompt)
    blocked = input_chars>MAX_SYNTHESIS_INPUT_CHARS
    if blocked:
        limit_notes.append(f"综合总输入{input_chars}字符超过{MAX_SYNTHESIS_INPUT_CHARS}字符限制，已停止综合；未按顺序截取或删除资料，全部来源保留供核对。")
        for meta in material_meta.values():
            meta["included_in_synthesis"] = False

    def source_dict(s):
        return {**detective.source_dict(s),**material_meta[s._no]}

    message = f"取得{len(sources)}条资料，仅合并同网址且证据文本完全相同的重复项后{len(selected)}条材料（不代表独立来源数）"
    message += "；总输入超限，已停止综合，调查未完成" if blocked else "；正在核验支持与反对证据"
    if truncated_nos:
        message += f"；来源{truncated_nos}超过每条3500字符限制，尾部未纳入，调查未完成"
    detective.emit(progress,"synthesis",message,start,sources=[source_dict(s) for s in sources])
    t = time.perf_counter()
    report = detective.assess(query,[] if blocked else selected,prompt=synthesis_prompt,timeout=max(1,min(35,48-(t-start))),progress=progress)
    if limit_notes:
        limitation = "；".join(limit_notes)
        report["limitations"] = "；".join(filter(None,[report.get("limitations"),limitation]))
        report["evidence_audit"]["issues"].extend(limit_notes)
        detective.mark_incomplete(report,query,"调查未完成：综合材料受长度限制，无法排除未纳入部分的反证。",limitation)
    timings["synthesis"] = round(time.perf_counter()-t,3)
    timings["total"] = round(time.perf_counter()-start,3)
    errors = [rd["error"] for rd in rounds if rd.get("error")]
    if errors:
        detective.mark_incomplete(report,query,"调查未完成：部分检索失败，不能声称全部角度已核查。","部分检索失败")
    provider,_,model,_ = detective.llm_openai.resolve()
    result = {"query":query,"mode":"investigate",**report,"sources":[source_dict(s) for s in sources],"material_selection":{"input_chars":input_chars,"max_input_chars":MAX_SYNTHESIS_INPUT_CHARS,"blocked":blocked,"truncated_source_nos":truncated_nos},"rounds":[{k:v for k,v in rd.items() if k!='results'} | {"sources":[source_dict(s) for s in rd['results']]} for rd in rounds],"steps":[{"no":1,"name":"拆分检索角度","detail":"模板计划，无额外拆题模型等待","plan":plan},{"no":2,"name":"多轮元宝搜索","detail":f"{len(rounds)}轮；每轮耗时和失败状态已记录"},{"no":3,"name":"疑似重复检查","detail":prompts.build_dup_block(dup)},{"no":4,"name":"逐字引文与判定门槛核对","detail":report['reason']}],"duplicates":dup,"domains":domains,"domain_summary":authority.domain_summary(domains),"authority_summary":authority.tier_summary(sources),"searched_at":detective._now_str(),"wsa_version":version,"provider":provider,"model":model,"mock":config.USE_MOCK,"timings":timings,"runtime_version":detective.RUNTIME_VERSION,"search_errors":errors}
    detective._dump_debug(result)
    return result
