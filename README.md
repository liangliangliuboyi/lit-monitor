# lit-monitor · 领域文献智能监测与情报生成流水线

一套**零第三方依赖**的 Python 文献监测系统：每周自动从 PubMed / OpenAlex / 预印本等来源抓取
指定领域的新文献，经「规则粗筛 + 大模型语义判定」分层筛选，产出可直接阅读的中文情报周报，
并推送邮件。

> 本项目为研究用途开源。默认提供**模板画像**，请按自己的研究方向配置后再运行。

---

## 1 它解决什么问题

- **文献过载**：一个方向每周新增数百至上千篇，人工浏览既不现实也不可靠。
- **通用工具不够用**：数据库提醒只做关键词布尔匹配，假阳性高，且**不区分"对某个研究团队是否重要"**。
- **长期无人值守**：一次性的检索脚本 ≠ 可持续运行的服务（要处理限流、断点、静默错误、数据一致性）。

## 2 设计要点

| 层 | 做法 |
|---|---|
| 采集 | PubMed（E-utilities，翻页防截断）、OpenAlex、bioRxiv / medRxiv、arXiv、RSS、导师组作者追踪 |
| 分层判据 | ① 关键词层（core / proxy / eco / exclude）② **团队方向贴合度** ③ 大模型语义标签 |
| 调度 | **规则先粗筛、大模型只补语义**；门控决定"这条值不值得花一次调用"；限流下自动冷却 / 熔断 / 调批次 |
| 可信度 | 下载后**强制内容校验**、路径自愈、同周重跑幂等、字段一致性校验 |
| 输出 | 一键打开链接.html / 本周综述.html / 题录总表.csv / 待下载清单.html + 邮件推送 |
| 自检 | `selfcheck.py` 对库量、字段覆盖、PDF 一致性、死链做周期体检 |

## 3 快速开始

```bash
python --version           # 需要 >= 3.8，无需 pip install 任何包
cp config.example.json config.json      # 填入你自己的密钥与路径
cp profile.example.md profile.md        # 改成你的研究方向（不用改代码）
python run.py                           # 一条龙：抓取 → 分层 → 下载 → 出报告 → 发信
```

单项运行：
```bash
python monitor.py fetch        # 只抓取
python screen.py               # 分层打分
python download.py --dry-run   # 试算下载，不实际下载
python download.py report --no-llm   # 只按当前库重建报告（不联网、不调模型）
python selfcheck.py --no-mail  # 自检
```

## 4 需要你自备的东西

| 项 | 说明 |
|---|---|
| PubMed API key | 可选，有则限速从 3 次/秒 提到 10 次/秒（NCBI 免费申请） |
| 大模型 API | 任意 OpenAI 兼容接口（本项目在 SiliconFlow / DeepSeek 上验证过）。**不填也能跑**，只是没有中文翻译与综述 |
| 邮箱 | 周报推送用；QQ 邮箱需填**授权码**而非登录密码 |
| `jcr.csv` | **未随包提供**：它是 Clarivate JCR 的衍生数据，受其许可约束。请用你机构的 JCR 导出文件运行 `build_jcr.py` 自行生成 |
| `journals.csv` | 已提供：人工提级表（分区不高但本方向绕不开的期刊） |

## 5 目录结构

```
monitor.py      抓取 / 入库 / 导出待打分
screen.py       关键词分层 + 大模型增强识别
summarize.py    中文摘要（可选，走统一 LLM 出口）
download.py     全文获取、内容校验、报告生成、路径自愈
reportlib.py    统一的富文本报告渲染（单一大模型出口 llm_chat）
jcr.py          期刊分级与常量
profile.py/.md  研究画像（改 md 即可，不用改代码）
queryx.py       检索词自适应闭环（本周捡到 → 下周补搜）
aiboard.py      可选的第二板块（医学AI / 通用AI），与主板块完全隔离
mail.py         邮件推送
selfcheck.py    周期自检
verify_dois.py  元数据体检（与 PubMed 反查比对）
run.py          串行编排
```

## 6 数据与隐私

本仓库**不包含**任何运行数据（`data/`、`reports/`、cookies、数据库均已被 `.gitignore` 排除）。
请勿把含有真实凭据的 `config.json`、机构登录 Cookie 或抓取到的题录数据提交到公开仓库。

## 7 许可

MIT（见 `LICENSE`）。第三方数据（PubMed / OpenAlex / arXiv 等）遵循各自的使用条款；
JCR 数据不在本包内。
