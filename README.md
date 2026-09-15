# Tactile arXiv Daily

面向 **灵巧手 × 数值型触觉（taxel / 三维力阵列，如帕西尼 PaXini）** 的个人文献追踪工具。
基于 [robotics_arXiv_daily](https://github.com/jiangranlv/robotics_arXiv_daily)（fork 自 [cv-arxiv-daily](https://github.com/Vincentqyw/cv-arxiv-daily)）改写：自动从 arXiv 拉取论文 → 本地规则多标签分类 → 识别触觉模态 → 生成可检索网页与 Markdown 汇总。

- 在线浏览：https://chen-hurry.github.io/tactile-arxiv-daily/ （GitHub Actions 每天自动更新）
- 本地浏览：直接用浏览器打开 `docs/index.html`（或 `bash scripts/serve.sh` 后访问 http://localhost:8765）
- 汇总表：`PAPERS.md`（自动生成）
- 数据库：`data/papers.json`

## 类别

| 类别 | 中文 | 内容 |
|---|---|---|
| Sim | 触觉仿真环境 | 触觉/力觉仿真器、可微触觉仿真、带触觉的仿真基准（MuJoCo / Isaac / SAPIEN / Genesis） |
| VTLA | 视觉-触觉-语言-动作模型 | 在 VLA 中引入触觉/力觉的多模态大模型 |
| VTA | 视觉-触觉-动作策略 | 不含语言条件的触觉(+视觉)策略：模仿学习、扩散策略、强化学习、Sim2Real |
| Other | 其他触觉相关 | 传感器、数据集、表征、力控等；`config.yaml → other_category.enabled: false` 可关闭 |

一篇论文可同时属于多个类别（按得分）。分类规则全部在 `config.yaml` 中，改完运行 `--reclassify` 立即对全库生效。

## 触觉模态标记

| 标记 | 含义 | 识别依据（示例） |
|---|---|---|
| 🔢 数值型 | taxel / 力阵列 | taxel、piezoresistive、capacitive、Hall-effect、uSkin、Xela、ReSkin、AnySkin、BioTac、PaXini、tri-axial force |
| 📷 视觉型 | 图像式触觉 | GelSight、DIGIT、GelSlim、TacTip、optical/vision-based tactile、tactile image |
| 🔀 混合 | 两者都出现 | |
| ❔ 未识别 | 摘要中无明确传感器信息 | 可在 `curation.yaml` 中手动指定 |

网页上可以按模态筛选（例如只看 🔢 数值型），并可标记已读/收藏（保存在浏览器本地）。

## 使用

### 自动更新（GitHub Actions）

`.github/workflows/daily.yml` 每天 01:30 / 13:30 UTC 在云端运行 `tactile_arxiv.py`，把 `data/papers.json`、`docs/papers.js`、`PAPERS.md` 的变化提交回仓库，GitHub Pages 随之更新。也可以在仓库 Actions 页面手动运行（可勾选 backfill）。

### 本地运行

```bash
pip install -r requirements.txt      # 仅需 PyYAML

python tactile_arxiv.py --backfill   # 首次：回溯历史文献（约 10~20 分钟，受 arXiv 限流影响）
python tactile_arxiv.py              # 日常增量更新
python tactile_arxiv.py --reclassify # 不联网：按新配置重新分类并重新生成页面
python tactile_arxiv.py --daemon 6   # 常驻进程，每 6 小时更新一次（二选一，推荐下面的 cron）

bash scripts/install_cron.sh           # 本机定时任务：每天 09:15 / 21:15 同步 GitHub 上的最新数据（git pull），日志在 logs/
bash scripts/install_cron.sh --remove  # 卸载定时任务
```

### 数据源与容错

1. **arXiv API**（主）：`config.yaml → fetch.queries` 中的宽召回检索式，按提交时间倒序增量翻页；429/503 自动指数退避。
2. **arxiv.org 搜索页**（兜底）：API 连续失败时启用，`fetch.html_queries`。
3. **arXiv 每日 RSS**（兜底）：`fetch.rss_feeds`，只含当天新论文。

只收录 `min_date`（默认 2023-01-01）之后发表的论文；所有来源的论文都要先通过 `relevance_gate`（触觉/力觉相关）才会入库。

### 人工整理

- `seeds.yaml`：人工整理的参考文献（带中文说明，网页上显示 ⭐ 精选）。新增一条 `id` 即可，下次运行会自动补全元数据。
- `curation.yaml`：强制分类、指定模态、置顶、隐藏、写笔记，优先级最高。

## 文件结构

```
VTLA/
├── tactile_arxiv.py     # 主程序：拉取 / 分类 / 生成
├── config.yaml          # 检索式、类别规则、模态规则
├── seeds.yaml           # 人工参考文献
├── curation.yaml        # 人工修正
├── data/papers.json     # 文献库
├── PAPERS.md            # 自动生成的分类汇总
├── docs/index.html      # 浏览页面（读取 docs/papers.js）
└── scripts/             # update.sh / install_cron.sh / serve.sh
```

## 致谢与许可证

本项目参考并改写自以下开源项目，遵循其 [Apache License 2.0](LICENSE)：

- [Vincentqyw/cv-arxiv-daily](https://github.com/Vincentqyw/cv-arxiv-daily)
- [jiangranlv/robotics_arXiv_daily](https://github.com/jiangranlv/robotics_arXiv_daily)

具体改动见 [NOTICE](NOTICE)。论文元数据来自 [arXiv](https://arxiv.org)（感谢 arXiv 提供开放接口），版权归原作者所有。
