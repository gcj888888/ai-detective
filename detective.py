"""元宝搜索查证与三态证据门槛。引文核验是必要条件，不等于语义正确性保证。"""
import datetime
from fractions import Fraction
import json
import re
import time
from pathlib import Path
import authority
import config
import llm_openai
import prompts
from wsa_search import WsaSearchError, SearchResult, search as wsa_search

RUNTIME_VERSION = "2026-10-25-evidence-v2.9.4"
VERDICTS = VERDICT_KEYS = ("对", "错", "不确定")
_CST = datetime.timezone(datetime.timedelta(hours=8))
ALIASES = {"真":"对", "假":"错", "说不准":"不确定", "没查到可靠来源":"不确定", **{s:s for s in VERDICTS}}

class Card:
    def __init__(self, verdict, reason, detail, raw):
        self.verdict, self.reason, self.detail, self.raw = verdict, reason or "", detail or "", raw or ""


def extract_verdict(text):
    m = re.search(r'结论[:：]\s*(没查到可靠来源|不确定|说不准|对|错|真|假)(?:\s|$|[。．.,，])', text or "")
    if m:
        return ALIASES[m.group(1)]
    for line in (text or "").splitlines()[:3]:
        if line.strip().strip("。.") in ALIASES:
            return ALIASES[line.strip().strip("。.")]
    return None


def parse_card(text):
    try:
        d = json.loads(text)
        return Card(ALIASES.get(d.get("verdict")), d.get("reason"), d.get("detail"), text)
    except (ValueError, TypeError, AttributeError):
        reason = re.search(r"理由[:：]\s*(.+)", text or "")
        detail = re.search(r"详情[:：]\s*([\s\S]+)$", text or "")
        return Card(extract_verdict(text), reason.group(1) if reason else "", detail.group(1) if detail else "", text)


def _now_str():
    return datetime.datetime.now(_CST).strftime("%Y-%m-%d %H:%M:%S")


def _dump_debug(record):
    try:
        folder = Path(config.BASE_DIR) / "logs"
        folder.mkdir(exist_ok=True)
        name = datetime.datetime.now(_CST).strftime("%Y%m%d_%H%M%S_%f")
        (folder / f"check_{name}.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def _tag_authority(results):
    for i, r in enumerate(results, 1):
        r._no = getattr(r, "_no", i)
        r._authority = authority.classify(r.url, r.site)
        r._tier = r._authority["tier"]
    return authority.tier_summary(results)


def source_dict(r):
    return {**r.to_dict(), "no":r._no, "round":getattr(r,"_round",1), "tier":r._tier, "tier_label":r._authority["label"], "tier_score":r._authority["score"]}


def emit(progress, stage, message, started, **extra):
    if progress:
        progress({"type":"progress", "stage":stage, "message":message, "elapsed_seconds":round(time.perf_counter()-started,2), **extra})


def _evidence_guidance(query, verdict, status, evidence, issues, uncertainty_reason="", model_steps=None):
    """Describe auditable evidence strength and safe next checks; never emit probabilities."""
    if status == "incomplete":
        level, label = "未完成", "调查未完成，当前没有可用的证据支持等级。"
    elif any("冲突" in issue for issue in issues):
        level, label = "冲突", "存在未解决的证据冲突。"
    elif verdict == "不确定":
        level, label = "有限", "现有材料不足以支持明确结论。"
    else:
        matched = [e for e in evidence if e.get("quote_verified") and e.get("source_eligible") and e.get("direct")]
        level, label = ("较强", "至少有一条可核验的直接证据达到来源背景门槛；这不是准确率或概率。") if matched else ("有限", "没有通过门槛的直接证据。")

    unsafe = re.compile(r"自行(?:服用|停药|加量|实验|投资|购买)|做实验|尝试治疗|保证收益|直接起诉")
    steps = [s.strip() for s in (model_steps or []) if isinstance(s,str) and s.strip() and not unsafe.search(s)][:3]
    domains = authority.detect_domain(query)
    if not steps:
        if status == "incomplete":
            steps = ["检查本次搜索或模型服务状态后重试；本次服务失败不能作为对错证据。"]
        elif verdict == "不确定":
            steps = ["打开最相关来源核对正文原文、发布日期和适用范围。"]
            if domains:
                steps.append("优先查找" + authority.domain_summary(domains[:1])[0]["label"] + "对应的主管机构、原始研究或产品官方说明。")
            if any(word in (uncertainty_reason or "") for word in ("条件", "含糊", "对象", "地区", "日期")):
                steps.append("补充对象、时间、地区、剂量或适用人群等限定条件，再重新查证。")
            if any(d in ("medical", "legal", "finance") for d in domains):
                steps.append("涉及专业决策时，向合格专业人士核实；不要仅凭本页作诊断、法律或投资决定。")
        elif verdict in ("对", "错"):
            steps = ["打开已引用来源核对原文、日期和适用条件；重点检查主张中的绝对化措辞。"]
    return {"evidence_strength":level,"evidence_strength_note":label,"evidence_basis":[s for s in (model_steps or []) if isinstance(s,str) and s.strip()][:3],"next_steps":steps}


def exact_arithmetic(query):
    """只接受完整、无单位的四则运算等式；有理数计算，不执行输入代码。"""
    text = query.strip().rstrip("？?。！!").strip()
    if text.startswith("请问"):
        text = text[2:].strip()
    for suffix in ("是正确的吗", "是对的吗", "是否正确", "对不对", "正确吗", "成立吗", "对吗", "吗"):
        if text.endswith(suffix):
            text = text[:-len(suffix)].strip()
            break
    text = text.translate(str.maketrans({"＝":"=", "＋":"+", "－":"-", "−":"-", "×":"*", "÷":"/", "（":"(", "）":")"})).replace("等于", "=")
    if len(text) > 160 or not re.fullmatch(r"[0-9.\s+*/()=\-]+", text) or text.count("=") != 1:
        return None

    def calculate(expr):
        tokens = re.findall(r"[0-9]+(?:\.[0-9]+)?|[()+*/\-]", expr)
        if not tokens or len(tokens) > 80 or "".join(tokens) != re.sub(r"\s+", "", expr):
            raise ValueError("unsupported expression")
        pos = 0

        def bounded(value):
            if max(value.numerator.bit_length(), value.denominator.bit_length()) > 4096:
                raise ValueError("calculation limit")
            return value

        def atom(depth):
            nonlocal pos
            if depth > 16 or pos >= len(tokens):
                raise ValueError("nesting or incomplete expression")
            token = tokens[pos]
            pos += 1
            if token in ("+", "-"):
                value = atom(depth+1)
                return value if token == "+" else -value
            if token == "(":
                value = expression(depth+1)
                if pos >= len(tokens) or tokens[pos] != ")":
                    raise ValueError("missing parenthesis")
                pos += 1
                return value
            if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", token) or len(token) > 25:
                raise ValueError("unsupported number")
            return Fraction(token)

        def term(depth):
            nonlocal pos
            value = atom(depth)
            while pos < len(tokens) and tokens[pos] in ("*", "/"):
                op = tokens[pos]
                pos += 1
                rhs = atom(depth)
                value = bounded(value * rhs if op == "*" else value / rhs)
            return value

        def expression(depth):
            nonlocal pos
            value = term(depth)
            while pos < len(tokens) and tokens[pos] in ("+", "-"):
                op = tokens[pos]
                pos += 1
                rhs = term(depth)
                value = bounded(value + rhs if op == "+" else value - rhs)
            return value

        value = expression(0)
        if pos != len(tokens):
            raise ValueError("trailing tokens")
        return value

    left, right = text.split("=")
    try:
        lhs, rhs = calculate(left), calculate(right)
    except (ValueError, ZeroDivisionError, OverflowError):
        return None
    verdict = "对" if lhs == rhs else "错"
    reason = f"按通常四则运算规则精确计算，左边为{lhs}，右边为{rhs}，等式{'成立' if lhs == rhs else '不成立'}。"
    calculation = {"expression":text,"left":str(lhs),"right":str(rhs),"equal":lhs == rhs,"method":"exact_rational_arithmetic"}
    return {"verdict":verdict,"reason":reason,"detail":"本结论来自程序对纯数字等式的精确计算；元宝搜索资料另列供查阅，不作为这次算术结论的证明。仅适用通常四则运算，不处理变量、单位、特殊数学体系或附加条件。","uncertainty_reason":"","status":"complete","answer_raw":"","llm_error":None,"verification_method":"exact_arithmetic","calculation":calculation,"evidence_audit":{"accepted":True,"issues":[],"evidence":[],"note":"程序精确计算，非网页引文认证。"},"evidence_strength":"较强","evidence_strength_note":"纯算术等式由程序用精确分数计算，不是模型自信度或概率。","evidence_basis":[reason,"计算依据为通常四则运算；搜索结果没有被冒充为计算证明。"],"next_steps":[]}


def mark_incomplete(report, query, reason, uncertainty, error=None):
    """服务未完成时同步清除已通过徽标，避免页面等级与状态矛盾。"""
    report.update(verdict="不确定", status="incomplete", reason=reason, uncertainty_reason=uncertainty)
    report["evidence_audit"]["accepted"] = False
    report.update(_evidence_guidance(query,"不确定","incomplete",[],[uncertainty],uncertainty))
    if error is not None:
        report["llm_error"] = error


def normalize_citations(text):
    """只兼容实际出现的 [n2] 写法；编号存在性和引文仍另行严格核对。"""
    return re.sub(r"\[n([1-9]\d*)\]", r"[\1]", text)


def unavailable_report(query, error):
    """模型忙或冷却时在搜索前结束，避免明知无法综合仍消耗搜索额度。"""
    guidance = _evidence_guidance(query,"不确定","incomplete",[],[error],error)
    return {"query":query,"verdict":"不确定","status":"incomplete","reason":"调查未完成："+error,"detail":"本次未发起搜索；请在服务恢复后重新查证。","uncertainty_reason":error,"llm_error":error,"sources":[],"result_count":0,"mock":False,"wsa_version":"","searched_at":_now_str(),"runtime_version":RUNTIME_VERSION,"timings":{"search":0.0,"synthesis":0.0,"total":0.0},"evidence_audit":{"accepted":False,"issues":[error],"evidence":[]},**guidance}


def assess(query, results, prompt=None, timeout=35, progress=None):
    """纯算术精确计算；其他命题由模型提出判断并核对证据门槛。"""
    arithmetic = exact_arithmetic(query)
    if arithmetic is not None:
        return arithmetic
    if not results:
        guidance = _evidence_guidance(query,"不确定","complete",[],["无资料"],"未查到资料")
        return {"verdict":"不确定", "reason":"本次未检索到有效资料。", "detail":"没有证据不能判断对错。", "uncertainty_reason":"未查到资料", "status":"complete", "answer_raw":"", "llm_error":None, "evidence_audit":{"accepted":False,"issues":["无资料"],"evidence":[]}, **guidance}
    _, key, model, base = llm_openai.resolve()
    _assess_started = time.perf_counter()

    def _on_retry(attempt, wait):
        emit(progress, "synthesis",
             f"模型服务短暂限流，{round(wait,1)} 秒后重试（第 {attempt} 次）；已有资料不会重搜。",
             _assess_started)

    def _on_fallback(new_model, reason):
        emit(progress, "synthesis",
             f"主模型刚才不可用，已自动改用同提供方备用模型 {new_model} 继续判断；证据门槛不变。",
             _assess_started)

    try:
        raw = llm_openai.chat(prompts.SYSTEM_PROMPT, prompt or prompts.build_user_prompt(query,results), api_key=key, model=model, base_url=base, timeout=max(1,timeout), max_tokens=1100, on_retry=_on_retry, on_fallback=_on_fallback)
    except llm_openai.LLMError as exc:
        guidance = _evidence_guidance(query,"不确定","incomplete",[],["模型调用未完成"],"服务失败或超时")
        return {"verdict":"不确定", "reason":"调查未完成：结论生成超时或服务暂时不可用。", "detail":"已取得资料列在下方，可核对原文；未生成的结论不代表说法错误。", "uncertainty_reason":"服务失败或超时", "status":"incomplete", "llm_error":str(exc), "evidence_audit":{"accepted":False,"issues":["模型调用未完成"],"evidence":[]}, **guidance}
    try:
        if not isinstance(raw,str):
            raise ValueError("non-text model content")
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
        data = json.loads(text)
        if not isinstance(data,dict):
            raise ValueError("not object")
        if data.get("verdict") not in VERDICTS or type(data.get("conflict")) is not bool:
            raise ValueError("invalid verdict or conflict")
        if "evidence" not in data:
            raise ValueError("missing evidence list")
        for field in ("reason","detail","uncertainty_reason","limitations"):
            if field in data and not isinstance(data[field],str):
                raise ValueError("invalid text field")
        for field in ("next_steps","evidence_basis"):
            if field in data and (not isinstance(data[field],list) or not all(isinstance(s,str) for s in data[field])):
                raise ValueError("invalid text list")
        if "evidence" in data:
            if not isinstance(data["evidence"],list):
                raise ValueError("invalid evidence list")
            for item in data["evidence"]:
                if not isinstance(item,dict) or item.get("stance") not in ("支持","反对","背景"):
                    raise ValueError("invalid evidence stance")
                if "direct" in item and type(item["direct"]) is not bool:
                    raise ValueError("invalid direct flag")
                if "quote" in item and not isinstance(item["quote"],str):
                    raise ValueError("invalid evidence quote")
    except (ValueError, TypeError):
        guidance = _evidence_guidance(query,"不确定","incomplete",[],["非JSON结构化输出"],"输出格式异常")
        return {"verdict":"不确定", "reason":"调查未完成：模型没有返回可核验的结构化证据。", "detail":"请查看来源或重新查证。", "uncertainty_reason":"输出格式异常", "status":"incomplete", "llm_error":"invalid_evidence_format", "answer_raw":raw, "evidence_audit":{"accepted":False,"issues":["非JSON结构化输出"],"evidence":[]}, **guidance}
    by_no = {r._no:r for r in results}
    evidence, issues = [], []
    clean = lambda s: re.sub(r"\s+", "", s or "")
    items = data.get("evidence")
    if not isinstance(items,list):
        items = []
        issues.append("证据列表缺失")
    for item in items[:12]:
        if not isinstance(item,dict):
            issues.append("证据格式错误")
            continue
        no = item.get("source_no")
        source = by_no.get(no) if type(no) is int else None
        quote = item.get("quote") if isinstance(item.get("quote"),str) else ""
        segment = item.get("segment_id")
        if segment is not None:
            # 编号一旦存在就只从实际提供的分段取回，编号坏了不回退到模型引文。
            quote = ""
            bounds = None
            if type(segment) is int:
                bounds = (segment, segment)
            elif isinstance(segment, str):
                match = re.fullmatch(r"s?([1-9]\d*)(?:\s*-\s*s?([1-9]\d*))?", segment)
                if match:
                    bounds = (int(match[1]), int(match[2] or match[1]))
            if source and bounds:
                parts = prompts.evidence_segments(source)
                first, last = bounds
                if 1 <= first <= last <= len(parts) and last-first < 12:
                    quote = "\n".join(parts[first-1:last])
        if segment is None and not clean(quote):
            # 空引文不算证据，也不当成“核对失败”去否决其他有效证据。
            continue
        # 已提供的分段由后台直接取回，短句也可匹配；自由引文仍限制碎片长度。
        verified = bool(source and (bool(clean(quote)) if segment is not None else len(clean(quote))>=8) and clean(quote) in clean(prompts.evidence_text(source)[:3500]))
        eligible = bool(source and source._tier in (authority.TIER_PRIMARY,authority.TIER_SECONDARY))
        evidence.append({"source_no":no,"stance":item.get("stance"),"quote":quote,"direct":item.get("direct") is True,"quote_verified":verified,"source_eligible":eligible})
        if not verified:
            issues.append(f"来源[{no}]引文无法在本次提供材料中核对")
    if not isinstance(data.get("conflict"), bool):
        issues.append("未明确报告有无证据冲突")
    verdict = data.get("verdict")
    if verdict not in VERDICTS:
        issues.append("结论不属于三态")
        verdict = "不确定"
    direct = [e for e in evidence if e["quote_verified"] and e["source_eligible"] and e["direct"]]
    desired = "支持" if verdict=="对" else "反对"
    if verdict in ("对","错") and not any(e["stance"]==desired for e in direct):
        issues.append("缺少直接、可逐字核验且来源背景可靠的对应证据")
    if data.get("conflict") is True or {e["stance"] for e in direct} >= {"支持","反对"}:
        issues.append("存在未解决的可靠证据冲突")
    # 在原始输出保留于日志的同时规范化展示；不把不存在的引用变成有效证据。
    for field in ("reason","detail","uncertainty_reason","limitations"):
        if isinstance(data.get(field),str):
            data[field] = normalize_citations(data[field])
    for field in ("next_steps","evidence_basis"):
        if isinstance(data.get(field),list):
            data[field] = [normalize_citations(value) for value in data[field]]
    detail = data.get("detail") if isinstance(data.get("detail"),str) else ""
    reason = data.get("reason") if isinstance(data.get("reason"),str) else ""
    refs = re.findall(r"\[(\d+)\]", reason+detail)
    if any(int(n) not in by_no for n in refs):
        issues.append("解释含不存在的来源编号")
    verified_nos = {e["source_no"] for e in evidence if e["quote_verified"]}
    if any(int(n) not in verified_nos for n in refs):
        issues.append("解释引用的材料缺少已匹配引文")
    evidence_nos = {e["source_no"] for e in direct if e["stance"]==desired}
    if verdict in ("对","错") and (not refs or not any(int(n) in evidence_nos for n in refs)):
        issues.append("解释未引用对应的已核验直接证据")
    accepted = verdict in ("对","错") and not issues
    if issues:
        verdict = "不确定"
        reason = "找到的资料还不足以确认这句话。"
        detail = "；".join(dict.fromkeys(issues)) + "。原始模型判断仅存日志，不作为通过核验的结论。"
    uncertainty = ("；".join(dict.fromkeys(issues)) if issues else str(data.get("uncertainty_reason") or data.get("limitations") or "证据尚不充分")) if verdict=="不确定" else ""
    guidance = _evidence_guidance(query,verdict,"complete",evidence,issues,uncertainty,data.get("next_steps"))
    basis = [s.strip() for s in data.get("evidence_basis",[]) if isinstance(s,str) and s.strip()][:3] if isinstance(data.get("evidence_basis"),list) else []
    if not basis:
        basis = [f"已逐字核对{sum(1 for e in evidence if e['quote_verified'])}条引文；{sum(1 for e in direct if e['stance']=='支持')}条直接支持、{sum(1 for e in direct if e['stance']=='反对')}条直接反对通过来源门槛。"]
    verified_n = sum(1 for e in evidence if e["quote_verified"])
    eligible_direct = [e for e in direct if e["stance"] in ("支持", "反对")]
    counts = {stance: sum(1 for e in eligible_direct if e["stance"] == stance) for stance in ("支持", "反对")}
    if verified_n:
        basis = [f"本次模型选出的{len(evidence)}条证据中，{verified_n}条引文与搜索材料匹配；其中{counts['支持']}条直接支持、{counts['反对']}条直接反对通过来源背景门槛。"]
    else:
        basis = ["本次没有可逐字匹配到搜索材料的引文。"]
    guidance["evidence_basis"] = basis
    return {"verdict":verdict,"reason":reason or "现有证据不足以判断。","detail":detail,"uncertainty_reason":uncertainty,"status":"complete","answer_raw":raw,"llm_error":None,"limitations":data.get("limitations"),"evidence_audit":{"accepted":accepted,"issues":list(dict.fromkeys(issues)),"evidence":evidence,"note":"逐字引文和来源背景核对通过不等于语义或最终结论已获独立人工审定。"},**guidance}


MAX_QUICK_BACKFILL = 2

def _backfill(query, results, deadline=None):
    counts = authority.tier_summary(results)
    if counts.get(authority.TIER_PRIMARY,0)+counts.get(authority.TIER_SECONDARY,0)>0:
        return results
    plan = authority.backfill_plan(authority.detect_domain(query))[:MAX_QUICK_BACKFILL]
    seen = {r.url for r in results}
    for _, site in plan:
        if deadline and deadline-time.perf_counter()<3:
            break
        try:
            extra,_ = wsa_search(query,secret_id=config.TENCENT_SECRET_ID,secret_key=config.TENCENT_SECRET_KEY,mode=config.WSA_MODE,cnt=config.WSA_CNT,site=site, timeout=min(6,deadline-time.perf_counter()) if deadline else 6)
        except WsaSearchError:
            continue
        for r in extra:
            if r.url and r.url not in seen:
                results.append(r)
                seen.add(r.url)
        if any(authority.classify(r.url)["tier"]==authority.TIER_PRIMARY for r in extra):
            break
    return results


def check(query, progress=None):
    if not config.USE_MOCK and exact_arithmetic(query) is None:
        unavailable = llm_openai.preflight()
        if unavailable:
            report = unavailable_report(query,unavailable)
            _dump_debug(report)
            return report
    started = time.perf_counter()
    timings = {}
    emit(progress,"search","正在用元宝搜索检索原始说法",started)
    error = None
    if config.USE_MOCK:
        results,version = [],"mock"
    else:
        try:
            results,version = wsa_search(query,secret_id=config.TENCENT_SECRET_ID,secret_key=config.TENCENT_SECRET_KEY,mode=config.WSA_MODE,cnt=config.WSA_CNT,timeout=10)
            results = _backfill(query,results,started+16)
        except WsaSearchError as exc:
            results,version,error = [],"",str(exc)
    timings["search"] = round(time.perf_counter()-started,3)
    counts = _tag_authority(results)
    emit(progress,"synthesis",f"已检索到{len(results)}条资料，正在核对直接证据；尚未下结论",started,sources=[source_dict(r) for r in results])
    begin = time.perf_counter()
    report = assess(query,results,timeout=max(1,min(35,48-(begin-started))),progress=progress)
    if error or config.USE_MOCK:
        mark_incomplete(report,query,"演示模式没有真实证据。" if config.USE_MOCK else "调查未完成：搜索服务失败。","演示模式" if config.USE_MOCK else "检索失败",error)
    timings["synthesis"] = round(time.perf_counter()-begin,3)
    timings["total"] = round(time.perf_counter()-started,3)
    result = {"query":query,**report,"sources":[source_dict(r) for r in results],"authority_summary":counts,"searched_at":_now_str(),"wsa_version":version,"result_count":len(results),"mock":config.USE_MOCK,"timings":timings,"runtime_version":RUNTIME_VERSION}
    _dump_debug(result)
    return result
