#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
邮件推送 —— 把本周周报发到邮箱

纯标准库（smtplib / email），不装任何第三方包，搬走就能用。

准备（以 QQ 邮箱为例，其他邮箱同理）:
  1. 登录 QQ 邮箱网页版 → 设置 → 账号 → 开启「IMAP/SMTP服务」
  2. 按提示发短信，拿到一串 16 位「授权码」——注意不是登录密码
  3. 把授权码填进 config.json 的 mail.password

常用 SMTP:
  QQ      smtp.qq.com      465(SSL) / 587(STARTTLS)
  163     smtp.163.com     465(SSL)
  Gmail   smtp.gmail.com   587(STARTTLS)
  交大    看学校邮箱说明，一般是 smtp.sjtu.edu.cn

用法:
  python mail.py              # 发本周周报
  python mail.py --to user@example.com # 临时换个收件人
  python mail.py --dry-run    # 只打印要发的内容，不真发
"""
from __future__ import annotations

import os
import smtplib
import ssl
import sys
from datetime import date
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import profile as P  # noqa: E402

BASE = os.path.dirname(os.path.abspath(__file__))


def latest_report(outdir):
    """找最近一份周报（md 与 html 成对）"""
    if not os.path.isdir(outdir):
        return None, None
    weeks = sorted({f[:-3] for f in os.listdir(outdir) if f.endswith(".md")}, reverse=True)
    for w in weeks:
        md = os.path.join(outdir, w + ".md")
        html = os.path.join(outdir, w + ".html")
        if os.path.exists(md):
            return md, (html if os.path.exists(html) else None)
    return None, None


def build_subject(cfg, week):
    prefix = (cfg.get("subject_prefix") or "[文献周报]").strip()
    return "%s %s" % (prefix, week)


def send(cfg, subject, html_body, plain_body, dry_run=False):
    host = cfg.get("smtp_host") or ""
    port = int(cfg.get("smtp_port") or 465)
    user = cfg.get("username") or ""
    pwd = cfg.get("password") or ""
    to = cfg.get("to") or []

    if dry_run:
        print("[dry-run] 将发送到: %s" % ", ".join(to))
        print("[dry-run] 主题: %s" % subject)
        print("[dry-run] 正文 %d 字符（HTML）" % len(html_body))
        return True

    if not host or not user or not pwd:
        print("× 邮件未配置：config.json 的 mail 段需要 smtp_host / username / password（授权码）")
        return False
    if not to:
        print("× 没有收件人：config.json 的 mail.to")
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = formataddr((str(Header(cfg.get("from_name") or "文献监控", "utf-8")), user))
    msg["To"] = ", ".join(to)
    msg["Date"] = formatdate(localtime=True)
    msg.attach(MIMEText(plain_body or "", "plain", "utf-8"))
    if html_body:
        msg.attach(MIMEText(html_body, "html", "utf-8"))

    ctx = ssl.create_default_context()
    try:
        if cfg.get("use_ssl", True) and port == 465:
            srv = smtplib.SMTP_SSL(host, port, context=ctx, timeout=45)
        else:
            srv = smtplib.SMTP(host, port, timeout=45)
            srv.ehlo()
            try:
                srv.starttls(context=ctx)
                srv.ehlo()
            except smtplib.SMTPException:
                pass
        with srv:
            srv.login(user, pwd)
            srv.sendmail(user, to, msg.as_string())
        return True
    except Exception as e:
        print("× 发送失败: %r" % (e,))
        return False


def main():
    args = sys.argv[1:]
    sys.path.insert(0, BASE)
    from profile import cfg
    c = cfg()
    mcfg = dict(c.get("mail") or {})
    if "--to" in args:
        i = args.index("--to")
        mcfg["to"] = [args[i + 1]] if i + 1 < len(args) else mcfg.get("to", [])

    outdir = os.path.join(BASE, c.get("output", {}).get("dir", "reports"))
    md, html = latest_report(outdir)
    if not md:
        print("× 没找到周报，先跑 python monitor.py report")
        return 1
    week = os.path.basename(md)[:-3]
    with open(md, encoding="utf-8") as f:
        plain = f.read()
    html_body = ""
    if html and (c.get("output", {}).get("html", True)):
        with open(html, encoding="utf-8") as f:
            html_body = f.read()

    subject = build_subject(mcfg, week)

    # AI 文献板块：随周报一起推送（内嵌到邮件正文末尾）
    ai_sec = ""
    try:
        import aiboard
        ai_sec = aiboard.email_section(c)
        if ai_sec and html_body:
            html_body = html_body.replace("</body>", ai_sec + "</body>", 1)
            plain += "\n\n【AI 文献板块】本周已收录，见邮件正文末节，或本地 AI文献/ 文件夹（0_人工智能文献.html）。\n"
    except Exception as e:
        print("[warn] AI 板块内嵌失败（不影响主邮件）：%r" % (e,))

    ok = send(mcfg, subject, html_body, plain, dry_run="--dry-run" in args)
    if ok and "--dry-run" not in args:
        print("√ 已发送：%s → %s" % (subject, ", ".join(mcfg.get("to", []))))
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.exit(main())
