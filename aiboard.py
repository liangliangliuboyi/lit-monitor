#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
AI 文献板块（医学人工智能 / 通用 AI / 大模型 / 智能体）

独立的文献流，与糖尿病周报并行、单独成册：
  - 抓 arXiv 的 cs.AI / cs.CL / cs.CV / cs.LG / cs.MA / stat.ML（顶会论文基本都在 arXiv 预印）
  - 用 profile.md 的「AI 板块关键词」筛出 AI 相关文献
  - 按  顶会 CCF-A(A0) / 医学人工智能(A1) / 通用 AI·大模型(A2)  三分
  - 下载 PDF 到  papers_dir/AI文献/<周>/<分类>/  本地文件夹
  - 生成与「一键打开」同款展示的报告（0/1/2/3 数字前缀命名）
  - 通过 mail.py 随周报一起推送到 QQ 邮箱

纯标准库（urllib / sqlite3 / xml.etree），零第三方依赖，搬走就能用。

用法：
  python aiboard.py              # 抓取 → 分类 → 大模型增强 → 报告 → 下载（一步到位，供 run.py 调用）
  python aiboard.py fetch        # 只抓取 + 分类 + 入库
  python aiboard.py report       # 只根据已入库数据出报告
  python aiboard.py download     # 只下载 PDF
  python aiboard.py recat        # 历史回填：用新版规则重算全库分类字段并重生成报告（不剔除已入库行）
  python aiboard.py augment      # 对全库未判条目跑大模型增强（需配置大模型）
"""
from __future__ import annotations

import datetime
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import reportlib as RL
import profile as P
import queryx          # 检索前 LLM 扩词（RAG 召回增强）+ 检索闭环（反馈环）

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, "ai.db")

# 落盘文件名（数字前缀 = 常用度排序，与糖尿病周报完全一致：0_一键打开 / 1_本周综述 / 2_题录总表 / 3_待下载）。
# 这几个名字由 reportlib.write_library/write_review/write_pending 统一写出，这里只做常量别名，避免文件名对不上。
F_AI_LIB = RL.F_LIB      # "0_一键打开链接.html"  最常用：全量清单 + 筛选
F_AI_REVIEW = RL.F_REVIEW  # "1_本周综述.html"    分组综述（本方向核心 + 必读 + 前沿 + 方法可迁移 + 趋势/关联）
F_AI_CSV = RL.F_CSV      # "2_题录总表.csv"      数据版（Zotero/Excel）
F_AI_PEND = RL.F_PEND    # "3_待下载清单.html"   没下到的

# AI 分类 → 本地 PDF 子文件夹（数字前缀，与报告文件一致）
CAT_FOLDER = {"A0": "0_大模型智能体", "A1": "1_医学人工智能", "A2": "2_通用AI其他"}
CAT_ORDER = ["A0", "A1", "A2"]

ARXIV_NS = "{http://www.w3.org/2005/Atom}"
# 顶会：不止 CCF-A，覆盖 AI/ML/CV/NLP/医学影像 各方向公认顶级会议
# （CCF-A 全量 + 强 CCF-B + 医学 AI 专属会 MICCAI/MIDL/ML4H/CHIL）
CONF_PAT = re.compile(
    r"\b(neurips|\bnips\b|icml|iclr|cvpr|iccv|eccv|\bacl\b|emnlp|naacl|aaai|ijcai|"
    r"kdd|coling|sigir|wacv|accv|bmvc|icpr|icdar|miccai|ipmi|midl|ml4h|chil|"
    r"siggraph|sigcomm|mlsys|aistats|uai|colt|ecai|aamas|corl|cikm|icdm|sdm|"
    r"ijcnn|interspeech|icassp|recomb|ismb|bibm|embc|acm\s*mm|acm multimedia|"
    r"www(?=\s*'?\d{2})|web conference)", re.I)
# 顶刊：AI / 医学 AI / 数字健康 领域公认顶级期刊（用于「顶会/顶刊线索」徽章 + PubMed 源检索）
JOURNAL_PAT = re.compile(
    r"(nature machine intelligence|nature medicine|nature biomedical engineering|"
    r"nature communications|nature computational science|nature reviews?|"
    r"npj digital medicine|digital medicine|lancet digital health|nejm ai|"
    r"new england journal|\bjama\b|\bjamia\b|"
    r"journal of the american medical informatics|journal of medical internet research|"
    r"\bjmir\b|medical image analysis|transactions on medical imaging|"
    r"transactions on pattern analysis|\btpami\b|\bjmlr\b|journal of machine learning research|"
    r"transactions on neural networks|\btnnls\b|transactions on knowledge and data|"
    r"bioinformatics|briefings in bioinformatics|plos digital health|"
    r"radiology.?artificial intelligence|journal of biomedical and health|\bjbhi\b|"
    r"science advances|science translational medicine|science robotics|"
    r"cell reports medicine|patterns\.?cell|acm computing surveys|"
    r"ieee transactions on artificial intelligence|acm transactions on intelligent)", re.I)
MED_PAT = re.compile(
    r"\b(medical|medicine|clinical|clinic|health|healthcare|hospital|radiolog|"
    r"diagnos|patient|electronic health record|\behr\b|biomed|patholog|\bmri\b|"
    r"ct scan|imaging|disease|cancer|tumor|cardio|diabet|telehealth|epidemi|"
    r"public health|mental health|drug|genom|biolog|surgery|nursing|pharma)\b", re.I)

# A0（用户主攻方向）：大模型 / 智能体 / 多模态 / 基础模型
LLM_PAT = re.compile(
    r"\b(large language model|\bllm\b|language model|\bgpt\b|chatgpt|llama|qwen|"
    r"deepseek|multimodal|multi-modal|vision language model|\bvlm\b|\bmllm\b|"
    r"\bagent\b|multi-agent|multi agent|rag\b|retrieval.augmented|"
    r"foundation model|prompt|instruction tuning|chatbot|diffusion model|"
    r"embodied|tool use|function calling|agentic)\b", re.I)

# AI 核心方向（与糖尿病侧「导师组方向」等价）：用户点名要重点跟的 AI 前沿方向。
# categorize 命中即记 core_hit = 方向名（报告里单独成组、优先翻译/写评语、进「本周必读」）。
# 关键词用小写子串匹配（命中标题+摘要），与 profile.md「AI 核心方向」块保持同步。
AI_CORE_DIRECTIONS = [
    ("大模型·智能体·多模态", [
        "large language model", "llm", "language model", "chatbot", "agent", "multi-agent",
        "multi agent", "rag", "retrieval augmented", "foundation model", "multimodal",
        "multi-modal", "vision language", "vlm", "mllm", "prompt", "instruction tuning",
        "embodied", "tool use", "function calling", "agentic", "diffusion transformer",
        "reasoning", "mixture of experts", "moe", "long context", "rlhf", "dpo", "alignment"]),
    ("医学AI大模型", [
        "medical large language model", "medical ai large model", "clinical large language model",
        "medical foundation model", "medical ai", "clinical llm"]),
    ("RAG+Agent+多模型增强", [
        "retrieval augmented generation medical", "rag medical", "multi-agent medical",
        "multi model medical", "agentic medical ai", "graph rag", "multimodal rag",
        "knowledge graph"]),
    ("眼底/视网膜AI", [
        "retinal image", "fundus image", "retinal disease", "ophthalmic", "eye image",
        "diabetic retinopathy"]),
    ("医学影像AI", [
        "medical imaging ai", "radiology ai", "medical image", "whole slide",
        "computational pathology"]),
    ("临床辅助决策系统", [
        "clinical decision support system ai", "clinical decision support system diabetes",
        "clinical decision support", "clinical recommendation"]),
    ("主动健康管理小程序", [
        "active health management", "mhealth diabetes", "health management app",
        "mobile health", "health app"]),
    ("AI VR运动干预", [
        "virtual reality exercise", "vr exercise", "exergame", "ai exercise intervention",
        "exercise intervention", "physical activity ai"]),
    ("可穿戴设备", [
        "wearable device", "smartwatch", "wearable", "smart watch", "sensor"]),
    ("数字健康/远程医疗", [
        "digital health", "telemedicine", "ehealth", "remote patient monitoring",
        "telehealth", "mobile health"]),
    ("慢病/糖尿病AI", [
        "diabetes ai", "glucose", "cgm", "chronic disease", "cardiovascular", "obesity"]),
]


# ---------------------------------------------------------------- 路径 / 周
def cfg():
    return P.cfg()


def ai_cfg():
    return (cfg().get("ai_board") or {})


def _ai_profile():
    """AI 板块的「画像词表」，供 queryx 扩词/闭环使用（等价糖尿病侧 profile.profile()）。
    core=AI 核心方向（权重词），extension=AI 板块关键词（过滤词）。"""
    return {"core": P.ai_core_terms(), "extension": P.ai_terms()}


def _ai_expanded_terms(c):
    """RAG 式召回增强：取 AI 板块的扩展检索词（静态画像扩词 + 上周反馈词）。
    带缓存 TTL，不每次都调 LLM；LLM 不可用返回 []（退回基础检索式，绝不阻断）。"""
    try:
        return queryx.load_expanded_terms(c, domain="ai")
    except Exception as e:
        print("[warn] AI 扩词失败，用基础检索式：%r" % e)
        return []


def week_folder(days):
    today = datetime.date.today()
    start = today - datetime.timedelta(days=max(1, int(days)) - 1)
    wk = "W%02d" % today.isocalendar()[1]
    return "%d-%s (%02d-%02d~%02d-%02d)" % (
        today.year, wk, start.month, start.day, today.month, today.day)


def ai_root():
    """AI 板块落盘根目录。

    优先用 ai_board.root（显式写死的 0博士文献）。这么设计是因为：糖尿病文献
    挪进 0博士文献/糖尿病文献 之后，若 AI 板块还跟着 paths.papers_dir 走，就会
    跑到「糖尿病文献/AI文献」里去。留空时才回退到旧的 papers_dir/AI文献 行为。
    """
    ac = ai_cfg()
    r = (ac.get("root") or "").strip()
    if not r:
        r = (cfg().get("paths") or {}).get("papers_dir") or ""
    return os.path.join(r, ac.get("folder", "AI文献"))


def week_dir(week):
    return os.path.join(ai_root(), week)


# ---------------------------------------------------------------- 数据库
def _con():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    con.execute("""CREATE TABLE IF NOT EXISTS papers(
        key TEXT PRIMARY KEY, week TEXT, title TEXT,         journal TEXT, tier TEXT,
        pub_date TEXT, doi TEXT, url TEXT, pdf_url TEXT, abstract TEXT, comment TEXT,
        conf_hint INTEGER DEFAULT 0, venue TEXT,
        authors TEXT, affiliations TEXT, source TEXT, cat TEXT, score REAL,
        topics TEXT, pdf_path TEXT,
        cn_title TEXT, cn_authors TEXT, cn_aff TEXT, cn_abstract TEXT,
        cn_keywords TEXT, note TEXT)""")
    # 旧库可能没有 comment / conf_hint 列，做一次安全迁移
    for col in ("comment", "conf_hint", "venue"):
        try:
            con.execute("ALTER TABLE papers ADD COLUMN %s" % col)
        except Exception:
            pass
    # 2026-10 对齐糖尿病标准：核心方向 / 等级标签 / LLM 识别 字段
    for col in ("core_hit", "tier_label", "llm_tag", "llm_reason", "advisor_fit"):
        try:
            con.execute("ALTER TABLE papers ADD COLUMN %s TEXT" % col)
        except Exception:
            pass
    return con


def save(rec):
    con = _con()
    cols = list(rec.keys())
    ph = ",".join("?" * len(cols))
    con.execute("INSERT OR REPLACE INTO papers(%s) VALUES(%s)" % (",".join(cols), ph),
                [rec[c] for c in cols])
    con.commit()
    con.close()


def load_week(week):
    """取一周的条目。

    这里的 week 已经是「2026-W40 (09-28~10-04)」这种【周标签】（见 week_folder），
    同一周内无论重跑几次都是同一个标签，所以直接等值查询就是稳定的。
    （糖尿病主库的 papers.week 记的是抓取当天日期，才有"同周换天即丢数据"的问题，
    那里已改成按 ISO 周区间取，见 download._week_rows。）
    """
    con = _con()
    rows = con.execute("SELECT * FROM papers WHERE week=?", (week,)).fetchall()
    con.close()
    return [dict(r) for r in rows]


def set_pdf(key, path):
    con = _con()
    con.execute("UPDATE papers SET pdf_path=? WHERE key=?", (path, key))
    con.commit()
    con.close()


def latest_week():
    con = _con()
    w = con.execute("SELECT max(week) FROM papers").fetchone()[0]
    con.close()
    return w


def load_all():
    """取全部条目（历史回填用）。"""
    con = _con()
    rows = con.execute("SELECT * FROM papers").fetchall()
    con.close()
    return [dict(r) for r in rows]


def distinct_weeks():
    con = _con()
    ws = [r[0] for r in con.execute("SELECT DISTINCT week FROM papers WHERE week IS NOT NULL ORDER BY week").fetchall()]
    con.close()
    return ws


def recat_all(regenerate=True):
    """历史回填：用新版 categorize 重新计算全部条目的分类字段（core_hit/tier_label/
    llm_tag/advisor_fit/score/cat），不剔除任何已入库行（drop_noise=False）。

    用途：老库（改版前入库的 437 篇）缺这些字段，回填后整块 AI 文献才与糖尿病标准一致。
    回填只写回可推导的字段，不依赖大模型；llm_tag 对非本方向核心项留空，等 augment 补齐。"""
    rows = categorize(load_all(), drop_noise=False)
    for it in rows:
        save(it)
    print("recat 完成：%d 篇已重算分类字段" % len(rows))
    if regenerate:
        c = cfg()
        for wk in distinct_weeks():
            its = load_week(wk)
            pdf_map = {it["key"]: os.path.relpath(it["pdf_path"], week_dir(wk))
                       for it in its if it.get("pdf_path") and os.path.exists(it["pdf_path"])}
            write_reports(wk, its, pdf_map, c)


def augment_all():
    """对全库尚未判过 llm_tag 的条目跑大模型增强（与 screen.llm_augment --all 等价）。
    需要配置大模型；无大模型则静默跳过。"""
    c = cfg()
    if not RL._llm_cfg(c):
        print("[augment_all] 未配置大模型，跳过")
        return
    rows = load_all()
    ai_llm_augment(rows, c)
    # 增强后顺手把新字段写回（ai_llm_augment 已直接 UPDATE 库，这里仅重生成报告）
    for wk in distinct_weeks():
        its = load_week(wk)
        pdf_map = {it["key"]: os.path.relpath(it["pdf_path"], week_dir(wk))
                   for it in its if it.get("pdf_path") and os.path.exists(it["pdf_path"])}
        write_reports(wk, its, pdf_map, c)
    print("augment_all 完成")


# ---------------------------------------------------------------- 抓取
def _arxiv_query(extra=None):
    """宽口径：AI 相关分类最新论文（供 A1/A2 新鲜流）。
    extra：queryx 的 LLM 扩词（RAG 召回增强），作为 abs: 词并进 OR，捞回画像没写的新方向。"""
    cats = ["cs.AI", "cs.CL", "cs.CV", "cs.LG", "cs.MA", "stat.ML"]
    parts = ["cat:%s" % c for c in cats]
    for t in (extra or [])[:40]:
        parts.append('abs:"%s"' % t)
    return urllib.parse.quote(" OR ".join(parts))


# CCF-A 级别 AI/ML 顶会（用于 A0 顶会板块的精准抓取）
_CONF_NAMES = ["NeurIPS", "ICML", "ICLR", "CVPR", "ICCV", "ECCV", "ACL", "EMNLP",
               "NAACL", "AAAI", "IJCAI", "KDD", "SIGIR", "WWW", "MICCAI", "COLING",
               "WACV", "IPMI", "SIGGRAPH", "ACM Multimedia"]


def _conf_query(extra=None):
    """顶会口径：摘要里点名了 CCF-A 顶会的论文（不限分类，按提交时间倒序）。
    抓取窗口更宽（conf_lookback_days），因为录用/相机就绪版本往往在投稿数月后才挂出来，
    7 天新鲜流里几乎不会出现会议名。
    extra：queryx 的 LLM 扩词，作为 abs: 词并进 OR。"""
    parts = ['abs:"%s"' % c for c in _CONF_NAMES]
    for t in (extra or [])[:40]:
        parts.append('abs:"%s"' % t)
    return urllib.parse.quote(" OR ".join(parts))


def fetch_arxiv(days, cap=200, query=None):
    """抓 arXiv 论文（按提交时间倒序），返回标准化记录列表。
    query 为 None 时默认用宽口径 AI 分类查询。"""
    if query is None:
        query = _arxiv_query()
    url = ("https://export.arxiv.org/api/query?search_query=%s"
           "&sortBy=submittedDate&sortOrder=descending&max_results=%d"
           % (query, cap))
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "lit-monitor/1.0"})
        last = None
        for attempt in range(4):
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    data = r.read().decode("utf-8", "replace")
                break
            except urllib.error.HTTPError as e:
                last = e
                if e.code == 429:
                    wait = 8 * (attempt + 1)
                    print("[warn] arXiv 429 限流，%ds 后重试(%d/4)…" % (wait, attempt + 1))
                    time.sleep(wait)
                    continue
                raise
        else:
            print("[warn] arXiv 抓取失败：%r" % last)
            return []
    except Exception as e:
        print("[warn] arXiv 抓取失败：%r" % e)
        return []
    try:
        root = ET.fromstring(data)
    except Exception as e:
        print("[warn] arXiv XML 解析失败：%r" % e)
        return []

    cutoff = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    out = []
    for e in root.findall(ARXIV_NS + "entry"):
        idu = (e.findtext(ARXIV_NS + "id") or "").strip()
        if not idu:
            continue
        aid = re.sub(r"v\d+$", "", idu.rsplit("/", 1)[-1])
        title = " ".join((e.findtext(ARXIV_NS + "title") or "").split())
        abstract = " ".join((e.findtext(ARXIV_NS + "summary") or "").split())
        comment = " ".join((e.findtext(ARXIV_NS + "comment") or "").split())
        pub = (e.findtext(ARXIV_NS + "published") or "")[:10]
        if pub < cutoff:
            continue
        authors = [a.findtext(ARXIV_NS + "name").strip()
                   for a in e.findall(ARXIV_NS + "author") if a.findtext(ARXIV_NS + "name")]
        pdf = ""
        for l in e.findall(ARXIV_NS + "link"):
            if (l.get("title") == "pdf") or (l.get("rel") == "related" and l.get("title") == "pdf"):
                pdf = l.get("href") or ""
                break
        if not pdf:
            pdf = "https://arxiv.org/pdf/%s.pdf" % aid
        out.append({
            "key": "arxiv:" + aid,
            "week": "",
            "title": title,
            "journal": "arXiv",
            "tier": "P",
            "pub_date": pub,
            "doi": aid,
            "url": "https://arxiv.org/abs/%s" % aid,
            "pdf_url": pdf,
            "abstract": (abstract + (" | " + comment if comment else "")).strip(),
            "comment": comment,
            "authors": "; ".join(authors),
            "affiliations": "",
            "source": "arxiv",
            "cat": "",
            "score": 0,
            "topics": "",
            "pdf_path": "",
            "cn_title": "", "cn_authors": "", "cn_aff": "",
            "cn_abstract": "", "cn_keywords": "", "note": "",
        })
    return out


# 医学 AI / 数字健康 顶级期刊（PubMed 源按这个刊表检索，收的是【真正已发表】的顶刊文章，
# 与 arXiv 预印本互补 —— 用户要的"顶刊"主要靠这条线拿到）
AI_TOP_JOURNALS = [
    "Nature machine intelligence", "Nature medicine", "Nature biomedical engineering",
    "Nature communications", "Nature computational science", "NPJ digital medicine",
    "The Lancet digital health", "NEJM AI", "JAMA", "JAMA network open",
    "Journal of the American Medical Informatics Association",
    "Journal of medical Internet research", "Medical image analysis",
    "IEEE transactions on medical imaging", "IEEE transactions on pattern analysis and machine intelligence",
    "Bioinformatics", "Briefings in bioinformatics", "PLOS digital health",
    "Radiology. Artificial intelligence", "IEEE journal of biomedical and health informatics",
    "Science advances", "Science translational medicine", "Cell reports medicine",
    "Patterns", "IEEE transactions on neural networks and learning systems",
]
AI_TOPIC_Q = ["artificial intelligence", "machine learning", "deep learning",
              "large language model", "foundation model", "neural network",
              "transformer", "multimodal", "AI agent", "predictive model"]


def fetch_pubmed_ai(days=7, cap=100, extra_terms=None):
    """从 PubMed 抓【近期发表在 AI/数字健康顶刊上】的医学 AI 文章。
    extra_terms：queryx 的 LLM 扩词，作为 [Title/Abstract] 词并进查询（提升召回）。

    与 arXiv 互补：arXiv 给的是最新预印本（快、但没发表）；
    这里给的是真正见刊的高质量文章（顶刊），两者合并才是完整的"顶会+顶刊"视野。
    """
    import urllib.parse
    api_key = (cfg().get("pubmed") or {}).get("api_key", "")
    ak = ("&api_key=%s" % api_key) if api_key else ""
    jq = " OR ".join('"%s"[Journal]' % j for j in AI_TOP_JOURNALS)
    tqs = ['"%s"[Title/Abstract]' % t for t in AI_TOPIC_Q]
    tqs += ['"%s"[Title/Abstract]' % t for t in list(extra_terms or [])[:40]]
    tq = " OR ".join(tqs)
    q = "(%s) AND (%s)" % (tq, jq)
    u = ("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?db=pubmed&term=%s"
         "&retmode=json&retmax=%d&sort=date&reldate=%s&datetype=edat%s"
         % (urllib.parse.quote(q), cap, max(1, int(days)), ak))
    try:
        req = urllib.request.Request(u, headers={"User-Agent": "lit-monitor/1.0"})
        with urllib.request.urlopen(req, timeout=60) as r:
            d = json.loads(r.read().decode("utf-8", "replace"))
        ids = d.get("esearchresult", {}).get("idlist", [])
    except Exception as e:
        print("[warn] PubMed(AI) esearch 失败：%r" % e)
        return []
    if not ids:
        return []
    time.sleep(0.5)
    ef = ("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?db=pubmed&id=%s&retmode=xml%s"
          % (",".join(ids), ak))
    try:
        import monitor
        req = urllib.request.Request(ef, headers={"User-Agent": "lit-monitor/1.0"})
        with urllib.request.urlopen(req, timeout=90) as r:
            xml = r.read().decode("utf-8", "replace")
        recs = monitor.pubmed_records(xml, source="pubmed_ai")
    except Exception as e:
        print("[warn] PubMed(AI) efetch 失败：%r" % e)
        return []
    out = []
    for r in recs:
        out.append({
            "key": "pmid:" + (r.get("pmid") or (r.get("doi") or r.get("title") or "")),
            "week": "",
            "title": r.get("title") or "",
            "journal": r.get("journal") or "",
            "tier": "P",
            "pub_date": r.get("pub_date") or "",
            "doi": r.get("doi") or "",
            "url": r.get("url") or "",
            "pdf_url": "",
            "abstract": r.get("abstract") or "",
            "comment": "",
            "authors": r.get("authors") or "",
            "affiliations": r.get("affiliations") or "",
            "source": "pubmed_ai",
            "cat": "",
            "score": 0,
            "topics": "",
            "pdf_path": "",
            "cn_title": "", "cn_authors": "", "cn_aff": "",
            "cn_abstract": "", "cn_keywords": "", "note": "",
        })
    return out


def venue_of(it):
    """找出这条文献点名了的顶会 / 顶刊，返回 (类型, 名称)；没有则返回 None。

    类型只分「顶会」「顶刊」，用于前端徽章显示（如「顶会 NeurIPS」「顶刊 Nature MI」）。
    注意：命中仅代表"与顶会/顶刊相关（正文点名）"，不代表已被该会录用或已在该刊发表。
    """
    txt = "%s %s %s" % (it.get("title", ""), it.get("abstract", ""), it.get("journal", ""))
    m = CONF_PAT.search(txt)
    if m:
        return ("顶会", (m.group(0) or "").strip().upper())
    m = JOURNAL_PAT.search(txt)
    if m:
        return ("顶刊", (m.group(0) or "").strip())
    return None


def categorize(items, drop_noise=True):
    """给每条记录定 cat(A0/A1/A2)、core_hit、tier_label、score 与默认 LLM 字段。

    A0 = 大模型·智能体·多模态（用户主攻方向）
    A1 = 医学人工智能（用户领域）
    A2 = 通用 AI / 其他
    core_hit = 命中「AI 核心方向」(AI_CORE_DIRECTIONS) → 记方向名；与糖尿病侧「导师组方向」等价
    conf_hint = 点名了顶会或顶刊（"顶会/顶刊线索"徽章，不代表已被录用/发表）
    tier_label = 点名顶会/顶刊时记其名（质量维度，与期刊 tier 互补）
    score 规则：研究方向基础分 + 本方向核心加权 + 顶会/顶刊加权 + 命中词数
    关键词都不命中、非医学、非大模型、非顶会顶刊线索 → 视为噪音剔除（drop_noise=False 时仅标记不剔除，供历史回填）。
    """
    terms = set(P.ai_terms())
    for it in items:
        txt = ("%s %s" % (it.get("title", ""), it.get("abstract", ""))).lower()
        hit = [t for t in terms if t and t in txt]
        it["topics"] = ",".join(hit[:6])
        v = venue_of(it)
        it["conf_hint"] = 1 if v else 0
        it["venue"] = ("%s %s" % v) if v else ""
        it["tier_label"] = (v[1] if v else "")   # v 形如 (类型, 名称)，这里取名称作为质量维度标签
        # 研究方向
        if LLM_PAT.search(txt):
            it["cat"] = "A0"
            base = 60
        elif MED_PAT.search(txt):
            it["cat"] = "A1"
            base = 55
        else:
            it["cat"] = "A2"
            base = 45
        # 本方向核心（与糖尿病侧「导师组方向」等价）
        core_name = ""
        for name, kws in AI_CORE_DIRECTIONS:
            if any(k in txt for k in kws):
                core_name = name
                break
        it["core_hit"] = core_name
        # 默认 LLM 字段（无大模型时也能有合理展示；有模型时由 ai_llm_augment 细化）
        it["llm_tag"] = "core" if core_name else ""
        it["llm_reason"] = ""
        it["advisor_fit"] = "high" if core_name else ""
        # 分数：方向基础分 + 本方向核心 + 顶会/顶刊 + 命中词数
        sc = base
        if core_name:
            sc += 20
        if v:
            sc += 12
        sc += min(len(hit), 6) * 2
        it["score"] = min(100, sc)
        # 关键词都不命中且非医学非大模型非顶会顶刊线索 → 视为噪音剔除（除非关键词表为空则全收）
        if terms and not hit and not v and not LLM_PAT.search(txt) and not MED_PAT.search(txt):
            it["_drop"] = True
    if drop_noise:
        return [it for it in items if not it.pop("_drop", False)]
    for it in items:
        it.pop("_drop", False)
    return items


# ---------------------------------------------------------------- 大模型增强（对齐糖尿病侧 screen.llm_augment）
def ai_llm_augment(items, cfgobj, verbose=True):
    """对尚未判过 llm_tag 的 AI 文献，送大模型判定 tag(core/extension/method/frontier) 与
    advisor_fit(high/medium/low = 与用户主攻方向贴合度)，写回 llm_tag / llm_reason / advisor_fit。
    对齐糖尿病侧 screen.llm_augment；无大模型时静默跳过（categorize 已给默认 tag/fit）。"""
    if not RL._llm_cfg(cfgobj) or not items:
        return
    cands = [it for it in items if not (it.get("llm_tag") or "").strip()]
    if not cands:
        return
    if verbose:
        print("[ai_llm_augment] %d 篇待判定（core/extension/method/frontier + 本方向贴合度）" % len(cands))
    B = 15
    for i in range(0, len(cands), B):
        batch = cands[i:i + B]
        lines = []
        key_by_idx = {}
        for idx, it in enumerate(batch, 1):
            lines.append("%d | %s | %s" % (idx, (it.get("title") or "")[:200], (it.get("abstract") or "")[:360]))
            key_by_idx[idx] = it["key"]
        prompt = (
            "你是一个人工智能文献分类器。下面是一周内新检索到的 AI/医学AI 文献（序号 | 标题 | 摘要节选）。\n"
            "【任务一】判断每篇与「用户主攻方向（医学人工智能 / 大模型·智能体·多模态）」的相关程度，从以下类别选一个：\n"
            "- core：明确关于医学AI / 大模型 / 智能体 / 多模态 / 医学影像AI / 临床决策AI\n"
            "- extension：相邻延伸主题（数字健康、可穿戴、远程医疗、AI for Science、AI 药物发现等），与医学AI高度互通\n"
            "- method：可迁移到医学AI的方法学 / 智能体框架 / 训练范式\n"
            "- frontier：新颖、能启发研究 idea 的前沿方向\n"
            "- irrelevant：与医学AI/大模型无关\n"
            "【任务二】判断每篇与【用户主攻方向】的贴合度，从 high / medium / low 中选一个：\n"
            "high = 直接命中用户点名方向（大模型·智能体·多模态、医学AI大模型、RAG+Agent、眼底/视网膜AI、"
            "临床辅助决策、主动健康小程序、AI VR运动、可穿戴、数字健康）；medium = 相邻/方法可迁移；low = 基本不相关。\n\n"
            "严格只输出一个 JSON 数组，每个元素为 "
            "{\"idx\":int, \"tag\":str, \"reason\":str(中文20字内), \"fit\":str(high/medium/low)}，"
            "顺序与输入一致，不要解释、不要序号以外的文字：\n"
            + "\n".join(lines)
        )
        out = RL.llm_chat([{"role": "user", "content": prompt}], cfgobj, max_tokens=1600,
                          tag="ai_llm_augment", n_items=len(lines))
        if not out:
            continue
        m = re.search(r"\[.*\]", out, re.S)
        if not m:
            continue
        try:
            arr = json.loads(m.group(0))
        except Exception:
            continue
        con = _con()
        for o in arr:
            try:
                idx = int(o.get("idx"))
                key = key_by_idx.get(idx)
                if not key:
                    continue
                tag = str(o.get("tag", "")).strip().lower()
                if tag not in ("core", "extension", "method", "frontier", "irrelevant"):
                    tag = ""
                fit = str(o.get("fit", "")).strip().lower()
                if fit not in ("high", "medium", "low"):
                    fit = ""
                reason = (o.get("reason") or "")[:200]
                con.execute("UPDATE papers SET llm_tag=?, llm_reason=?, advisor_fit=? WHERE key=?",
                            (tag, reason, fit, key))
            except Exception:
                continue
        con.commit()
        con.close()


# ---------------------------------------------------------------- 下载
def _pdf_ok(path, title):
    """下到的 PDF 是否真是这篇文章（复用 download.pdf_matches 的内容校验）。
    校验模块不可用时放行。"""
    try:
        import download as D
        return bool(D.pdf_matches(path, (title or "").strip())[0])
    except Exception:
        return True


def download(cfgobj, week, items):
    """把 arXiv PDF 下到 week 文件夹对应分类子目录，回填 pdf_path，返回 pdf_map(key→相对路径)。

    默认只下高价值分类（顶会 CCF-A + 医学人工智能），通用 AI 只留在线链接，避免一次性下几百篇。
    落盘后做【内容校验】（pdf_matches）：内容不符的 PDF 直接丢弃，避免「标题对、点开是别人的文章」。
    可用 config.ai_board.download_cats / max_download 调整。"""
    folder = week_dir(week)
    pdf_map = {}
    if not (ai_cfg().get("download", True) and items):
        return pdf_map
    dcfg = ai_cfg()
    dl_cats = dcfg.get("download_cats") or ["A0", "A1"]
    max_dl = int(dcfg.get("max_download", 60))
    timeout = int((cfgobj.get("download") or {}).get("timeout", 25))
    n = 0
    for it in items:
        key = it["key"]
        # 已有 PDF：先校验内容，抽不出文本/内容不符的当作没有、重新下载（清掉失效链接）
        if it.get("pdf_path") and os.path.exists(it["pdf_path"]):
            if _pdf_ok(it["pdf_path"], it.get("title")):
                try:
                    pdf_map[key] = os.path.relpath(it["pdf_path"], folder)
                except Exception:
                    pass
                continue
            else:
                try:
                    os.remove(it["pdf_path"])
                except Exception:
                    pass
                set_pdf(key, "")
        if not it.get("pdf_url"):
            continue
        if it.get("cat") not in dl_cats:
            continue
        if n >= max_dl:
            continue
        sub = CAT_FOLDER.get(it.get("cat"), "2_通用AI大模型")
        d = os.path.join(folder, sub)
        os.makedirs(d, exist_ok=True)
        aid = re.sub(r"v\d+$", "", key.split(":", 1)[-1])
        dest = os.path.join(d, aid + ".pdf")
        try:
            req = urllib.request.Request(it["pdf_url"],
                                         headers={"User-Agent": "lit-monitor/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = r.read()
            with open(dest, "wb") as f:
                f.write(data)
            if os.path.getsize(dest) <= 1024:
                try:
                    os.remove(dest)
                except Exception:
                    pass
                continue
            # 内容校验：下到的必须真是这篇文章，否则丢弃（避免「标题对、点开是别人的文章」）
            if _pdf_ok(dest, it.get("title")):
                set_pdf(key, dest)
                pdf_map[key] = os.path.relpath(dest, folder)
                n += 1
            else:
                print("  [skip] PDF 内容与题录不符，已丢弃：%s" % ((it.get("title") or "")[:52]))
                try:
                    os.remove(dest)
                except Exception:
                    pass
        except Exception as e:
            print("  [skip] PDF 下载失败 %s : %r" % (aid, e))
        time.sleep(0.3)
    print("AI PDF 下载完成：%d 篇（仅 %s）" % (n, "/".join(dl_cats)))
    return pdf_map


# ---------------------------------------------------------------- 报告
def _body_grouped(items, pdf_map, pending=False):
    body = ""
    for c in CAT_ORDER:
        grp = [it for it in items if it.get("cat") == c]
        if not grp:
            continue
        grp.sort(key=lambda x: -(x.get("score") or 0))
        body += '<h2 class="gh" style="border-color:%s">%s <span class="count">%d 篇</span></h2>' \
                '<div class="grid">%s</div>' % (RL.cat_color(c), RL.cat_label(c), len(grp),
                                               "".join(RL.card_html(it, pdf_map.get(it["key"]), pending) for it in grp))
    return body


def _save_cn(batch):
    """把 enrich / ai_llm_augment 产出的中文评语、翻译、LLM 标签写回 ai.db（边翻边存）。"""
    con = _con()
    for it in batch:
        con.execute(
            "UPDATE papers SET note=?, cn_title=?, cn_authors=?, cn_aff=?, cn_abstract=?, "
            "cn_keywords=?, llm_tag=?, llm_reason=?, advisor_fit=?, core_hit=? WHERE key=?",
            (it.get("note") or "", it.get("cn_title") or "", it.get("cn_authors") or "",
             it.get("cn_aff") or "", it.get("cn_abstract") or "", it.get("cn_keywords") or "",
             it.get("llm_tag") or "", it.get("llm_reason") or "", it.get("advisor_fit") or "",
             it.get("core_hit") or "", it["key"]))
    con.commit()
    con.close()


def write_reports(week, items, pdf_map, cfgobj):
    """生成 AI 板块报告：0_ 一键打开 / 2_ 题录总表 / 3_ 待下载 / 1_ 综述。
    统一走 reportlib（AI_MODE 开关），与糖尿病侧完全同构：分组(A0/A1/A2) / 筛选栏 /
    中文评语+翻译门控 / 本周必读 / 前沿专栏 / 方法可迁移 / 趋势预警+跨文献关联。"""
    RL.set_ai_mode(True)
    try:
        folder = week_dir(week)
        os.makedirs(folder, exist_ok=True)
        items = [dict(it) for it in items]
        for it in items:
            # 确保字段齐全（老库可能没有新列，给默认值）
            it.setdefault("cat", "A2")
            it.setdefault("core_hit", "")
            it.setdefault("llm_tag", "")
            it.setdefault("llm_reason", "")
            it.setdefault("advisor_fit", "")
            it.setdefault("conf_hint", 0)
            it.setdefault("venue", "")
            it["_cat"] = it.get("cat") or "A2"

        # 0_ 一键打开（含翻译/评语，AI 门控：core_hit / conf_hint；边翻边存）
        RL.write_library(folder, week, items, pdf_map, cfgobj, save_cb=_save_cn)
        _save_cn(items)
        # 3_ 待下载清单（没下到的）
        fail = [it for it in items if not pdf_map.get(it["key"])]
        RL.write_pending(folder, week, fail, cfgobj)
        # 1_ 综述（本方向核心 + 研究/内容分组 + 必读 + 前沿 + 方法可迁移 + 趋势/关联）
        RL.write_review(folder, week, items, cfgobj, week=week)

        # 2_ 题录总表.csv（数据版，可导入 Zotero / Excel）
        import csv
        cpath = os.path.join(folder, F_AI_CSV)
        with open(cpath, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(["序号", "分类", "标题", "中文标题", "中文作者", "中文单位", "中文关键词",
                        "中文摘要", "作者", "期刊", "发表时间", "arXiv ID", "链接", "本地PDF",
                        "是否已下载", "本方向核心", "LLM标签", "贴合度"])
            for i, it in enumerate(items, 1):
                w.writerow([i, RL.cat_label(it["_cat"]), it.get("title", ""), it.get("cn_title", ""),
                            it.get("cn_authors", ""), it.get("cn_aff", ""), it.get("cn_keywords", ""),
                            it.get("cn_abstract", ""),
                            it.get("authors", ""), it.get("journal", ""), it.get("pub_date", ""),
                            it.get("doi", ""), it.get("url", ""),
                            pdf_map.get(it["key"]) or "", "是" if pdf_map.get(it["key"]) else "否",
                            it.get("core_hit", "") or "", it.get("llm_tag", "") or "",
                            it.get("advisor_fit", "") or ""])
    finally:
        RL.set_ai_mode(False)   # 防止污染后续糖尿病报告（同进程内）

    p1 = os.path.join(folder, F_AI_LIB)
    print("AI 文献报告：%s （%d 篇；PDF 已下载 %d 篇）" % (p1, len(items), len(pdf_map)))
    return p1


# ---------------------------------------------------------------- 邮件内嵌片段（自包含内联样式，不受主邮件筛选影响）
def _ai_card(it):
    col = RL.cat_color(it.get("cat") or "A2")
    title = RL.esc(it.get("title") or "")
    cn = RL.esc(it.get("cn_title") or "")
    cn_html = ('<div style="font-size:13px;color:#1A1A1A;margin:2px 0">%s</div>' % cn) if cn else ""
    note = RL.esc(it.get("note") or "")
    note_html = ('<div style="font-size:12.5px;color:#444;background:#FFF8E6;border-radius:6px;'
                 'padding:5px 8px;margin:4px 0">%s</div>' % note) if note else ""
    abs_cn = RL.esc(it.get("cn_abstract") or "")
    abs_html = ""
    if abs_cn:
        abs_html = ('<div style="font-size:12px;color:#444;max-height:160px;overflow:auto;'
                    'margin:4px 0">%s</div>' % abs_cn)
    conf_html = ""
    if it.get("conf_hint"):
        _v = RL.esc((it.get("venue") or "").strip() or RL.jcr.CONF_HINT_LABEL)
        conf_html = ('<span style="font-size:11px;color:#fff;background:%s;'
                     'padding:2px 8px;border-radius:6px;margin-left:6px">%s</span>'
                     % (RL.jcr.CONF_HINT_COLOR, _v))
    core_html = ""
    if it.get("core_hit"):
        core_html = ('<span style="font-size:11px;color:#fff;background:#B8860B;'
                     'padding:2px 8px;border-radius:6px;margin-left:6px">核心 · %s</span>'
                     % RL.esc(it.get("core_hit") or ""))
    links = ['<a href="%s" target="_blank" style="color:#0C447C;margin-right:10px">arXiv 原文</a>'
             % RL.esc(it.get("url") or "#")]
    if it.get("pdf_url"):
        links.append('<a href="%s" target="_blank" style="color:#fff;background:#2E7D32;'
                     'padding:2px 8px;border-radius:6px;text-decoration:none">PDF</a>' % RL.esc(it["pdf_url"]))
    return ('<div style="background:#fff;border:0.5px solid #E2E0D6;border-left:5px solid %s;'
            'border-radius:10px;padding:10px 12px;margin:8px 0">'
            '<span style="font-size:11px;color:#fff;background:%s;padding:2px 8px;border-radius:6px">%s</span>'
            '%s%s <span style="font-size:11px;color:#6B6A63">%s</span>'
            '<div style="display:block;font-size:14px;font-weight:600;color:#0C447C;margin:4px 0">%s</div>%s'
            '<div style="font-size:12px;color:#5F5E5A">%s</div>%s%s'
            '<div style="margin-top:6px">%s</div></div>'
            ) % (col, col, RL.cat_label(it.get("cat") or "A2"), conf_html, core_html,
                 RL.esc(it.get("journal") or ""), title, cn_html,
                 RL.esc(it.get("pub_date") or ""), note_html, abs_html, "".join(links))


def email_section(cfgobj):
    """返回可内嵌进周报邮件的 AI 板块 HTML 片段（自包含样式），无数据返回空串。"""
    if not ai_cfg().get("enabled"):
        return ""
    wk = latest_week()
    if not wk:
        return ""
    items = load_week(wk)
    if not items:
        return ""
    items.sort(key=lambda x: (CAT_ORDER.index(x.get("cat") or "A2") if (x.get("cat") or "A2") in CAT_ORDER else 9,
                              -(x.get("score") or 0)))
    counts = {c: sum(1 for it in items if it.get("cat") == c) for c in CAT_ORDER}
    stat = " · ".join("%s %d 篇" % (RL.cat_label(c), counts[c]) for c in CAT_ORDER)
    head = ('<h2 class="rev-h2">🤖 AI 文献板块（本周 · %s）</h2>'
            '<div class="rev-p">%s ｜ 共 %d 篇。完整交互版见本地 <b>%s</b></div>'
            % (RL.esc(wk), RL.esc(stat), len(items), RL.esc(F_AI_LIB)))
    body = ""
    for c in CAT_ORDER:
        grp = [it for it in items if it.get("cat") == c][:30]
        if not grp:
            continue
        body += ('<h3 style="font-size:15px;margin:14px 0 4px;color:%s">%s '
                 '<span style="font-size:12px;color:#888">%d 篇</span></h3>'
                 % (RL.cat_color(c), RL.cat_label(c), len(grp)))
        body += "".join(_ai_card(it) for it in grp)
    return head + body


# ---------------------------------------------------------------- 主流程
def run(no_llm=False, no_download=False):
    c = cfg()
    ac = ai_cfg()
    if not ac.get("enabled"):
        print("ai_board 未启用，跳过。")
        return
    if no_llm:
        c.setdefault("llm", {})["enabled"] = False
        print("[no-llm] 跳过大模型翻译/评语，仅做抓取+下载+英文报告")
    if no_download:
        print("[no-download] 跳过 PDF 下载，仅抓取+报告（本地 PDF 留待后续或下次排程）")
    days = int(ac.get("lookback_days", 7))
    cap = int(ac.get("max_per_run", 150))
    conf_days = int(ac.get("conf_lookback_days", 120))
    conf_cap = int(ac.get("conf_cap", 200))
    week = week_folder(days)
    print("=== AI 文献板块 · %s ===" % week)

    # 1) 抓取：A1/A2 走 7 天新鲜流；A0 顶会走更宽的会议关键词窗口
    #    检索前先做「LLM 扩词」(RAG 召回增强)：并入 AI 画像静态扩词 + 上周反馈词，提升召回
    extra = _ai_expanded_terms(c)
    if extra:
        print("AI 扩词（RAG 召回增强）：并入 %d 个扩展检索词" % len(extra))
    raw = []
    if ac.get("sources", {}).get("arxiv", True):
        raw += fetch_arxiv(days, cap=max(cap, 200), query=_arxiv_query(extra))    # 新鲜 AI 论文
        time.sleep(3)                                          # 错开两次 arXiv 请求，避免 429
        raw += fetch_arxiv(conf_days, cap=conf_cap, query=_conf_query(extra))     # 顶会录用/相机就绪
    if ac.get("sources", {}).get("pubmed_medical_ai", True):
        # 真正见刊的顶刊文章（Nature MI / Lancet DH / JAMIA / npj DM / MedIA / TMI ...）
        pm = fetch_pubmed_ai(days=max(days, int(ac.get("journal_lookback_days", 14))),
                             cap=100, extra_terms=extra)
        print("PubMed 顶刊源：%d 篇" % len(pm))
        raw += pm
    # 按 key 去重（同一篇可能同时落在两个流）
    seen = {}
    for it in raw:
        seen[it["key"]] = it
    raw = list(seen.values())
    items = categorize(raw)
    if not items:
        print("本期没有匹配的 AI 文献。")
        return
    for it in items:
        it["week"] = week
    for it in items:
        save(it)
    week_items = load_week(week)
    print("抓取 + 分类完成：%d 篇（原始 %d）" % (len(items), len(raw)))

    # 1.2) 检索闭环（反馈环）：用本周实际入库的 AI 文献，反推下周该补搜的检索词，写入 AI 独立缓存。
    #      与糖尿病侧 monitor 的检索闭环同构；LLM 不可用时自动跳过，不阻断主流程。
    try:
        if week_items:
            _n = queryx.suggest_and_store(c, _ai_profile(), week_items, week, domain="ai")
            if _n:
                print("检索闭环：基于本周 %d 篇生成 %d 个下周检索词（已写入 AI 缓存）" % (len(week_items), _n))
    except Exception as e:
        print("[warn] 检索闭环生成失败（跳过，不阻断主流程）：%r" % e)

    # 1.5) 大模型增强判定（core/extension/method/frontier + 本方向贴合度），写回 ai.db。
    # 与糖尿病侧 screen.llm_augment 等价：默认只判「非核心」(llm_tag 为空) 的条目，
    # 命中本方向核心的条目 categorize 已置 llm_tag='core' 并跳过，避免浪费额度。
    # 无大模型时 ai_llm_augment 静默返回（categorize 已给默认 tag/fit）。
    ai_llm_augment(week_items, c)
    week_items = load_week(week)  # 重新读入增强后的字段，供后续报告使用

    # 2) 先出报告（在线链接，立即可看），再下 PDF（耗时），下完刷新本地按钮
    #    首次出图也要带上「磁盘上已有」的 PDF，否则重跑时会把已下好的本地按钮清零
    def _existing_map(items):
        m = {}
        for it in items:
            p = it.get("pdf_path") or ""
            if p and os.path.exists(p):
                try:
                    m[it["key"]] = os.path.relpath(p, week_dir(week))
                except Exception:
                    m[it["key"]] = p
        return m

    write_reports(week, week_items, _existing_map(week_items), c)
    if not no_download:
        pdf_map = download(c, week, week_items)
        if pdf_map:
            merged = _existing_map(load_week(week))
            merged.update(pdf_map)
            write_reports(week, week_items, merged, c)


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    a = sys.argv[1:]
    if "fetch" in a:
        c = cfg(); ac = ai_cfg(); days = int(ac.get("lookback_days", 7))
        week = week_folder(days)
        extra = _ai_expanded_terms(c)
        if extra:
            print("AI 扩词（RAG 召回增强）：并入 %d 个扩展检索词" % len(extra))
        raw = []
        raw += fetch_arxiv(days, cap=max(int(ac.get("max_per_run", 150)), 200), query=_arxiv_query(extra))
        raw += fetch_arxiv(int(ac.get("conf_lookback_days", 120)),
                           cap=int(ac.get("conf_cap", 200)), query=_conf_query(extra))
        seen = {}
        for it in raw:
            seen[it["key"]] = it
        items = categorize(list(seen.values()))
        for it in items:
            it["week"] = week; save(it)
        ai_llm_augment(items, c)
        try:
            if items:
                _n = queryx.suggest_and_store(c, _ai_profile(), items, week, domain="ai")
                if _n:
                    print("检索闭环：基于本周 %d 篇生成 %d 个下周检索词" % (len(items), _n))
        except Exception as e:
            print("[warn] 检索闭环生成失败（跳过）：%r" % e)
        print("fetch 完成：%d 篇" % len(items))
    elif "report" in a:
        c = cfg(); week = latest_week()
        if week:
            if "--no-llm" in a:
                c.setdefault("llm", {})["enabled"] = False
            its = load_week(week)
            pdf_map = {it["key"]: os.path.relpath(it["pdf_path"], week_dir(week))
                       for it in its if it.get("pdf_path") and os.path.exists(it["pdf_path"])}
            write_reports(week, its, pdf_map, c)
    elif "download" in a:
        c = cfg(); week = latest_week()
        if week:
            its = load_week(week)
            download(c, week, its)
    elif "recat" in a:
        recat_all(regenerate=("--no-report" not in a))
    elif "augment" in a:
        augment_all()
    else:
        run(no_llm=("--no-llm" in a), no_download=("--no-download" in a))


if __name__ == "__main__":
    main()
