#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
周日晚提醒邮件 —— 告诉用户明天(周一)03:00 会自动跑文献监控

纯标准库，复用 config.json 的 mail 段（与 mail.py 同一套 SMTP 配置）。
邮件内容：提醒明日自动运行、简报晚间到达、以及手动触发命令。

用法:
  python remind.py            # 发提醒邮件
  python remind.py --dry-run # 只打印内容不发
"""
from __future__ import annotations

import os
import smtplib
import ssl
import sys
from datetime import date, timedelta

from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate

BASE = os.path.dirname(os.path.abspath(__file__))


def send(cfg, subject, body, dry_run=False):
    host = cfg.get("smtp_host") or ""
    port = int(cfg.get("smtp_port") or 465)
    user = cfg.get("username") or ""
    pwd = cfg.get("password") or ""
    to = cfg.get("to") or []
    if dry_run:
        print("[dry-run] 将发送至: %s" % ", ".join(to))
        print("[dry-run] 主题: %s" % subject)
        print(body)
        return True
    if not host or not user or not pwd:
        print("× 邮件未配置：config.json 的 mail 段需要 smtp_host / username / password（授权码）")
        return False
    if not to:
        print("× 没有收件人")
        return False
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = formataddr((str(Header(cfg.get("from_name") or "文献监控", "utf-8")), user))
    msg["To"] = ", ".join(to)
    msg["Date"] = formatdate(localtime=True)
    ctx = ssl.create_default_context()
    try:
        if cfg.get("use_ssl", True) and port == 465:
            srv = smtplib.SMTP_SSL(host, port, context=ctx, timeout=45)
        else:
            srv = smtplib.SMTP(host, port, timeout=45)
            srv.ehlo()
            try:
                srv.starttls(context=ctx); srv.ehlo()
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
    sys.path.insert(0, BASE)
    from profile import cfg
    m = dict(cfg().get("mail") or {})
    dry = "--dry-run" in sys.argv
    d = date.today() + timedelta(days=1)
    body = (
        "<YOUR NAME>同学你好，\n\n"
        "提醒一下：文献自动监控流水线已安排在【明天（%s，周一）凌晨 03:00】自动运行。\n"
        "你不需要做任何操作，流水线会自己完成：抓取本周新文献 → 分层筛选 → 生成简报 → "
        "下载可获取的开放获取 PDF → 晚上把简报发到你这个邮箱。\n\n"
        "如果你希望自己手动触发（比如临时想提前跑），在 lit-monitor 目录执行：\n"
        "    cd E:\\workbuddyplace\\lit-monitor\n"
        "    python run.py\n"
        "只想要简报不下载的话：python run.py --no-download\n\n"
        "保持电脑开机、连着网即可。若有异常，月度自检报告会单独提醒你。\n\n"
        "—— 文献监控 lit-monitor" % d.isoformat()
    )
    ok = send(m, "%s 提醒：明日(周一)03:00 自动跑文献监控" % (m.get("subject_prefix") or "[文献周报]"), body, dry_run=dry)
    if ok and not dry:
        print("√ 已发送周二提醒邮件")
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.exit(main())
