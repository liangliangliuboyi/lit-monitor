#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
清洗浏览器导出的 Netscape cookies，让 Python 能读。

为什么需要这个：
  用 "Get cookies.txt" 之类的扩展导出后，常有大量行写成
      .example.com  FALSE  /  ...
  即「域名带前导点」但第 2 列（domain_specified）却是 FALSE。
  Python 的 http.cookiejar.MozillaCookieJar 有一条断言
      domain_specified == initial_dot
  不满足就直接抛 AssertionError: invalid Netscape format cookies file，
  **整个文件一条都读不进来**（不是跳过坏行，是全崩）。
  实测某次导出 1361 行里有 1127 行是这种，不修完全没法用。

用法：
  python fix_cookies.py <导出的cookies.txt> [输出路径]
  python fix_cookies.py "E:/b浏览器/cookies_all.txt"          # 默认输出 ./cookies_sjtu.txt
  python fix_cookies.py a.txt b.txt --merge c.txt             # 多文件合并去重

它还会：
  - 合并多个导出文件（按 domain+name 去重，先出现的优先）
  - 报告过期条数、是否含关键登录态（jaccount / shib / WoS / 图书馆）
"""

import http.cookiejar as cj
import os
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))

# 值得确认是否存在的登录态关键词（按你的机构改）
KEY_HINTS = [
    ("交大 jAccount 登录态", "jaccount"),
    ("Shibboleth 会话", "shib"),
    ("Web of Science", "webofscience"),
    ("Clarivate", "clarivate"),
    ("图书馆", "lib.sjtu"),
    ("Elsevier/ScienceDirect", "sciencedirect"),
    ("Wiley", "wiley"),
    ("OUP", "oup.com"),
    ("Springer/Nature", "springer"),
]


def normalize(lines):
    """修正「前导点 + FALSE」这种不合规行，返回 (清洗后行列表, 修正行数)。"""
    out, fixed = [], 0
    for ln in lines:
        s = ln.rstrip("\r\n")
        if not s.strip() or s.lstrip().startswith("#"):
            out.append(s)
            continue
        f = s.split("\t")
        if len(f) < 7:
            continue                      # 残缺行直接丢
        if f[0].startswith(".") and f[1].upper() != "TRUE":
            f[1] = "TRUE"
            fixed += 1
        out.append("\t".join(f))
    return out, fixed


def merge_files(paths):
    """多文件合并，按 (domain, name) 去重，先出现的优先。"""
    seen, order, total_fixed = {}, [], 0
    for p in paths:
        with open(p, encoding="utf-8", errors="replace") as fh:
            lines, fixed = normalize(fh.read().splitlines())
        total_fixed += fixed
        for ln in lines:
            if not ln.strip() or ln.lstrip().startswith("#"):
                continue
            f = ln.split("\t")
            if len(f) < 7:
                continue
            k = (f[0], f[5])
            if k not in seen:
                order.append(k)
            seen[k] = ln
        print("  读入 %-45s %5d 行（修正 %d）" % (os.path.basename(p), len(lines), fixed))
    body = [seen[k] for k in order]
    return body, total_fixed


def report(dst):
    print("\n校验结果：")
    try:
        c = cj.MozillaCookieJar()
        c.load(dst, ignore_discard=True, ignore_expires=True)
    except Exception as e:
        print("  ✗ 仍然读不进来：%r" % e)
        return
    now = time.time()
    live = [x for x in c if x.expires is None or x.expires > now]
    print("  可加载 %d 条，其中未过期 %d 条" % (len(c), len(live)))
    print("  关键登录态检查：")
    for label, kw in KEY_HINTS:
        n = len([x for x in c if kw in x.domain.lower()])
        flag = "✓" if n else "·"
        print("    %s %-22s %d 条" % (flag, label, n))
    print("\n提示：jaccount / shib 是会话级 cookie，会过期；"
          "每次跑之前重新导出并跑一次本脚本即可。")


def main():
    flags = [a for a in sys.argv[1:] if a.startswith("--")]
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    merge = "--merge" in flags
    if not args:
        print(__doc__)
        return 1
    # 输出路径：优先 -o；否则单文件时可用第 2 个位置参数；
    # 合并模式下所有位置参数都是输入源，绝不能把某个源文件当输出（会覆盖用户的导出文件！）
    if "-o" in flags:
        dst = args[0] if False else sys.argv[sys.argv.index("-o") + 1]
        paths = args
    elif merge or len(args) == 1:
        dst = os.path.join(BASE, "cookies_sjtu.txt")
        paths = args
    else:
        dst = args[1]
        paths = args[:1]

    print("清洗 cookies → %s" % dst)
    body, fixed = merge_files(paths)
    header = ["# Netscape HTTP Cookie File",
              "# sanitized by fix_cookies.py @ %s" % time.strftime("%Y-%m-%d %H:%M")]
    with open(dst, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(header + body) + "\n")
    print("\n共修正 %d 行格式错误，写出 %d 条 → %s" % (fixed, len(body), dst))
    report(dst)
    return 0


if __name__ == "__main__":
    sys.exit(main())
