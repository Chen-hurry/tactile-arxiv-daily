#!/usr/bin/env python3
"""Tactile arXiv Daily —— 灵巧手 × 数值型触觉文献追踪工具。

参考与出处（Apache License 2.0，详见 LICENSE / NOTICE）：
  * cv-arxiv-daily        https://github.com/Vincentqyw/cv-arxiv-daily
  * robotics_arXiv_daily  https://github.com/jiangranlv/robotics_arXiv_daily
本文件在 robotics_arXiv_daily/daily_arxiv.py 的基础上改写，主要变化：
  * 只依赖 PyYAML + 标准库（arXiv API / RSS 直接解析 Atom/RSS XML）
  * 宽检索 + 本地规则多标签分类（分类规则全部在 config.yaml 中）
  * 识别触觉模态：数值型(taxel/力阵列) / 视觉型(GelSight 等) / 混合 / 未知
  * 429 限流指数退避；API 不可用时回退到 arXiv 每日 RSS
  * seeds.yaml 人工参考文献 + curation.yaml 人工修正
  * 输出 data/papers.json（数据库）、README.md、docs/papers.js（配合 docs/index.html 浏览）

用法：
  python tactile_arxiv.py                 # 日常增量更新
  python tactile_arxiv.py --backfill      # 首次运行：多翻页回溯历史文献
  python tactile_arxiv.py --reclassify    # 不联网，仅按最新 config 重新分类并重新生成页面
  python tactile_arxiv.py --daemon 6      # 常驻进程，每 6 小时更新一次
"""
import argparse
import datetime as dt
import html
import json
import logging
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

import yaml

ROOT = os.path.dirname(os.path.abspath(__file__))
ATOM = "{http://www.w3.org/2005/Atom}"
ARXIV_NS = "{http://arxiv.org/schemas/atom}"
DC_NS = "{http://purl.org/dc/elements/1.1/}"
USER_AGENT = "tactile-arxiv-daily/1.0 (personal literature tracker)"

logging.basicConfig(format="[%(asctime)s %(levelname)s] %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S", level=logging.INFO)
log = logging.getLogger("tactile")


# ----------------------------------------------------------------------------
# 配置 / 存储
# ----------------------------------------------------------------------------
def rel(path):
    return path if os.path.isabs(path) else os.path.join(ROOT, path)


def load_yaml(path, default=None):
    path = rel(path)
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return default if data is None else data


def load_db(path):
    path = rel(path)
    if not os.path.exists(path):
        return {"last_update": None, "papers": {}}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_db(path, db):
    path = rel(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=1, sort_keys=True)
    os.replace(tmp, path)


# ----------------------------------------------------------------------------
# HTTP（带限流退避）
# ----------------------------------------------------------------------------
class RateLimitedError(RuntimeError):
    pass


_last_request = [0.0]
_api_down = [False]  # 熔断：本轮 API 重试耗尽后不再请求 API，直接走兜底数据源


def api_get(url, fetch_cfg):
    if _api_down[0]:
        raise RateLimitedError("arXiv API 本轮已熔断")
    try:
        return http_get(url, fetch_cfg)
    except Exception:
        _api_down[0] = True
        raise


def http_get(url, fetch_cfg):
    delay = float(fetch_cfg.get("delay_seconds", 5))
    retries = int(fetch_cfg.get("max_retries", 6))
    timeout = float(fetch_cfg.get("timeout", 60))
    backoff = max(delay, 10.0)
    for attempt in range(retries + 1):
        wait = delay - (time.time() - _last_request[0])
        if wait > 0:
            time.sleep(wait)
        _last_request[0] = time.time()
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < retries:
                retry_after = e.headers.get("Retry-After") if e.headers else None
                sleep_s = float(retry_after) if retry_after and retry_after.isdigit() else backoff
                log.warning(f"HTTP {e.code}，{sleep_s:.0f}s 后重试 ({attempt + 1}/{retries})")
                time.sleep(sleep_s)
                backoff = min(backoff * 2, 600)
                continue
            if e.code == 429:
                raise RateLimitedError(url[:120]) from e
            raise
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            if attempt < retries:
                log.warning(f"网络错误 {e}，{backoff:.0f}s 后重试 ({attempt + 1}/{retries})")
                time.sleep(backoff)
                backoff = min(backoff * 2, 600)
                continue
            raise


# ----------------------------------------------------------------------------
# arXiv 解析
# ----------------------------------------------------------------------------
ID_RE = re.compile(r"(\d{4}\.\d{4,5})(v\d+)?")
URL_RE = re.compile(r"https?://[^\s,;()<>\"'}]+")


def clean(s):
    return re.sub(r"\s+", " ", s or "").strip()


def delatex(s):
    """RSS 作者名中的 LaTeX 重音：Bar{\\i}\\c{s} -> Baris"""
    s = re.sub(r"\\[a-zA-Z]\{(\w)\}", r"\1", s)      # \c{s}
    s = re.sub(r"\\[\"'`^~.=]\{?(\w)\}?", r"\1", s)  # \"u \'{e}
    s = re.sub(r"\{\\([ij])\}|\\([ij])\b", r"\1\2", s)
    return s.replace("{", "").replace("}", "")


def extract_links(*texts):
    code, project = None, None
    for t in texts:
        for u in URL_RE.findall(t or ""):
            u = u.rstrip(".")
            if "github.com" in u or "gitlab.com" in u or "huggingface.co" in u:
                code = code or u
            elif "arxiv.org" not in u:
                project = project or u
    return code, project


def parse_atom(xml_bytes):
    root = ET.fromstring(xml_bytes)
    total = root.find("{http://a9.com/-/spec/opensearch/1.1/}totalResults")
    papers = []
    for e in root.findall(f"{ATOM}entry"):
        raw_id = e.findtext(f"{ATOM}id", "")
        m = ID_RE.search(raw_id)
        if not m:  # 例如 id_list 查询中不存在的 ID 会返回 error entry
            continue
        pid = m.group(1)
        comment = clean(e.findtext(f"{ARXIV_NS}comment", ""))
        abstract = clean(e.findtext(f"{ATOM}summary", ""))
        code, project = extract_links(comment, abstract)
        prim = e.find(f"{ARXIV_NS}primary_category")
        papers.append({
            "id": pid,
            "version": m.group(2) or "",
            "title": clean(e.findtext(f"{ATOM}title", "")),
            "abstract": abstract,
            "authors": [clean(a.findtext(f"{ATOM}name", "")) for a in e.findall(f"{ATOM}author")],
            "published": e.findtext(f"{ATOM}published", "")[:10],
            "updated": e.findtext(f"{ATOM}updated", "")[:10],
            "comment": comment,
            "primary_category": prim.get("term") if prim is not None else "",
            "arxiv_categories": [c.get("term") for c in e.findall(f"{ATOM}category")],
            "code_url": code,
            "project_url": project,
        })
    return papers, int(total.text) if total is not None and total.text else None


def parse_rss(xml_bytes, feed):
    root = ET.fromstring(xml_bytes)
    pub = root.findtext("channel/pubDate", "")
    try:
        day = dt.datetime.strptime(pub[:16], "%a, %d %b %Y").date().isoformat()
    except ValueError:
        day = dt.date.today().isoformat()
    papers = []
    for it in root.findall("channel/item"):
        desc = it.findtext("description", "")
        if re.search(r"Announce Type:\s*(replace|replace-cross)", desc):
            continue  # 只要新论文
        m = ID_RE.search(it.findtext("link", "") or desc)
        if not m:
            continue
        abstract = clean(desc.split("Abstract:", 1)[-1])
        authors = [delatex(clean(a)) for a in (it.findtext(f"{DC_NS}creator", "") or "").split(",") if clean(a)]
        code, project = extract_links(abstract)
        cats = [clean(c.text) for c in it.findall("category") if c.text]
        papers.append({
            "id": m.group(1), "version": "",
            "title": clean(it.findtext("title", "")),
            "abstract": abstract, "authors": authors,
            "published": day, "updated": day, "comment": "",
            "primary_category": cats[0] if cats else feed,
            "arxiv_categories": cats or [feed],
            "code_url": code, "project_url": project,
        })
    return papers


MONTHS = {m: i for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july", "august",
                                        "september", "october", "november", "december"], 1)}


def strip_tags(s):
    return clean(html.unescape(re.sub(r"<[^>]+>", " ", s or "")))


def parse_search_html(page):
    """解析 arxiv.org/search 结果页（API 限流时的兜底）。"""
    papers = []
    for block in page.split('<li class="arxiv-result">')[1:]:
        m = re.search(r'arxiv\.org/abs/(\d{4}\.\d{4,5})', block)
        if not m:
            continue
        title = re.search(r'<p class="title is-5 mathjax">(.*?)</p>', block, re.S)
        full = re.search(r'<span class="abstract-full[^"]*"[^>]*>(.*?)<a class="is-size-7"', block, re.S)
        authors = re.search(r'<p class="authors">(.*?)</p>', block, re.S)
        date = re.search(r'Submitted</span>\s*(\d{1,2}) (\w+), (\d{4})', block)
        comment = re.search(r'Comments:</span>\s*<span[^>]*>(.*?)</span>', block, re.S)
        cats = re.findall(r'<span class="tag is-small[^>]*>([\w.\-]+)</span>', block)
        published = ""
        if date and date.group(2).lower() in MONTHS:
            published = f"{date.group(3)}-{MONTHS[date.group(2).lower()]:02d}-{int(date.group(1)):02d}"
        abstract, comment = strip_tags(full.group(1) if full else ""), strip_tags(comment.group(1) if comment else "")
        code, project = extract_links(comment, abstract)
        papers.append({
            "id": m.group(1), "version": "",
            "title": strip_tags(title.group(1) if title else ""),
            "abstract": abstract,
            "authors": re.findall(r'<a href="/search/\?searchtype=author[^"]*">([^<]+)</a>', authors.group(1)) if authors else [],
            "published": published, "updated": published, "comment": comment,
            "primary_category": cats[0] if cats else "", "arxiv_categories": cats,
            "code_url": code, "project_url": project,
        })
    return papers


def fetch_search_html(fetch_cfg, db, pages, min_date="", backfill=False):
    found = {}
    size = 200
    for q in fetch_cfg.get("html_queries", []):
        for page in range(pages):
            params = {"query": q, "searchtype": "abstract", "abstracts": "show",
                      "order": "-announced_date_first", "size": size, "start": page * size}
            url = "https://arxiv.org/search/?" + urllib.parse.urlencode(params)
            log.info(f"arxiv.org 搜索页 page {page + 1}/{pages}: {q}")
            papers = parse_search_html(http_get(url, fetch_cfg).decode("utf-8", "replace"))
            new = sum(p["id"] not in db["papers"] and p["id"] not in found for p in papers)
            for p in papers:
                found.setdefault(p["id"], p)
            log.info(f"  返回 {len(papers)} 篇，其中新论文 {new} 篇")
            if len(papers) < size or (new == 0 and not backfill):
                break
            if min_date and papers and max(p["published"] or "9" for p in papers) < min_date:
                break
    return found


def build_query(q, cats):
    if cats:
        return f"({q}) AND ({' OR '.join('cat:' + c for c in cats)})"
    return q


def fetch_api(fetch_cfg, db, pages, min_date=""):
    """对每条检索式按提交时间倒序翻页；遇到整页都已入库的旧论文则提前停止。"""
    found = {}
    known = db["papers"]
    size = int(fetch_cfg.get("page_size", 100))
    failures = 0
    for q in fetch_cfg.get("queries", []):
        query = build_query(q, fetch_cfg.get("arxiv_categories"))
        for page in range(pages):
            params = {"search_query": query, "start": page * size, "max_results": size,
                      "sortBy": "submittedDate", "sortOrder": "descending"}
            url = fetch_cfg["api_url"] + "?" + urllib.parse.urlencode(params)
            log.info(f"API 查询 page {page + 1}/{pages}: {q[:70]}")
            try:
                papers, total = parse_atom(api_get(url, fetch_cfg))
            except Exception as e:  # noqa: BLE001 保留已拉到的结果，只放弃这条检索式剩余页
                failures += 1
                log.error(f"  API 请求失败: {e}")
                break
            new = 0
            for p in papers:
                if p["id"] not in known and p["id"] not in found:
                    new += 1
                found[p["id"]] = p
            log.info(f"  返回 {len(papers)} 篇，其中新论文 {new} 篇（总匹配 {total}）")
            if len(papers) < size or (new == 0 and pages <= fetch_cfg.get("max_pages_per_query", 3)):
                break
            if min_date and papers and max(p["published"] for p in papers) < min_date:
                break  # 整页都早于起始日期
    if failures and not found:
        raise RuntimeError(f"arXiv API 全部 {failures} 次请求失败")
    return found, failures


def fetch_rss(fetch_cfg):
    found = {}
    for feed in fetch_cfg.get("rss_feeds", []):
        url = f"https://rss.arxiv.org/rss/{feed}"
        log.info(f"RSS: {url}")
        try:
            for p in parse_rss(http_get(url, fetch_cfg), feed):
                found.setdefault(p["id"], p)
        except Exception as e:  # noqa: BLE001 单个 feed 失败不影响其他
            log.error(f"RSS {feed} 失败: {e}")
    return found


def parse_abs_page(page, pid):
    meta = lambda k: [html.unescape(v) for v in re.findall(rf'<meta name="citation_{k}" content="([^"]*)"', page)]  # noqa: E731
    title = meta("title")
    if not title:
        return None
    date = (meta("date") or [""])[0].replace("/", "-")
    abstract = clean((meta("abstract") or [""])[0])
    comment = re.search(r'<td class="tablecell comments mathjax">(.*?)</td>', page, re.S)
    comment = strip_tags(comment.group(1)) if comment else ""
    prim = re.search(r'<span class="primary-subject">[^(]*\(([\w.\-]+)\)', page)
    code, project = extract_links(comment, abstract)
    authors = [" ".join(reversed([x.strip() for x in a.split(",", 1)])) for a in meta("author")]
    return {"id": pid, "version": "", "title": clean(title[0]), "abstract": abstract, "authors": authors,
            "published": date, "updated": date, "comment": comment,
            "primary_category": prim.group(1) if prim else "", "arxiv_categories": [prim.group(1)] if prim else [],
            "code_url": code, "project_url": project}


def fetch_by_ids(fetch_cfg, ids):
    found = {}
    ids = list(ids)
    try:
        found.update(_fetch_by_ids_api(fetch_cfg, ids))
    except Exception as e:  # noqa: BLE001
        log.warning(f"API 按 ID 获取失败，改为逐篇读取 arxiv.org/abs 页面: {e}")
    for pid in ids:
        if pid in found:
            continue
        try:
            p = parse_abs_page(http_get(f"https://arxiv.org/abs/{pid}", fetch_cfg).decode("utf-8", "replace"), pid)
            if p:
                found[pid] = p
        except Exception as e:  # noqa: BLE001
            log.error(f"  {pid} 获取失败: {e}")
    log.info(f"按 ID 获取到 {len(found)}/{len(ids)} 篇")
    return found


def _fetch_by_ids_api(fetch_cfg, ids):
    found = {}
    for i in range(0, len(ids), 50):
        chunk = ids[i:i + 50]
        params = {"id_list": ",".join(chunk), "max_results": len(chunk)}
        url = fetch_cfg["api_url"] + "?" + urllib.parse.urlencode(params)
        log.info(f"按 ID 获取元数据 {i + 1}-{i + len(chunk)}/{len(ids)}")
        papers, _ = parse_atom(api_get(url, fetch_cfg))
        for p in papers:
            found[p["id"]] = p
    return found


# ----------------------------------------------------------------------------
# 分类
# ----------------------------------------------------------------------------
class Rules:
    def __init__(self, cfg):
        c = lambda s: re.compile(s, re.IGNORECASE)  # noqa: E731
        self.gate = [[c(r) for r in grp] for grp in cfg.get("relevance_gate", [])]
        self.exclude_gate = [c(r) for r in cfg.get("exclude_gate", [])]
        self.cats = {}
        for name, spec in cfg["categories"].items():
            self.cats[name] = {
                "match": [[c(r) for r in grp] for grp in spec.get("match", [])],
                "exclude": [c(r) for r in spec.get("exclude", [])],
                "boost": [c(r) for r in spec.get("boost", [])],
            }
        mod = cfg.get("modality", {})
        self.numeric = [c(r) for r in mod.get("numeric", [])]
        self.vision = [c(r) for r in mod.get("vision", [])]
        self.hand = c(cfg.get("hand_regex", "dexterous"))
        self.paxini = c(cfg.get("paxini_regex", "paxini"))
        self.other = cfg.get("other_category", {}).get("name", "Other")
        self.other_enabled = cfg.get("other_category", {}).get("enabled", True)
        sel = cfg.get("classify", {})
        self.min_score = float(sel.get("min_score", 4))
        self.keep_ratio = float(sel.get("keep_ratio", 0.5))
        self.max_topics = int(sel.get("max_topics", 3))

    @staticmethod
    def _all_groups(groups, text):
        # 组内每个正则都要命中（一个组可写成多个 AND 条件）
        return all(r.search(text) for r in groups)

    def relevant(self, text):
        if any(r.search(text) for r in self.exclude_gate):
            return False
        return any(self._all_groups(g, text) for g in self.gate)

    def classify(self, title, abstract):
        """返回 {类别: 得分}（全部命中的类别）。得分只统计各类别第一组（区分性关键词）和 boost，
        避免 “tactile” 这类通用词把所有类别都抬高。"""
        text = f"{title}\n{abstract}"
        result = {}
        for name, spec in self.cats.items():
            if not spec["match"] or not all(self._all_groups(g, text) for g in spec["match"]):
                continue
            if any(r.search(text) for r in spec["exclude"]):
                continue
            score = 0.0
            for r, w in [(r, 1.0) for r in spec["match"][0]] + [(r, 1.5) for r in spec["boost"]]:
                hits = [m.group(0).lower() for m in r.finditer(text)]
                if not hits:
                    continue
                # 不同关键词数量 + 标题命中 + 出现次数（封顶）
                score += w * (2 * len(set(hits)) + 4 * bool(r.search(title)) + min(len(hits), 4) * 0.5)
            result[name] = round(score, 1)
        return result

    def select_topics(self, scores):
        if not scores:
            return []
        best = max(scores.values())
        keep = [k for k, v in scores.items() if v >= max(self.min_score, self.keep_ratio * best)]
        return sorted(keep, key=lambda k: -scores[k])[: self.max_topics]

    def modality(self, text):
        num = sorted({m.group(0).lower() for r in self.numeric for m in r.finditer(text)})
        vis = sorted({m.group(0).lower() for r in self.vision for m in r.finditer(text)})
        if num and vis:
            kind = "mixed"
        elif num:
            kind = "numeric"
        elif vis:
            kind = "vision"
        else:
            kind = "unknown"
        return kind, num[:6], vis[:6]


def annotate(paper, rules, seed=None, cur=None):
    """根据规则 + 人工信息计算 topics / modality 等派生字段。"""
    text = f"{paper['title']}\n{paper['abstract']}"
    scores = rules.classify(paper["title"], paper["abstract"])
    topics = rules.select_topics(scores)
    kind, num_kw, vis_kw = rules.modality(text)

    for src in (seed, cur):
        if not src:
            continue
        forced = src.get("categories") or ([src["category"]] if src.get("category") else [])
        forced = [t for t in forced + (src.get("extra_categories", []) or []) if t in rules.cats or t == rules.other]
        if src is cur and cur.get("categories"):
            topics = []  # curation 显式指定时完全覆盖
        for t in reversed(forced):
            if t in topics:
                topics.remove(t)
            topics.insert(0, t)
        if src.get("modality") in ("numeric", "vision", "mixed", "unknown"):
            kind = src["modality"]

    paper.update({
        "topics": topics or [rules.other],
        "scores": scores,
        "modality": kind,
        "modality_keywords": {"numeric": num_kw, "vision": vis_kw},
        "hand": bool(rules.hand.search(text)),
        "paxini": bool(rules.paxini.search(text)),
        "seed": bool(seed),
        "note": (cur or {}).get("note") or (seed or {}).get("note") or "",
        "pinned": bool((cur or {}).get("pinned")),
        "hidden": bool((cur or {}).get("hidden")),
    })
    return paper


def normalize_modality(m):
    return {"taxel": "numeric", "force": "numeric", "numeric": "numeric", "vision-based": "vision",
            "vision": "vision", "mixed": "mixed"}.get((m or "").lower(), "unknown")


def load_seeds(path):
    seeds = {}
    for s in load_yaml(path, []) or []:
        m = ID_RE.search(str(s.get("id", "")))
        if not m:
            continue
        s = dict(s)
        s["id"] = m.group(1)
        if "modality" in s:
            s["modality"] = normalize_modality(s["modality"])
        seeds[s["id"]] = s
    return seeds


def load_curation(path):
    cur = load_yaml(path, {}) or {}
    out = {}
    for pid, v in (cur.get("papers") or {}).items():
        m = ID_RE.search(str(pid))
        if m:
            out[m.group(1)] = v or {}
    return out


# ----------------------------------------------------------------------------
# 输出
# ----------------------------------------------------------------------------
MOD_LABEL = {"numeric": "🔢 数值型", "vision": "📷 视觉型", "mixed": "🔀 混合", "unknown": "❔ 未知"}


def topic_order(cfg):
    return list(cfg["categories"]) + [cfg.get("other_category", {}).get("name", "Other")]


def topic_meta(cfg):
    meta = {k: {"zh": v.get("zh", ""), "desc": v.get("desc", "")} for k, v in cfg["categories"].items()}
    oc = cfg.get("other_category", {})
    meta[oc.get("name", "Other")] = {"zh": oc.get("zh", ""), "desc": oc.get("desc", "")}
    return meta


def sort_key(p):
    return (not p.get("pinned"), "".join(chr(255 - ord(ch)) for ch in p.get("published", "")))


def md_escape(s):
    return (s or "").replace("|", "\\|")


def write_readme(cfg, db):
    papers = [p for p in db["papers"].values() if not p.get("hidden")]
    meta = topic_meta(cfg)
    lines = [f"# {cfg.get('title', 'Tactile arXiv Daily')}", "",
             f"> {cfg.get('subtitle', '')}", ">",
             f"> 更新时间：{db['last_update']} ｜ 共 {len(papers)} 篇 ｜ 网页浏览：`docs/index.html`", "",
             "模态标记：🔢 数值型（taxel/力阵列） · 📷 视觉型（GelSight 等图像触觉） · 🔀 混合 · ❔ 未识别 ｜ 🖐 灵巧手 · ⭐ 人工精选 · 📌 置顶", "",
             "## 目录", ""]
    for t in topic_order(cfg):
        n = sum(t in p["topics"] for p in papers)
        if n:
            anchor = re.sub(r"[^a-z0-9\- ]", "", t.lower()).replace(" ", "-")
            lines.append(f"- [{t}（{meta[t]['zh']}）](#{anchor}) — {n} 篇")
    lines.append("")
    for t in topic_order(cfg):
        group = sorted((p for p in papers if t in p["topics"]), key=sort_key)
        if not group:
            continue
        lines += [f"## {t}", "", f"**{meta[t]['zh']}**：{meta[t]['desc']}", "",
                  "| 日期 | 标题 | 作者 | 模态 | 链接 |", "|:---|:---|:---|:---|:---|"]
        for p in group:
            flags = ("📌" if p.get("pinned") else "") + ("⭐" if p.get("seed") else "") + ("🖐" if p.get("hand") else "")
            authors = p["authors"][0] + (" et al." if len(p["authors"]) > 1 else "") if p["authors"] else ""
            links = f"[arXiv](https://arxiv.org/abs/{p['id']})"
            if p.get("code_url"):
                links += f" · [Code]({p['code_url']})"
            if p.get("project_url"):
                links += f" · [Page]({p['project_url']})"
            lines.append(f"| {p['published']} | {flags} **{md_escape(p['title'])}** | {md_escape(authors)} "
                         f"| {MOD_LABEL[p['modality']]} | {links} |")
        lines.append("")
    with open(rel(cfg["paths"]["readme"]), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def write_web(cfg, db):
    payload = {
        "title": cfg.get("title"), "subtitle": cfg.get("subtitle"),
        "last_update": db["last_update"],
        "topics": [{"name": t, **topic_meta(cfg)[t]} for t in topic_order(cfg)],
        "hide_vision_default": bool(cfg.get("modality", {}).get("hide_vision_only_by_default")),
        "papers": sorted(
            ({k: p.get(k) for k in ("id", "title", "authors", "abstract", "published", "updated", "comment",
                                    "primary_category", "code_url", "project_url", "topics", "scores",
                                    "modality", "modality_keywords", "hand", "paxini", "seed", "note",
                                    "pinned", "added_at")}
             for p in db["papers"].values() if not p.get("hidden")),
            key=lambda p: p["published"], reverse=True),
    }
    path = rel(cfg["paths"]["web_data"])
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("window.TACTILE_DATA = ")
        json.dump(payload, f, ensure_ascii=False)
        f.write(";\n")


# ----------------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------------
def run(cfg, args):
    paths = cfg["paths"]
    fetch_cfg = cfg["fetch"]
    db = load_db(paths["db"])
    rules = Rules(cfg)
    seeds = load_seeds(paths["seeds"])
    cur = load_curation(paths["curation"])
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    min_date = str(cfg.get("min_date") or "")

    def too_old(p, pid):
        # arXiv 新式编号 YYMM.NNNNN 自带提交年月，没有日期时用它判断
        date = (p or {}).get("published") or f"20{pid[:2]}-{pid[2:4]}-99"
        return bool(min_date) and date < min_date and pid not in cur

    seeds = {pid: s for pid, s in seeds.items() if not too_old(None, pid)}
    fetched = {}

    if not args.reclassify:
        # 1) seeds / curation 中尚未入库（或元数据缺失）的论文按 ID 拉取
        missing = [i for i in list(seeds) + list(cur) if i not in db["papers"] or db["papers"][i].get("meta_pending")]
        if missing:
            try:
                fetched.update(fetch_by_ids(fetch_cfg, missing))
            except Exception as e:  # noqa: BLE001
                log.error(f"按 ID 获取失败（下次重试）: {e}")
        # 2) 检索式增量拉取；API 失败则回退到 arxiv.org 搜索页 + 当日 RSS
        pages = int(fetch_cfg.get("backfill_pages", 20) if args.backfill else fetch_cfg.get("max_pages_per_query", 3))
        api_ok = True
        try:
            api_found, failures = fetch_api(fetch_cfg, db, pages, min_date)
            fetched.update(api_found)
            api_ok = failures == 0
        except Exception as e:  # noqa: BLE001
            api_ok = False
            log.error(f"arXiv API 拉取失败，改用兜底数据源: {e}")
        if not api_ok or args.html:
            try:
                for pid, p in fetch_search_html(fetch_cfg, db, pages, min_date, args.backfill).items():
                    fetched.setdefault(pid, p)
            except Exception as e:  # noqa: BLE001
                log.error(f"arxiv.org 搜索页拉取失败: {e}")
        if (not api_ok or args.rss) and fetch_cfg.get("rss_fallback", True):
            for pid, p in fetch_rss(fetch_cfg).items():
                fetched.setdefault(pid, p)

    # 3) 合并入库
    added = 0
    for pid, p in fetched.items():
        text = f"{p['title']}\n{p['abstract']}"
        if (pid not in seeds and pid not in cur and not rules.relevant(text)) or too_old(p, pid):
            continue
        old = db["papers"].get(pid)
        if old and not old.get("meta_pending") and old.get("version") and not p.get("version", "").startswith("v"):
            continue  # 已有 API 元数据，不用搜索页数据覆盖
        if old is None:
            added += 1
            p["added_at"] = now
        else:
            p["added_at"] = old.get("added_at", now)
        db["papers"][pid] = p

    # seeds 拉取失败时先用 seeds 中的标题占位
    for pid, s in seeds.items():
        if pid not in db["papers"]:
            db["papers"][pid] = {"id": pid, "version": "", "title": s.get("title", pid), "abstract": "",
                                 "authors": [], "published": s.get("date", ""), "updated": "", "comment": "",
                                 "primary_category": "", "arxiv_categories": [], "code_url": None,
                                 "project_url": None, "added_at": now, "meta_pending": True}
            added += 1

    # 4) 重新分类全部论文（config 改动即时生效）；不再满足门槛的非人工论文移除
    removed = 0
    for pid in list(db["papers"]):
        p = db["papers"][pid]
        if too_old(p, pid) or (pid not in seeds and pid not in cur
                               and not rules.relevant(f"{p['title']}\n{p['abstract']}")):
            del db["papers"][pid]
            removed += 1
            continue
        annotate(p, rules, seeds.get(pid), cur.get(pid))
        if p["topics"] == [rules.other] and not rules.other_enabled and pid not in seeds and pid not in cur:
            del db["papers"][pid]
            removed += 1

    if not args.reclassify:
        db["last_update"] = now
    db["last_update"] = db["last_update"] or now
    save_db(paths["db"], db)
    write_readme(cfg, db)
    write_web(cfg, db)

    counts = {}
    for p in db["papers"].values():
        for t in p["topics"]:
            counts[t] = counts.get(t, 0) + 1
    log.info(f"完成：新增 {added} 篇，移除 {removed} 篇，库中共 {len(db['papers'])} 篇")
    for t in topic_order(cfg):
        log.info(f"  {t:<24} {counts.get(t, 0)}")

    if cfg.get("git_auto_commit") and os.path.isdir(os.path.join(ROOT, ".git")):
        subprocess.run(["git", "-C", ROOT, "add", "-A"], check=False)
        subprocess.run(["git", "-C", ROOT, "commit", "-qm", f"auto update {now} (+{added})"], check=False)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=os.path.join(ROOT, "config.yaml"))
    ap.add_argument("--backfill", action="store_true", help="多翻页回溯历史文献（首次运行建议使用）")
    ap.add_argument("--reclassify", action="store_true", help="不联网，只按当前配置重新分类/生成页面")
    ap.add_argument("--rss", action="store_true", help="即使 API 成功也额外抓取当日 RSS")
    ap.add_argument("--html", action="store_true", help="即使 API 成功也额外抓取 arxiv.org 搜索页")
    ap.add_argument("--daemon", type=float, metavar="HOURS", help="常驻运行，每隔 HOURS 小时更新一次")
    args = ap.parse_args()

    while True:
        cfg = load_yaml(args.config)  # 每轮重新读取，修改配置无需重启
        try:
            run(cfg, args)
        except Exception:  # noqa: BLE001
            log.exception("本轮更新失败")
            if not args.daemon:
                sys.exit(1)
        if not args.daemon:
            break
        log.info(f"下次更新：{args.daemon} 小时后")
        time.sleep(args.daemon * 3600)


if __name__ == "__main__":
    main()
