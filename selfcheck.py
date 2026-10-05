#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
月度自检 —— 检查文献监控系统自身的健康度，并邮件报告问题

检查项:
  1. config.json 是否合法、关键项是否缺失（mail.password / papers_dir）
  2. 期刊分级数据是否就绪（jcr.csv / journals.csv）
  3. 数据库是否存在、最近一次抓取是哪周、库内总量
  4. 各数据源开关是否打开
  5. 外部 API 是否可达（PubMed esearch / OpenAlex 各试一次）
  6. 上次运行距今是否过久（超过 10 天就告警）

报告默认打印，并（在 mail.password 已填时）发到邮箱。
用法:
  python selfcheck.py            # 自检 + 邮件报告
  python selfcheck.py --no-mail  # 只打印不发
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from datetime import date, timedelta

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

import profile as P   # noqa: E402
import jcr            # noqa: E402


def line(t):
    print(t)


def check_reachability():
    """各试一次，返回 [(源, ok, 耗时秒, 说明)]"""
    out = []
    # PubMed esearch（最轻量）
    try:
        import urllib.request, urllib.parse
        ak = (P.cfg().get("pubmed") or {}).get("api_key", "")
        u = ("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?db=pubmed&term=diabetes"
             "&retmode=json&retmax=1&api_key=%s" % ak)
        t = time.time()
        with urllib.request.urlopen(u, timeout=25) as r:
            d = json.loads(r.read())
        ok = bool(d.get("esearchresult", {}).get("idlist"))
        out.append(("PubMed", ok, round(time.time() - t, 1), "esearch 正常" if ok else "返回空"))
    except Exception as e:
        out.append(("PubMed", False, 0, repr(e)[:80]))
    # OpenAlex
    try:
        import urllib.request
        u = "https://api.openalex.org/works?filter=title_and_abstract.search:diabetes&per-page=1&mailto=user@example.com"
        t = time.time()
        with urllib.request.urlopen(u, timeout=25) as r:
            d = json.loads(r.read())
        ok = bool(d.get("results"))
        out.append(("OpenAlex", ok, round(time.time() - t, 1), "正常" if ok else "返回空"))
    except Exception as e:
        out.append(("OpenAlex", False, 0, repr(e)[:80]))
    return out


def check_ai_board(cfg, issues, warns):
    """AI 文献板块（ai.db）自检：配置 / 库健康 / PDF 内容一致性 / 分类字段覆盖度。
    与 [3] 对 papers.db 的检查对称，保证 AI 板块也纳入月度体检。"""
    import re as _re
    line("\n[7] AI 文献板块（ai_board / ai.db）")
    ac = (cfg.get("ai_board") or {})
    if not ac.get("enabled"):
        line("  · 未启用（enabled=false），跳过")
        return
    for k in ("root", "folder"):
        if not (ac.get(k) or "").strip():
            warns.append("ai_board.%s 为空 → AI 板块落盘路径异常。" % k)
            line("  ⚠ ai_board.%s 为空" % k)
    ai_db = os.path.join(BASE, "ai.db")
    if not os.path.exists(ai_db):
        warns.append("ai.db 不存在 → AI 板块还没跑过。运行 python aiboard.py 跑一次。")
        line("  ⚠ ai.db 不存在（尚未抓取）")
        return
    try:
        import download as D
        con = sqlite3.connect(ai_db)
        con.row_factory = sqlite3.Row
        cols = {r[1] for r in con.execute("PRAGMA table_info(papers)")}
        n = con.execute("SELECT count(*) FROM papers").fetchone()[0]
        wk = con.execute("SELECT max(week) FROM papers").fetchone()[0]
        line("  ✓ 库内 %d 条，最近一周 = %s" % (n, wk))
        # AI 板块的 week 是「2026-W40 (09-28~10-04)」周标签，用 ISO 周折算距今天数
        if wk:
            m = _re.search(r"(\d{4})-W(\d{2})", wk or "")
            last = None
            if m:
                try:
                    last = date.fromisocalendar(int(m.group(1)), int(m.group(2)), 1)
                except Exception:
                    last = None
            if last is not None:
                gap = (date.today() - last).days
                if gap > 14:
                    warns.append("AI 板块距离上次抓取已 %d 天，流水线可能停了。" % gap)
                    line("  ⚠ 距上次抓取 %d 天" % gap)
                else:
                    line("  ✓ 上次抓取 %d 天前" % gap)
        # 分类字段覆盖度（防止改版前老库缺 core_hit/llm_tag 等列）
        for col in ("core_hit", "tier_label", "llm_tag", "advisor_fit"):
            if col not in cols:
                warns.append("ai.db 缺列 %s → 旧库未迁移，跑 python aiboard.py recat 补。" % col)
                line("  ⚠ 缺列 %s" % col)
        if "core_hit" in cols and n:
            cnt = con.execute("SELECT count(*) FROM papers WHERE core_hit IS NOT NULL AND core_hit<>''").fetchone()[0]
            line("  · 命中本方向核心(core_hit) %d/%d 篇 (%.0f%%)" % (cnt, n, 100.0 * cnt / max(n, 1)))
        # PDF 内容一致性 + 死链
        tot = miss = orphan = 0
        for r in con.execute("SELECT key, title, pdf_path FROM papers WHERE pdf_path<>'' AND pdf_path IS NOT NULL"):
            p = r["pdf_path"]
            if not p:
                continue
            if not os.path.exists(p):
                orphan += 1
                continue
            tot += 1
            ok, _ = D.pdf_matches(p, r["title"] or "")
            if not ok:
                miss += 1
        con.close()
        if miss:
            issues.append("AI 板块本地 PDF 有 %d 个内容与题录不符 → 报告里会显示错的文章。"
                          "跑 python aiboard.py download 会自动重下/剔除。" % miss)
            line("  ✗ 本地 PDF %d/%d 个内容不符" % (miss, tot))
        elif tot:
            line("  ✓ 本地 PDF %d 个内容与题录相符" % tot)
        else:
            line("  · 暂无已下载 PDF")
        if orphan:
            warns.append("AI 板块有 %d 条 pdf_path 指向不存在的文件（挪过目录？跑 aiboard.py download 重链）。" % orphan)
            line("  ⚠ %d 条 pdf_path 失效" % orphan)
    except Exception as e:
        issues.append("AI 板块自检失败: %s" % e)
        line("  ✗ 自检失败: %s" % e)


def run():
    cfg = P.cfg()
    issues, warns = [], []

    line("=" * 60)
    line("文献监控 月度自检  %s" % date.today().isoformat())
    line("=" * 60)

    # 1) config
    line("\n[1] 配置 config.json")
    if not cfg.get("mail", {}).get("password"):
        warns.append("mail.password 为空 → 周报/提醒邮件都发不出去。需填 QQ 邮箱 16 位授权码。")
        line("  ⚠ mail.password 未填（邮件不会发）")
    else:
        line("  ✓ mail.password 已填")
    pd = (cfg.get("paths") or {}).get("papers_dir") or ""
    if not pd:
        warns.append("paths.papers_dir 为空 → 不会下载 PDF，只出报告。")
        line("  ⚠ papers_dir 为空（不下载 PDF）")
    else:
        try:
            os.makedirs(pd, exist_ok=True)
            tmp = os.path.join(pd, ".writetest")
            with open(tmp, "w") as f:
                f.write("ok")
            os.remove(tmp)
            line("  ✓ papers_dir 可写: %s" % pd)
        except Exception as e:
            issues.append("papers_dir 不可写: %s (%s)" % (pd, e))
            line("  ✗ papers_dir 不可写: %s" % e)

    # 2) 期刊数据
    line("\n[2] 期刊分级数据")
    jcr_csv = os.path.join(BASE, "jcr.csv")
    jn_csv = os.path.join(BASE, "journals.csv")
    if os.path.exists(jcr_csv):
        st = jcr.stats()
        line("  ✓ jcr.csv 已加载: %s" % st)
    else:
        issues.append("jcr.csv 缺失 → 期刊分级失效，全部判为未分级。需运行 build_jcr.py 重新生成。")
        line("  ✗ jcr.csv 缺失")
    if not os.path.exists(jn_csv):
        warns.append("journals.csv 缺失（人工期刊提级表）。可补，不致命。")
        line("  ⚠ journals.csv 缺失")

    # 3) 数据库
    line("\n[3] 数据库 papers.db")
    dbp = os.path.join(BASE, "data", "papers.db")
    if not os.path.exists(dbp):
        warns.append("data/papers.db 不存在 → 还没跑过抓取。运行 python run.py 跑一次。")
        line("  ⚠ 数据库不存在（尚未抓取）")
    else:
        con = sqlite3.connect(dbp)
        try:
            n = con.execute("SELECT count(*) FROM papers").fetchone()[0]
            row = con.execute("SELECT max(week) FROM papers").fetchone()[0]
            line("  ✓ 库内 %d 条，最近一周 = %s" % (n, row))
            if row:
                last = date.fromisoformat(row)
                gap = (date.today() - last).days
                if gap > 10:
                    issues.append("距离上次抓取已 %d 天，流水线可能停了。" % gap)
                    line("  ✗ 距上次抓取 %d 天（疑似停摆）" % gap)
                else:
                    line("  ✓ 上次抓取 %d 天前" % gap)
        except Exception as e:
            issues.append("数据库读取失败: %s" % e)
            line("  ✗ 读取失败: %s" % e)
        con.close()

    # 4) 数据源开关
    line("\n[4] 数据源开关")
    srcs = cfg.get("sources") or {}
    on = [k for k, v in srcs.items() if v and not str(k).startswith("_")]
    line("  已开启: %s" % (", ".join(on) or "（无）"))
    if not on:
        issues.append("所有数据源都关了 → 抓不到任何文献。")
    if not srcs.get("pubmed"):
        warns.append("PubMed 被关 → 生物医学核心来源缺失，强烈建议保持开启。")

    # 5) 外部可达性
    line("\n[5] 外部 API 可达性")
    for name, ok, sec, msg in check_reachability():
        if ok:
            line("  ✓ %-9s %.1fs  %s" % (name, sec, msg))
        else:
            issues.append("%s 不可达: %s" % (name, msg))
            line("  ✗ %-9s %s" % (name, msg))

    # 6) 题录一致性：DOI 有没有被"参考文献 DOI"污染（2026-10-04/05 事故的哨兵），
    #    本地 PDF 内容有没有和题录对不上（报告里会显示成"已下载"却打开是别人的文章）。
    line("\n[6] 题录一致性（DOI / 本地 PDF）")
    try:
        import verify_dois
        bad = verify_dois.scan(verbose=False)
        if bad:
            issues.append("发现 %d 条记录的 DOI 与 PubMed 不符（被参考文献 DOI 覆盖过）。"
                          "运行 python verify_dois.py --fix 修正。" % len(bad))
            line("  ✗ DOI 与 PubMed 不符 %d 条（跑 verify_dois.py --fix 修）" % len(bad))
        else:
            line("  ✓ PubMed 来源的 DOI 与 NCBI 逐条一致")
    except Exception as e:
        warns.append("DOI 体检没跑成: %r" % (e,))
        line("  ⚠ DOI 体检失败: %r" % (e,))
    if os.path.exists(dbp):
        try:
            import download as D
            con = sqlite3.connect(dbp)
            con.row_factory = sqlite3.Row
            tot = miss = orphan = 0
            for r in con.execute("SELECT key, title, pdf_path FROM papers WHERE pdf_path<>''"):
                p = r["pdf_path"]
                if not p:
                    continue
                if not os.path.exists(p):
                    orphan += 1
                    continue
                tot += 1
                ok, _ = D.pdf_matches(p, r["title"] or "")
                if not ok:
                    miss += 1
            con.close()
            if miss:
                issues.append("本地 PDF 有 %d 个内容与题录不符 → 报告里会显示错的文章。"
                              "跑 python download.py report 会自动剔除。" % miss)
                line("  ✗ 本地 PDF %d/%d 个内容不符" % (miss, tot))
            elif tot:
                line("  ✓ 本地 PDF %d 个内容与题录全部相符" % tot)
            else:
                line("  · 暂无已下载 PDF")
            if orphan:
                warns.append("有 %d 条 pdf_path 指向不存在的文件（挪过目录？跑 download.py report 会自动重链）。" % orphan)
                line("  ⚠ %d 条 pdf_path 失效（会被自动重链）" % orphan)
        except Exception as e:
            warns.append("PDF 内容体检失败: %r" % (e,))
            line("  ⚠ PDF 体检失败: %r" % (e,))

    # 7) AI 文献板块（ai.db）
    check_ai_board(cfg, issues, warns)

    # 汇总
    line("\n" + "=" * 60)
    line("结论")
    line("=" * 60)
    if not issues and not warns:
        line("  ✓ 一切正常，无需处理。")
    else:
        if issues:
            line("  必须处理（%d 项）:" % len(issues))
            for i in issues:
                line("    • %s" % i)
        if warns:
            line("  建议处理（%d 项）:" % len(warns))
            for w in warns:
                line("    • %s" % w)
    line("=" * 60)

    report = "文献监控月度自检 %s\n\n" % date.today().isoformat()
    report += "必须处理:\n" + ("\n".join("• " + i for i in issues) if issues else "（无）") + "\n\n"
    report += "建议处理:\n" + ("\n".join("• " + w for w in warns) if warns else "（无）") + "\n"

    if "--no-mail" not in sys.argv:
        m = dict(cfg.get("mail") or {})   # 2026-10-05 修：这里原来写的是 cfg()，dict 不可调用 → 自检邮件必崩
        if m.get("password"):
            try:
                import smtplib, ssl
                from email.header import Header
                from email.mime.text import MIMEText
                from email.utils import formataddr, formatdate
                host = m.get("smtp_host"); port = int(m.get("smtp_port") or 465)
                body = report
                msg = MIMEText(body, "plain", "utf-8")
                msg["Subject"] = Header("%s 月度自检报告" % (m.get("subject_prefix") or "[文献周报]"), "utf-8")
                msg["From"] = formataddr((str(Header(m.get("from_name") or "文献监控", "utf-8")), m.get("username", "")))
                msg["To"] = ", ".join(m.get("to", []))
                msg["Date"] = formatdate(localtime=True)
                ctx = ssl.create_default_context()
                with (smtplib.SMTP_SSL(host, port, context=ctx, timeout=45) if (m.get("use_ssl", True) and port == 465)
                      else smtplib.SMTP(host, port, timeout=45)) as srv:
                    srv.login(m.get("username", ""), m.get("password", ""))
                    srv.sendmail(m.get("username", ""), m.get("to", []), msg.as_string())
                line("\n√ 自检报告已发到邮箱")
            except Exception as e:
                line("\n× 发邮件失败: %r" % (e,))
        else:
            line("\n（mail.password 未填，跳过邮件；报告已打印在上文）")
    return 0 if not issues else 1


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.exit(run())
