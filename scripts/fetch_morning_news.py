#!/usr/bin/env python3
"""
早朝简报采集脚本
每日 06:00 自动运行，抓取全球新闻 RSS → data/morning_brief_YYYYMMDD.json
覆盖: 政治 | 军事 | 经济 | AI大模型
"""
import json, pathlib, datetime, subprocess, re, sys, os, logging
from urllib.parse import quote_plus
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree as ET
from file_lock import atomic_json_write
from utils import validate_url, read_json

log = logging.getLogger('朝报')
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(name)s] %(message)s', datefmt='%H:%M:%S')

DATA = pathlib.Path(__file__).resolve().parent.parent / 'data'
NEWS_SOURCE_LIBRARY_FILE = DATA / 'news_source_library.json'

# ── RSS 源配置 ──────────────────────────────────────────────────────────
FEEDS = {
    '政治': [
        ('NYT World', 'https://rss.nytimes.com/services/xml/rss/nyt/World.xml'),
        ('NYT Politics', 'https://rss.nytimes.com/services/xml/rss/nyt/Politics.xml'),
        ('Al Jazeera', 'https://www.aljazeera.com/xml/rss/all.xml'),
    ],
    '军事': [
        ('Defense News', 'https://www.defensenews.com/arc/outboundfeeds/rss/?outputType=xml'),
        ('Breaking Defense', 'https://breakingdefense.com/feed/'),
        ('Military Times', 'https://www.militarytimes.com/arc/outboundfeeds/rss/'),
    ],
    '经济': [
        ('CNBC', 'https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114'),
        ('FT World', 'https://www.ft.com/world?format=rss'),
        ('Economist Intl', 'https://www.economist.com/international/rss.xml'),
    ],
    'AI大模型': [
        ('Hacker News', 'https://hnrss.org/newest?q=AI+LLM+model&points=50'),
        ('VentureBeat AI', 'https://venturebeat.com/category/ai/feed/'),
        ('MIT Tech Review', 'https://www.technologyreview.com/feed/'),
    ],
}

CATEGORY_KEYWORDS = {
    '军事': ['war', 'military', 'troops', 'attack', 'missile', 'army', 'navy', 'weapons',
              '战', '军', '导弹', '士兵', 'ukraine', 'russia', 'china sea', 'nato'],
    'AI大模型': ['ai', 'llm', 'gpt', 'claude', 'gemini', 'openai', 'anthropic', 'deepseek',
                'machine learning', 'neural', 'model', '大模型', '人工智能', 'chatgpt'],
}


def _default_news_source_library():
    return {
        'sources': [
            {'name': 'BBC中文', 'domain': 'bbc.com', 'categories': ['政治', '经济', 'AI大模型'],
             'feeds': ['https://feeds.bbci.co.uk/zhongwen/simp/rss.xml', 'https://feeds.bbci.co.uk/zhongwen/trad/rss.xml']},
            {'name': '新华社', 'domain': 'xinhuanet.com', 'categories': ['政治', '经济', 'AI大模型'], 'feeds': []},
            {'name': '央视网', 'domain': 'cctv.com', 'categories': ['政治', '经济', 'AI大模型'], 'feeds': []},
            {'name': '中国新闻网', 'domain': 'chinanews.com.cn', 'categories': ['政治', '经济', 'AI大模型'],
             'feeds': ['https://www.chinanews.com.cn/rss/scroll-news.xml', 'https://www.chinanews.com.cn/rss/world.xml']},
            {'name': '人民网', 'domain': 'people.com.cn', 'categories': ['政治', '经济']},
            {'name': '财新网', 'domain': 'caixin.com', 'categories': ['经济', 'AI大模型']},
            {'name': '第一财经', 'domain': 'yicai.com', 'categories': ['经济']},
            {'name': '澎湃新闻', 'domain': 'thepaper.cn', 'categories': ['政治', '经济', 'AI大模型']},
            {'name': '36氪', 'domain': '36kr.com', 'categories': ['经济', 'AI大模型'], 'feeds': ['https://36kr.com/feed']},
            {'name': '华尔街见闻', 'domain': 'wallstreetcn.com', 'categories': ['经济', 'AI大模型']},
        ]
    }


def _load_news_source_library():
    data = read_json(NEWS_SOURCE_LIBRARY_FILE, {})
    if not isinstance(data, dict) or not isinstance(data.get('sources'), list):
        data = _default_news_source_library()
    sources = []
    for s in data.get('sources', []):
        if not isinstance(s, dict):
            continue
        name = str(s.get('name') or '').strip()
        domain = str(s.get('domain') or '').strip().lower()
        cats = s.get('categories') or []
        if not name or not domain or not isinstance(cats, list):
            continue
        if domain.startswith('http://') or domain.startswith('https://'):
            try:
                domain = (urlsplit(domain).hostname or '').lower()
            except Exception:
                domain = ''
        if not domain:
            continue
        sources.append({
            'name': name,
            'domain': domain,
            'categories': [str(c).strip() for c in cats if str(c).strip()],
            'feeds': [str(f).strip() for f in (s.get('feeds') or []) if str(f).strip()],
        })
    if not sources:
        data = _default_news_source_library()
        sources = data['sources']
    return {'sources': sources}


def _build_allowed_domains(source_library: dict, enabled_cats: list[str]) -> set[str]:
    allowed = set()
    enabled = set(enabled_cats or [])
    for s in (source_library.get('sources') or []):
        cats = set(s.get('categories') or [])
        if enabled and not (cats & enabled):
            continue
        d = str(s.get('domain') or '').strip().lower()
        if d.startswith('www.'):
            d = d[4:]
        if d:
            allowed.add(d)
    return allowed


def _canon_host(host: str) -> str:
    h = str(host or '').strip().lower()
    if h.startswith('www.'):
        h = h[4:]
    return h


def _host_in_allowed_domains(url: str, allowed_domains: set[str]) -> bool:
    if not allowed_domains:
        return True
    try:
        host = _canon_host((urlsplit(str(url or '')).hostname or ''))
    except Exception:
        return False
    if not host:
        return False
    for d in allowed_domains:
        dd = _canon_host(d)
        if host == dd or host.endswith('.' + dd):
            return True
    return False


def _feed_in_allowed_domains(feed_url: str, allowed_domains: set[str]) -> bool:
    if not allowed_domains:
        return True
    try:
        host = _canon_host((urlsplit(str(feed_url or '')).hostname or ''))
    except Exception:
        return False
    if not host:
        return False
    for d in allowed_domains:
        dd = _canon_host(d)
        if host == dd or host.endswith('.' + dd):
            return True
    return False


def _build_library_feeds_by_category(source_library: dict, enabled_cats: list[str]) -> dict:
    result = {c: [] for c in (enabled_cats or [])}
    enabled = set(enabled_cats or [])
    for src in (source_library.get('sources') or []):
        cats = set(src.get('categories') or [])
        target_cats = sorted(cats & enabled)
        feeds = [str(f).strip() for f in (src.get('feeds') or []) if str(f).strip()]
        if not target_cats or not feeds:
            continue
        name = str(src.get('name') or '网站库来源').strip()[:80] or '网站库来源'
        for cat in target_cats:
            for feed in feeds:
                result.setdefault(cat, []).append((name, feed))
    return result

def _extract_json_block(text: str):
    """从模型输出中提取 JSON。支持 fenced block 与裸 JSON 对象。"""
    if not text:
        return None
    m = re.search(r"```json\s*(\{[\s\S]*?\})\s*```", text, flags=re.IGNORECASE)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass
    start = text.find('{')
    end = text.rfind('}')
    if start >= 0 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except Exception:
            pass
    return None


_URL_PROBE_CACHE = {}


def _normalize_public_http_url(url: str) -> str:
    """规范化并校验公开可访问 URL（拒绝空白、非法 scheme）。"""
    raw = str(url or '').strip()
    if not raw:
        return ''
    # 带空白通常是 hallucination 链接
    if re.search(r'\s', raw):
        return ''
    try:
        s = urlsplit(raw)
        cleaned = urlunsplit((s.scheme, s.netloc, s.path, s.query, ''))
    except Exception:
        return ''
    if not validate_url(cleaned, allowed_schemes=('https', 'http')):
        return ''
    return cleaned


def _is_user_facing_link_acceptable(url: str) -> bool:
    """过滤“可连通但不适合前台直达”的链接。"""
    low = str(url or '').lower()
    # Google News RSS 包装链接经常非稳定直达页，用户体验差（404/落地不一致）
    if 'news.google.com/rss/articles/' in low:
        return False
    return True


def _looks_like_article_url(url: str) -> bool:
    try:
        s = urlsplit(str(url or ''))
        path = (s.path or '').strip('/')
    except Exception:
        return False
    if not path:
        return False
    low_path = path.lower()
    # 首页/栏目页误命中保护（仅拦截最明显情况）
    if low_path in ('index', 'index.html', 'home'):
        return False
    # 明确过滤常见频道/首页路径（避免把“站点入口”当文章）
    if re.fullmatch(r'zhongwen/(simp|trad)', low_path):
        return False
    if '/topics/' in low_path:
        return False
    if re.fullmatch(r'(news|world|business|technology|finance|china)(/)?', low_path):
        return False
    # 明确放行文章详情路径
    if '/articles/' in low_path or '/article/' in low_path:
        return True
    # 常见文章 URL 特征：较长 slug、含日期、html 后缀等
    if len(path) >= 18:
        return True
    if re.search(r'\d{4}[-/]\d{1,2}[-/]\d{1,2}', path):
        return True
    if path.endswith(('.html', '.htm', '.shtml')):
        return True
    return True


def _url_is_reachable(url: str, timeout: int = 6) -> bool:
    """探活 URL：先 HEAD，失败再 GET。"""
    if not url:
        return False
    if url in _URL_PROBE_CACHE:
        return _URL_PROBE_CACHE[url]

    ua = 'Mozilla/5.0 (compatible; MorningBriefLinkCheck/1.0)'
    ok = False
    try:
        req = Request(url, headers={'User-Agent': ua}, method='HEAD')
        with urlopen(req, timeout=timeout) as resp:
            code = int(getattr(resp, 'status', 200) or 200)
            ok = 200 <= code < 400
    except Exception:
        try:
            req = Request(url, headers={'User-Agent': ua}, method='GET')
            with urlopen(req, timeout=timeout) as resp:
                code = int(getattr(resp, 'status', 200) or 200)
                ok = 200 <= code < 400
        except Exception:
            ok = False
    _URL_PROBE_CACHE[url] = ok
    return ok


def _host_is_reachable(url: str, timeout: int = 6) -> bool:
    """域名级探活：正文页被反爬时，用站点可达作为用户可访问的弱证明。"""
    try:
        s = urlsplit(str(url or ''))
        host = (s.hostname or '').strip()
        if not host:
            return False
        site = f'{s.scheme or "https"}://{host}/'
    except Exception:
        return False
    return _url_is_reachable(site, timeout=timeout)


def _enough_information_density(title: str, summary: str) -> bool:
    """信息量门槛：避免过短、空洞摘要。"""
    t = str(title or '').strip()
    s = str(summary or '').strip()
    if len(t) < 8:
        return False
    if len(s) < 28:
        return False
    return True


_SOFT_404_PATTERNS = (
    '页面不存在',
    '您要查看的页面不存在',
    'page not found',
    '内容不存在',
    '文章不存在',
    '已删除',
)


def _pick_title_tokens(title: str) -> list[str]:
    raw = str(title or '').strip()
    if not raw:
        return []
    tokens = []
    # 中文片段（2-8字）
    for t in re.findall(r'[\u4e00-\u9fff]{2,8}', raw):
        if t not in tokens:
            tokens.append(t)
        if len(tokens) >= 4:
            break
    # 英文词（长度>=4）
    if len(tokens) < 4:
        for t in re.findall(r'[A-Za-z][A-Za-z0-9_-]{3,}', raw):
            low = t.lower()
            if low not in tokens:
                tokens.append(low)
            if len(tokens) >= 4:
                break
    return tokens


def _url_is_valid_article(url: str, title: str = '', timeout: int = 8) -> bool:
    """校验链接是否为有效新闻详情页（非软404）。"""
    cache_key = f'{url}::{title}'
    if cache_key in _URL_PROBE_CACHE:
        return bool(_URL_PROBE_CACHE[cache_key])

    ua = 'Mozilla/5.0 (compatible; MorningBriefArticleCheck/1.0)'
    ok = False
    try:
        req = Request(url, headers={'User-Agent': ua}, method='GET')
        with urlopen(req, timeout=timeout) as resp:
            code = int(getattr(resp, 'status', 200) or 200)
            if not (200 <= code < 400):
                _URL_PROBE_CACHE[cache_key] = False
                return False
            ctype = str(resp.headers.get('Content-Type', '')).lower()
            if 'text/html' not in ctype:
                _URL_PROBE_CACHE[cache_key] = False
                return False
            raw = resp.read(120000).decode('utf-8', errors='ignore')
    except Exception:
        _URL_PROBE_CACHE[cache_key] = False
        return False

    low = raw.lower()
    # “404”在大量正常页面脚本/CSS中也会出现，不能作为单一判定条件。
    soft_404 = any(p in low for p in _SOFT_404_PATTERNS)
    if not soft_404 and re.search(r'>(\s*404\s*)<', low):
        if ('not found' in low) or ('不存在' in low):
            soft_404 = True
    if soft_404:
        _URL_PROBE_CACHE[cache_key] = False
        return False

    # 页面内容需至少命中一个标题关键词（降低“跳到无关页/首页”的概率）
    tokens = _pick_title_tokens(title)
    if tokens:
        matched = any(tok.lower() in low for tok in tokens)
        if not matched:
            _URL_PROBE_CACHE[cache_key] = False
            return False

    ok = True
    _URL_PROBE_CACHE[cache_key] = ok
    return ok


def _normalize_agent_item(item: dict, fallback_source: str):
    if not isinstance(item, dict):
        return None
    title = str(item.get('title') or '').strip()
    summary = str(item.get('summary') or item.get('desc') or '').strip()
    link = str(item.get('link') or '').strip()
    source = str(item.get('source') or fallback_source or '情报官 Agent').strip()
    if not title or not summary or not link:
        return None
    # 强制中文内容
    if not re.search(r'[\u4e00-\u9fff]', title + summary):
        return None
    norm_link = _normalize_public_http_url(link)
    if not norm_link:
        return None
    if not _is_user_facing_link_acceptable(norm_link):
        return None
    if not _url_is_reachable(norm_link):
        return None
    if not _url_is_valid_article(norm_link, title):
        return None
    return {
        'title': title[:180],
        'summary': summary[:320],
        'link': norm_link,
        'pub_date': str(item.get('pub_date') or item.get('time') or '').strip()[:40],
        'image': '',
        'source': source[:80],
    }


def _write_result_payload(result: dict, day: str):
    today_file = DATA / f'morning_brief_{day}.json'
    atomic_json_write(today_file, result)
    atomic_json_write(DATA / 'morning_brief.json', result)
    return today_file


def _build_category_report(category: str, items: list[dict]) -> dict:
    top = items[:3]
    headline = f'{category} 前沿早报'
    if top:
        key_titles = '；'.join([str(x.get('title') or '')[:22] for x in top if x.get('title')])
        digest = f'本栏聚焦 {len(items)} 条高热动态：{key_titles}'
    else:
        digest = '暂无有效新闻数据'
    return {
        'headline': headline,
        'digest': digest[:220],
        'count': len(items),
    }


def _is_reputable_source(source: str, link: str) -> bool:
    source_low = str(source or '').lower()
    host = ''
    try:
        host = (urlsplit(str(link or '')).hostname or '').lower()
    except Exception:
        host = ''
    trusted_domains = (
        'xinhua',
        'people.com.cn',
        'cctv.com',
        'chinanews.com',
        'caixin.com',
        'thepaper.cn',
        'jiemian.com',
        'yicai.com',
        'wallstreetcn.com',
        '36kr.com',
        'huxiu.com',
        'stcn.com',
        'cs.com.cn',
        'ifeng.com',
        'sina.com.cn',
        'qq.com',
        'sohu.com',
        'bjnews.com.cn',
    )
    trusted_names = (
        '新华社', '人民网', '央视', '中国新闻网', '财新', '澎湃', '界面', '第一财经',
        '华尔街见闻', '36氪', '虎嗅', '证券时报', '中国证券报', '凤凰', '新浪', '腾讯', '搜狐', '新京报',
        '经济观察报', '中国科学报', 'Science', 'EETimes'
    )
    if any(k in host for k in trusted_domains):
        return True
    if host.endswith('.gov.cn') or '.gov.cn' in host:
        return True
    if host.endswith('.org.cn') or '.org.cn' in host:
        return True
    if any(k in str(source or '') for k in trusted_names):
        return True
    if re.search(r'(部|委|局|办|院|总署|协会|政府网|人民银行|网信办|中科院|教育部|日报|晚报|周刊|杂志|新闻网|新闻网|观察报|科技报)', str(source or '')):
        return True
    # 兼容部分国际主流媒体（可读性较高）
    if any(k in host for k in ('reuters.com', 'apnews.com', 'bbc.com', 'ft.com', 'wsj.com', 'bloomberg.com')):
        return True
    if any(k in source_low for k in ('reuters', 'bbc', 'financial times', 'bloomberg', 'ap news')):
        return True
    # 兜底：只要是合法公开域名（非内网/非本地），且给了来源名，也视为可接受媒体来源
    if host and '.' in host and not host.endswith(('localhost', '.local')):
        if re.match(r'^[a-z0-9.-]+$', host) and str(source or '').strip():
            return True
    return False


def _collect_with_zaochao_agent(
    enabled_cats: list[str],
    day: str,
    source_library: dict,
    merged_feeds: dict,
    max_items: int = 8,
    timeout: int = 180,
):
    """先用网站库真实 feed 生成候选池，再让情报官做排序与中文改写。"""
    if not enabled_cats:
        return None
    min_required = 5
    allowed_domains = _build_allowed_domains(source_library, enabled_cats)
    if not allowed_domains:
        return None

    candidate_pool = {}
    for cat in enabled_cats:
        feeds = merged_feeds.get(cat, [])
        if not feeds:
            continue
        raw_items = fetch_category(cat, feeds, max_items=max(max_items * 5, 24))
        rows = []
        used = set()
        for idx, it in enumerate(raw_items, 1):
            link = _normalize_public_http_url(it.get('link', ''))
            if not link or link in used:
                continue
            if not _host_in_allowed_domains(link, allowed_domains):
                continue
            if not _is_user_facing_link_acceptable(link):
                continue
            if not _looks_like_article_url(link):
                continue
            if not _url_is_valid_article(link, ''):
                continue
            title = str(it.get('title') or '').strip()
            summary = str(it.get('summary') or '').strip()
            if not _enough_information_density(title, summary):
                continue
            rows.append({
                'id': f'{cat}-{idx}',
                'title': title[:180],
                'summary': summary[:260],
                'link': link,
                'source': str(it.get('source') or '')[:80],
                'pub_date': str(it.get('pub_date') or '')[:40],
            })
            used.add(link)
            if len(rows) >= max(max_items * 3, 16):
                break
        if rows:
            candidate_pool[cat] = rows

    if not candidate_pool:
        return None

    feedback = ''
    for attempt in range(1, 4):
        prompt = (
            "你是情报官（zaochao）Agent。\n"
            "你将收到网站库真实候选新闻池。\n"
            f"请按分类挑选每类不少于 {min_required} 条、最多 {max_items} 条最值得关注的新闻。\n"
            "硬规则：\n"
            "1) 只能使用候选池中的 id，禁止生成新链接；\n"
            "2) 输出中文标题+中文摘要；\n"
            "3) 只输出 JSON。\n"
            "输出格式：\n"
            "{\n"
            "  \"categories\": {\n"
            "    \"经济\": [{\"id\":\"经济-1\",\"title\":\"中文标题\",\"summary\":\"中文摘要\"}],\n"
            "    \"AI大模型\": [...]\n"
            "  }\n"
            "}\n"
            + (f"上一次不达标原因：{feedback}\n请修正。\n" if feedback else "")
            + "候选池：\n"
            + json.dumps(candidate_pool, ensure_ascii=False)
        )
        cmd = ['openclaw', 'agent', '--agent', 'zaochao', '-m', prompt, '--timeout', str(max(60, int(timeout)))]
        p = subprocess.run(cmd, capture_output=True, text=True)
        if p.returncode != 0:
            feedback = f'调用失败 rc={p.returncode}'
            continue
        payload = _extract_json_block((p.stdout or '').strip())
        if not isinstance(payload, dict):
            feedback = '输出不是合法 JSON'
            continue
        selected = payload.get('categories')
        if not isinstance(selected, dict):
            feedback = '缺少 categories'
            continue

        out = {
            'date': day,
            'generated_at': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'categories': {},
            'category_reports': {},
        }
        errors = []
        total = 0
        for cat in enabled_cats:
            by_id = {x['id']: x for x in candidate_pool.get(cat, [])}
            picked = selected.get(cat, [])
            normalized = []
            used_links = set()
            if not isinstance(picked, list):
                picked = []
            for row in picked:
                rid = str((row or {}).get('id') or '').strip()
                base = by_id.get(rid)
                if not base:
                    continue
                title = str((row or {}).get('title') or '').strip() or base.get('title', '')
                summary = str((row or {}).get('summary') or '').strip() or base.get('summary', '')
                if not re.search(r'[\u4e00-\u9fff]', title + summary):
                    title, summary = base.get('title', ''), base.get('summary', '')
                if not _enough_information_density(title, summary):
                    continue
                link = base.get('link', '')
                if link in used_links:
                    continue
                normalized.append({
                    'title': title[:180],
                    'summary': summary[:320],
                    'link': link,
                    'pub_date': base.get('pub_date', ''),
                    'image': '',
                    'source': base.get('source', ''),
                })
                used_links.add(link)
                if len(normalized) >= max_items:
                    break
            # 若情报官选择不足，自动用候选池补齐（仅补真实候选，不新增外部链接）
            if len(normalized) < min_required:
                for base in candidate_pool.get(cat, []):
                    link = str(base.get('link') or '').strip()
                    if not link or link in used_links:
                        continue
                    t = str(base.get('title') or '').strip()
                    s = str(base.get('summary') or '').strip() or t
                    if not _enough_information_density(t, s):
                        continue
                    normalized.append({
                        'title': t[:180],
                        'summary': s[:320],
                        'link': link,
                        'pub_date': base.get('pub_date', ''),
                        'image': '',
                        'source': base.get('source', ''),
                    })
                    used_links.add(link)
                    if len(normalized) >= min_required:
                        break
            if len(normalized) < min_required:
                errors.append(f'[{cat}] 条数不足：{len(normalized)}/{min_required}')
            out['categories'][cat] = normalized
            out['category_reports'][cat] = _build_category_report(cat, normalized)
            total += len(normalized)
        if not errors and total > 0:
            return out
        feedback = '；'.join(errors[:20]) or '结果为空'
        log.warning(f'情报官第{attempt}轮不达标：{feedback}')

    log.warning(f'情报官连续3轮不达标，放弃本次覆盖。最后原因：{feedback}')
    return None


def _google_news_query_feed(query: str) -> str:
    q = quote_plus(query)
    return f'https://news.google.com/rss/search?q={q}&hl=zh-CN&gl=CN&ceid=CN:zh-Hans'


def _default_feeds_for_category(category: str):
    """为任意分类提供一个“热门兜底”源，保证新增分类也能采集。"""
    query_map = {
        '政治': '国际 政治 热门',
        '军事': '国际 军事 热门',
        '经济': '全球 经济 市场 热门',
        'AI大模型': 'AI 大模型 热门',
    }
    q = query_map.get(category, f'{category} 热门')
    return [('Google News 热门', _google_news_query_feed(q))]


def _pub_ts(pub: str):
    """将 pubDate 转为 unix ts，失败返回 0。"""
    if not pub:
        return 0
    try:
        return int(parsedate_to_datetime(pub).timestamp())
    except Exception:
        return 0

def curl_rss(url, timeout=10):
    """用 curl 抓取 RSS"""
    try:
        from urllib.request import Request, urlopen
        req = Request(url, headers={'User-Agent': 'Mozilla/5.0 (compatible; MorningBrief/1.0)'})
        response = urlopen(req, timeout=timeout)
        return response.read().decode('utf-8', errors='ignore')
    except Exception:
        return ''

def _safe_parse_xml(xml_text, max_size=5*1024*1024):
    """安全解析 XML：限制大小，禁用外部实体（防 XXE）。"""
    if len(xml_text) > max_size:
        log.warning(f'XML 内容过大 ({len(xml_text)} bytes)，跳过')
        return None
    # 剥离 DOCTYPE / ENTITY 声明以防 XXE
    cleaned = re.sub(r'<!DOCTYPE[^>]*>', '', xml_text, flags=re.IGNORECASE)
    cleaned = re.sub(r'<!ENTITY[^>]*>', '', cleaned, flags=re.IGNORECASE)
    try:
        return ET.fromstring(cleaned)
    except ET.ParseError:
        return None


def parse_rss(xml_text):
    """解析 RSS XML → list of {title, desc, link, pub_date, image}"""
    items = []
    try:
        root = _safe_parse_xml(xml_text)
        if root is None:
            return items
        # RSS 2.0
        ns = {'media': 'http://search.yahoo.com/mrss/'}
        for item in root.findall('.//item')[:8]:
            def get(tag):
                el = item.find(tag)
                return (el.text or '').strip() if el is not None else ''
            title = get('title')
            desc  = re.sub(r'<[^>]+>', '', get('description'))[:200]
            link  = get('link')
            pub   = get('pubDate')
            # 图片
            img = ''
            enc = item.find('enclosure')
            if enc is not None and 'image' in (enc.get('type') or ''):
                img = enc.get('url', '')
            media = item.find('media:thumbnail', ns) or item.find('media:content', ns)
            if media is not None:
                img = media.get('url', img)
            items.append({'title': title, 'desc': desc, 'link': link,
                          'pub_date': pub, 'image': img})
    except Exception:
        pass
    return items

def match_category(item, category):
    """判断新闻是否属于该分类（用于军事/AI过滤）"""
    kws = CATEGORY_KEYWORDS.get(category, [])
    if not kws:
        return True
    text = (item['title'] + ' ' + item['desc']).lower()
    return any(k in text for k in kws)

def fetch_category(category, feeds, max_items=5):
    """抓取一个分类的新闻"""
    seen_urls = set()
    results = []
    for source_name, url in feeds:
        if len(results) >= max_items:
            break
        xml = curl_rss(url)
        if not xml:
            continue
        items = parse_rss(xml)
        for item in items:
            if not item['title']:
                continue
            norm_link = _normalize_public_http_url(item.get('link', ''))
            if not norm_link:
                continue
            if not _is_user_facing_link_acceptable(norm_link):
                continue
            if norm_link in seen_urls:
                continue
            # 军事和AI分类需要关键词过滤
            if category in CATEGORY_KEYWORDS and not match_category(item, category):
                continue
            seen_urls.add(norm_link)
            results.append({
                'title': item['title'],
                'summary': item['desc'] or item['title'],
                'link': norm_link,
                'pub_date': item['pub_date'],
                'image': item['image'],
                'source': source_name,
                '_ts': _pub_ts(item.get('pub_date', '')),
            })
            if len(results) >= max_items:
                break
    # “热门”近似：按发布时间倒序，优先最新
    results.sort(key=lambda x: x.get('_ts', 0), reverse=True)
    for x in results:
        x.pop('_ts', None)
    return results[:max_items]

def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--force', action='store_true', help='强制采集，忽略幂等锁')
    parser.add_argument('--agent-first', action='store_true', help='优先交给情报官 Agent 采集（不做回退）')
    args = parser.parse_args()

    # 幂等锁：防重复执行
    today = datetime.date.today().strftime('%Y%m%d')
    lock_file = DATA / f'morning_brief_{today}.lock'
    if lock_file.exists() and not args.force:
        age = datetime.datetime.now().timestamp() - lock_file.stat().st_mtime
        if age < 3600:  # 1小时内不重复
            log.info(f'今日已采集（{today}），跳过（使用 --force 强制采集）')
            return
    # 注意：lock 放到采集成功后再 touch，防止失败也锁定

    # 读取用户配置
    config_file = DATA / 'morning_brief_config.json'
    config = {}
    try:
        config = json.loads(config_file.read_text())
    except Exception:
        pass

    # 已启用的分类
    enabled_cats = set()
    if config.get('categories'):
        for c in config['categories']:
            if c.get('enabled', True):
                enabled_cats.add(c['name'])
    else:
        enabled_cats = set(FEEDS.keys())

    # 网站库（来源白名单）
    source_library = _load_news_source_library()
    enabled_cat_list = sorted(enabled_cats)
    allowed_domains = _build_allowed_domains(source_library, enabled_cat_list)

    # 用户自定义关键词（全局加权）
    user_keywords = [kw.lower() for kw in config.get('keywords', [])]

    # 从网站库构建 feed（从源头限制来源）
    custom_feeds = config.get('custom_feeds', [])
    merged_feeds = _build_library_feeds_by_category(source_library, enabled_cat_list)
    for cf in custom_feeds:
        cat = cf.get('category', '')
        feed_url = cf.get('url', '')
        if cat in enabled_cats and feed_url:
            # 校验自定义源 URL（SSRF 防护）
            if validate_url(feed_url):
                if _feed_in_allowed_domains(feed_url, allowed_domains):
                    merged_feeds.setdefault(cat, []).append((cf.get('name', '自定义'), feed_url))
                else:
                    log.warning(f'自定义源不在网站库域名范围，跳过: {feed_url}')
            else:
                log.warning(f'自定义源 URL 不合法，跳过: {feed_url}')

    log.info(f'开始采集 {today}...')
    log.info(f'  启用分类: {", ".join(enabled_cats)}')
    if user_keywords:
        log.info(f'  关注词: {", ".join(user_keywords)}')
    if custom_feeds:
        log.info(f'  自定义源: {len(custom_feeds)} 个')
    log.info(f'  网站库域名: {", ".join(sorted(allowed_domains))}')
    for c in enabled_cat_list:
        log.info(f'  {c} 可用源: {len(merged_feeds.get(c, []))} 个')

    if args.agent_first:
        try:
            agent_result = _collect_with_zaochao_agent(
                enabled_cat_list,
                today,
                source_library,
                merged_feeds,
                max_items=8,
                timeout=180,
            )
            if agent_result:
                today_file = _write_result_payload(agent_result, today)
                total = sum(len(v) for v in (agent_result.get('categories') or {}).values())
                log.info(f'✅ 情报官采集完成：共 {total} 条 → {today_file.name}')
                lock_file.touch()
                return
            log.warning('情报官结果为空或缺少有效链接：本次不覆盖现有早报（已禁用回退）')
            return
        except Exception as e:
            log.warning(f'情报官采集异常：本次不覆盖现有早报（已禁用回退）: {e}')
            return

    result = {
        'date': today,
        'generated_at': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'categories': {},
        'category_reports': {},
    }

    for category, feeds in merged_feeds.items():
        log.info(f'  采集 {category}...')
        items = fetch_category(category, feeds)
        # Boost items matching user keywords
        if user_keywords:
            for item in items:
                text = (item.get('title', '') + ' ' + item.get('summary', '')).lower()
                item['_kw_hits'] = sum(1 for kw in user_keywords if kw in text)
            items.sort(key=lambda x: x.get('_kw_hits', 0), reverse=True)
            for item in items:
                item.pop('_kw_hits', None)
        result['categories'][category] = items
        result['category_reports'][category] = _build_category_report(category, items)
        log.info(f'    {category}: {len(items)} 条')

    # 写入今日文件 + 覆写 latest（看板读这个）
    today_file = _write_result_payload(result, today)

    total = sum(len(v) for v in result['categories'].values())
    log.info(f'✅ 完成：共 {total} 条新闻 → {today_file.name}')

    # 采集成功后才写入幂等锁
    lock_file.touch()

if __name__ == '__main__':
    main()
