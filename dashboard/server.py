#!/usr/bin/env python3
"""
三省六部 · 看板本地 API 服务器
Port: 7891 (可通过 --port 修改)

Endpoints:
  GET  /                       → dashboard.html
  GET  /api/live-status        → data/live_status.json
  GET  /api/agent-config       → data/agent_config.json
  POST /api/set-model          → {agentId, model}
  GET  /api/model-change-log   → data/model_change_log.json
  GET  /api/last-result        → data/last_model_change_result.json
"""
import json, pathlib, subprocess, sys, threading, argparse, datetime, logging, re, os, socket, time, uuid, shutil, hashlib, base64
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
from urllib.request import Request, urlopen

# 引入文件锁工具，确保与其他脚本并发安全
scripts_dir = str(pathlib.Path(__file__).parent.parent / 'scripts')
sys.path.insert(0, scripts_dir)
from file_lock import atomic_json_read, atomic_json_write, atomic_json_update
from utils import validate_url, read_json, now_iso
from court_discuss import (
    create_session as cd_create, advance_discussion as cd_advance,
    get_session as cd_get, conclude_session as cd_conclude,
    list_sessions as cd_list, destroy_session as cd_destroy,
    get_fate_event as cd_fate, OFFICIAL_PROFILES as CD_PROFILES,
)

log = logging.getLogger('server')
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(name)s] %(message)s', datefmt='%H:%M:%S')

CHANNELS_DIR = pathlib.Path(__file__).parent.parent / 'edict' / 'backend' / 'app' / 'channels'
if str(CHANNELS_DIR.parent) not in sys.path:
    sys.path.insert(0, str(CHANNELS_DIR.parent))
from channels import get_channel, get_channel_info, CHANNELS as NOTIFICATION_CHANNELS

OCLAW_HOME = pathlib.Path.home() / '.openclaw'
OPENCLAW_SKILLS_HOME = OCLAW_HOME / 'skills'
AGENTS_SKILLS_HOME = pathlib.Path.home() / '.agents' / 'skills'
MAX_REQUEST_BODY = 30 * 1024 * 1024  # 30 MB
ALLOWED_ORIGIN = None  # Set via --cors; None means restrict to localhost
_DASHBOARD_PORT = 7891  # Updated at startup from --port arg
_DEFAULT_ORIGINS = {
    'http://127.0.0.1:7891', 'http://localhost:7891',
    'http://127.0.0.1:5173', 'http://localhost:5173',  # Vite dev server
}
_SAFE_NAME_RE = re.compile(r'^[a-zA-Z0-9_\-\u4e00-\u9fff]+$')
_SAFE_SKILL_RE = re.compile(r'^[a-zA-Z0-9_\-.\u4e00-\u9fff]+$')

BASE = pathlib.Path(__file__).parent
DIST = BASE / 'dist'          # React 构建产物 (npm run build)
LEGACY_DASHBOARD_HTML = BASE / 'dashboard.html'  # 传统单页看板（当前主入口）
DATA = BASE.parent / "data"
SCRIPTS = BASE.parent / 'scripts'
_ACTIVE_TASK_DATA_DIR = None
LEARNING_PLAN_FILE = DATA / 'learning_plans.json'
PM_FILE = DATA / 'project_management.json'
AUTOMATION_FILE = DATA / 'automation_tasks.json'
PM_ISOLATION_FILE = DATA / 'agent_isolation_registry.json'
AGENT_WORK_SCOPE_FILE = DATA / 'agent_work_scopes.json'
AGENT_WORK_BINDINGS_FILE = DATA / 'agent_work_bindings.json'
JZG_FILE = DATA / 'jiangzuojian.json'
JZG_EXTERNAL_DOCS_DIR = DATA / 'external_docs'
PM_DESIGN_FOLDER_ID = 'FLD-DESIGN'
PM_DESIGN_FOLDER_NAME = '项目设计'
PM_VERSION_FOLDER_ID = 'FLD-VERSION'
PM_VERSION_FOLDER_NAME = '版本控制'
PM_DESIGN_SECTIONS = ('requirements', 'architecture', 'function')
PM_SUGGESTION_STATUS = {'pending', 'adopted'}
PM_VERSION_STATUS = {'draft', 'local', 'github'}
JZG_FOLLOWUP_FOLDER_ID = 'JZG-FOLLOWUP'
JZG_FOLLOWUP_FOLDER_NAME = '项目跟进'
JZG_STRATEGY_FOLDER_ID = 'JZG-STRATEGY'
JZG_STRATEGY_FOLDER_NAME = '策议司'
JZG_BOARD_FOLDER_ID = 'JZG-BOARD'
JZG_BOARD_FOLDER_NAME = '智能看板'
JZG_DOC_DEFAULT_FOLDER_ID = 'JZG-DOC-DEFAULT'
JZG_DOC_DEFAULT_FOLDER_NAME = '未分类'
JZG_DEFAULT_DAILY_TEMPLATE = (
    "【将作监日报】{date}\n"
    "项目：{project_name}\n\n"
    "一、当日完成\n"
    "{done_items}\n\n"
    "二、当前未完成\n"
    "{todo_items}\n\n"
    "三、风险与建议\n"
    "- （由兵部补充）\n"
)
JZG_DEFAULT_WEEKLY_TEMPLATE = (
    "【将作监周报】{start_date} ~ {end_date}\n"
    "项目：{project_name}\n\n"
    "一、本周完成\n"
    "{done_items}\n\n"
    "二、当前未完成\n"
    "{todo_items}\n\n"
    "三、下周计划与提醒\n"
    "- （由兵部补充）\n"
)

AUTOMATION_ALLOWED_AGENTS = {'taizi', 'zhongshu', 'menxia', 'shangshu', 'libu', 'hubu', 'bingbu', 'xingbu', 'rnd', 'libu_hr', 'zaochao'}

DEFAULT_AGENT_WORK_SCOPES = {
    'rnd': [
        {
            'entry': '项目设计 · 研发部生成',
            'service': '围绕 PRD 生成或重写需求/架构/功能设计文档。',
            'invoke': 'agent',
            'bindingId': 'rnd_design_generate',
            'match': ['项目设计-需求说明生成', '项目设计-架构设计生成', '项目设计-功能设计生成', 'design-generate'],
        },
        {
            'entry': '版本控制 · 更新版本',
            'service': '按项目变更汇总版本记录，输出更新说明与发布清单。',
            'invoke': 'agent',
            'bindingId': 'rnd_version_generate',
            'match': ['版本控制-更新版本触发', 'version-generate', '更新版本'],
        },
        {
            'entry': '问题详情 · 研发部复审',
            'service': '对问题给出实现拆分建议、技术路径与落地步骤。',
            'invoke': 'agent',
            'bindingId': 'rnd_review',
            'match': ['问题详情-研发部复审触发', '研发部复审触发', 'rnd-review', '复审'],
        },
        {
            'entry': '问题详情 · 研发部催办',
            'service': '针对长时间未推进问题发起催办并推动状态流转。',
            'invoke': 'agent',
            'bindingId': 'rnd_execute',
            'match': ['问题详情-研发部催办触发', '研发部催办触发', 'execute', '催办'],
        },
    ],
    'bingbu': [
        {'entry': 'PM小组 · 专家小组策议', 'service': '协助拆解需求优先级、方案路径与阶段目标。', 'invoke': 'agent'},
        {'entry': 'PM小组 · 项目跟进', 'service': '输出当日待办推进建议与项目节奏提醒。', 'invoke': 'agent'},
        {'entry': 'PM小组 · 版本协同', 'service': '支持版本计划梳理与发布前协同检查。', 'invoke': 'agent'},
    ],
    'libu': [
        {'entry': '藏经阁 · 细化目录', 'service': '将学习主题拆为可执行目录与阶段化学习路线。', 'invoke': 'agent'},
        {'entry': '藏经阁 · 扫地僧问答', 'service': '围绕主题提供问答、总结与知识回写建议。', 'invoke': 'agent'},
        {'entry': '藏经阁 · 主题笔记', 'service': '沉淀学习笔记结构，形成长期可复用知识资产。', 'invoke': 'ui'},
    ],
    'libu_hr': [
        {'entry': '人事部 · 部门详情', 'service': '维护各 Agent 的模型、SOUL、技能与职责配置。', 'invoke': 'ui'},
        {'entry': '人事部 · 会话治理', 'service': '查看会话概览并支持按会话进入对话面板。', 'invoke': 'ui'},
        {'entry': '人事部 · 组织编排', 'service': '维护部门列表排序与人员治理结构。', 'invoke': 'ui'},
        {
            'entry': '人事部 · SOUL重新整理',
            'service': '基于接口映射与会话触发记录，自动整理并生成可保存的 SOUL 草案。',
            'invoke': 'agent',
            'bindingId': 'hr_soul_reorganize',
            'match': ['soul重新整理', '重整soul', '人事部-soul重新整理触发'],
        },
    ],
    'taizi': [
        {'entry': '任务统筹 · 新建旨意', 'service': '接收需求并发起任务流转，推进跨部门协作。', 'invoke': 'agent'},
        {'entry': '任务统筹 · 状态流转', 'service': '根据任务状态触发部门承接与后续处理流程。', 'invoke': 'agent'},
    ],
    'zhongshu': [
        {'entry': '文书治理 · 模板输出', 'service': '规范文书结构与记录格式，保证信息可追溯。', 'invoke': 'agent'},
    ],
    'menxia': [
        {'entry': '审校治理 · 质检复核', 'service': '审查交付完整性，提出修正意见与风险提示。', 'invoke': 'agent'},
    ],
    'shangshu': [
        {'entry': '中枢调度 · 统筹协同', 'service': '协调多部门执行节奏，保障关键事项闭环。', 'invoke': 'agent'},
    ],
    'hubu': [
        {'entry': '资源治理 · 成本视图', 'service': '汇总资源投入与关键消耗数据，提供决策参考。', 'invoke': 'agent'},
    ],
    'xingbu': [
        {'entry': '规则治理 · 风险约束', 'service': '维护流程边界与规则检查，提示异常风险。', 'invoke': 'agent'},
    ],
    'zaochao': [
        {'entry': '朝报治理 · 摘要播报', 'service': '汇总当日关键信息并形成可读简报。', 'invoke': 'agent'},
    ],
}

DEFAULT_AGENT_WORK_BINDINGS = {
    'rnd_design_generate': {
        'agentId': 'rnd',
        'source': 'dashboard.html::pmGenerateDesign -> POST /api/pm/design-generate',
    },
    'rnd_version_generate': {
        'agentId': 'rnd',
        'source': 'dashboard.html::pmGenerateVersion -> POST /api/pm/version-generate',
    },
    'rnd_review': {
        'agentId': 'rnd',
        'source': 'dashboard.html::pmRndReview(review) -> POST /api/pm/rnd-review',
    },
    'rnd_execute': {
        'agentId': 'rnd',
        'source': 'dashboard.html::pmRndReview(execute) -> POST /api/pm/rnd-review',
    },
    'hr_soul_reorganize': {
        'agentId': 'libu_hr',
        'source': 'dashboard.html::reorganizeSoul -> POST /api/agent-soul/reorganize',
    },
}

# 静态资源 MIME 类型
_MIME_TYPES = {
    '.html': 'text/html; charset=utf-8',
    '.js':   'application/javascript; charset=utf-8',
    '.css':  'text/css; charset=utf-8',
    '.json': 'application/json; charset=utf-8',
    '.png':  'image/png',
    '.jpg':  'image/jpeg',
    '.jpeg': 'image/jpeg',
    '.gif':  'image/gif',
    '.svg':  'image/svg+xml',
    '.ico':  'image/x-icon',
    '.woff': 'font/woff',
    '.woff2': 'font/woff2',
    '.ttf':  'font/ttf',
    '.map':  'application/json',
}


def cors_headers(h):
    req_origin = h.headers.get('Origin', '')
    if ALLOWED_ORIGIN:
        origin = ALLOWED_ORIGIN
    elif req_origin in _DEFAULT_ORIGINS:
        origin = req_origin
    else:
        origin = f'http://127.0.0.1:{_DASHBOARD_PORT}'
    h.send_header('Access-Control-Allow-Origin', origin)
    h.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
    h.send_header('Access-Control-Allow-Headers', 'Content-Type')


def _iter_task_data_dirs():
    """返回可用的任务数据目录候选（优先 workspace，其次本地 data）。"""
    dirs = [DATA]
    for p in sorted(OCLAW_HOME.glob('workspace-*/data')):
        if p.is_dir():
            dirs.append(p)
    return dirs


def _task_source_score(task_file: pathlib.Path):
    """给任务源打分：优先非 demo 任务，其次任务数，再按文件更新时间。"""
    try:
        tasks = atomic_json_read(task_file, [])
    except Exception:
        tasks = []
    if not isinstance(tasks, list):
        tasks = []
    non_demo = sum(1 for t in tasks if str((t or {}).get('id', '')) and not str((t or {}).get('id', '')).startswith('JJC-DEMO'))
    try:
        mtime = task_file.stat().st_mtime
    except Exception:
        mtime = 0
    return (1 if non_demo > 0 else 0, non_demo, len(tasks), mtime)


def get_task_data_dir():
    """自动选择当前任务数据目录，并缓存结果以保持一次服务期内稳定。"""
    global _ACTIVE_TASK_DATA_DIR
    if _ACTIVE_TASK_DATA_DIR and _ACTIVE_TASK_DATA_DIR.is_dir():
        return _ACTIVE_TASK_DATA_DIR
    best_dir = DATA
    best_score = (-1, -1, -1, -1)
    for d in _iter_task_data_dirs():
        tf = d / 'tasks_source.json'
        if not tf.exists():
            continue
        score = _task_source_score(tf)
        if score > best_score:
            best_score = score
            best_dir = d
    _ACTIVE_TASK_DATA_DIR = best_dir
    log.info(f'任务数据源: {_ACTIVE_TASK_DATA_DIR}')
    return _ACTIVE_TASK_DATA_DIR


def load_tasks():
    task_data_dir = get_task_data_dir()
    return atomic_json_read(task_data_dir / 'tasks_source.json', [])


def save_tasks(tasks):
    task_data_dir = get_task_data_dir()
    atomic_json_write(task_data_dir / 'tasks_source.json', tasks)
    # Trigger refresh (异步，不阻塞，避免僵尸进程)
    script = task_data_dir.parent / 'scripts' / 'refresh_live_data.py'
    if not script.exists():
        script = SCRIPTS / 'refresh_live_data.py'

    def _refresh():
        try:
            subprocess.run(['python3', str(script)], timeout=30)
        except Exception as e:
            log.warning(f'refresh_live_data.py 触发失败: {e}')
    threading.Thread(target=_refresh, daemon=True).start()


def handle_task_action(task_id, action, reason):
    """Stop/cancel/resume a task from the dashboard."""
    tasks = load_tasks()
    task = next((t for t in tasks if t.get('id') == task_id), None)
    if not task:
        return {'ok': False, 'error': f'任务 {task_id} 不存在'}

    old_state = task.get('state', '')
    _ensure_scheduler(task)
    _scheduler_snapshot(task, f'task-action-before-{action}')

    if action == 'stop':
        task['state'] = 'Blocked'
        task['block'] = reason or '皇上叫停'
        task['now'] = f'⏸️ 已暂停：{reason}'
    elif action == 'cancel':
        task['state'] = 'Cancelled'
        task['block'] = reason or '皇上取消'
        task['now'] = f'🚫 已取消：{reason}'
    elif action == 'resume':
        # Resume to previous active state or Doing
        task['state'] = task.get('_prev_state', 'Doing')
        task['block'] = '无'
        task['now'] = f'▶️ 已恢复执行'

    if action in ('stop', 'cancel'):
        task['_prev_state'] = old_state  # Save for resume

    task.setdefault('flow_log', []).append({
        'at': now_iso(),
        'from': '皇上',
        'to': task.get('org', ''),
        'remark': f'{"⏸️ 叫停" if action == "stop" else "🚫 取消" if action == "cancel" else "▶️ 恢复"}：{reason}'
    })

    if action == 'resume':
        _scheduler_mark_progress(task, f'恢复到 {task.get("state", "Doing")}')
    else:
        _scheduler_add_flow(task, f'皇上{action}：{reason or "无"}')

    task['updatedAt'] = now_iso()

    save_tasks(tasks)
    if action == 'resume' and task.get('state') not in _TERMINAL_STATES:
        dispatch_for_state(task_id, task, task.get('state'), trigger='resume')
    label = {'stop': '已叫停', 'cancel': '已取消', 'resume': '已恢复'}[action]
    return {'ok': True, 'message': f'{task_id} {label}'}


def handle_archive_task(task_id, archived, archive_all_done=False):
    """Archive or unarchive a task, or batch-archive all Done/Cancelled tasks."""
    tasks = load_tasks()
    if archive_all_done:
        count = 0
        for t in tasks:
            if t.get('state') in ('Done', 'Cancelled') and not t.get('archived'):
                t['archived'] = True
                t['archivedAt'] = now_iso()
                count += 1
        save_tasks(tasks)
        return {'ok': True, 'message': f'{count} 道旨意已归档', 'count': count}
    task = next((t for t in tasks if t.get('id') == task_id), None)
    if not task:
        return {'ok': False, 'error': f'任务 {task_id} 不存在'}
    task['archived'] = archived
    if archived:
        task['archivedAt'] = now_iso()
    else:
        task.pop('archivedAt', None)
    task['updatedAt'] = now_iso()
    save_tasks(tasks)
    label = '已归档' if archived else '已取消归档'
    return {'ok': True, 'message': f'{task_id} {label}'}


def update_task_todos(task_id, todos):
    """Update the todos list for a task."""
    tasks = load_tasks()
    task = next((t for t in tasks if t.get('id') == task_id), None)
    if not task:
        return {'ok': False, 'error': f'任务 {task_id} 不存在'}

    task['todos'] = todos
    task['updatedAt'] = now_iso()
    save_tasks(tasks)
    return {'ok': True, 'message': f'{task_id} todos 已更新'}


def read_skill_content(agent_id, skill_name):
    """Read SKILL.md content for a specific skill."""
    # 输入校验：防止路径遍历
    requested_agent_id = str(agent_id or '').strip()
    if not _SAFE_NAME_RE.match(requested_agent_id) or not _SAFE_SKILL_RE.match(skill_name):
        return {'ok': False, 'error': '参数含非法字符'}
    agent_id = _normalize_agent_id(requested_agent_id)
    cfg = read_json(DATA / 'agent_config.json', {})
    agents = cfg.get('agents', [])
    ag = next((a for a in agents if a.get('id') == agent_id), None)
    if not ag:
        return {'ok': False, 'error': f'Agent {requested_agent_id} 不存在'}
    sk = next((s for s in ag.get('skills', []) if s.get('name') == skill_name), None)
    if not sk:
        return {'ok': False, 'error': f'技能 {skill_name} 不存在'}
    skill_path = pathlib.Path(sk.get('path', '')).resolve()
    # 路径遍历保护：确保路径在 OCLAW_HOME 或项目目录下
    allowed_roots = (
        OCLAW_HOME.resolve(),
        OPENCLAW_SKILLS_HOME.resolve(),
        BASE.parent.resolve(),
        AGENTS_SKILLS_HOME.resolve(),
    )
    if not any(str(skill_path).startswith(str(root)) for root in allowed_roots):
        return {'ok': False, 'error': '路径不在允许的目录范围内'}
    if not skill_path.exists():
        return {'ok': True, 'name': skill_name, 'agent': requested_agent_id, 'content': '(SKILL.md 文件不存在)', 'path': str(skill_path)}
    try:
        content = skill_path.read_text()
        return {'ok': True, 'name': skill_name, 'agent': requested_agent_id, 'content': content, 'path': str(skill_path)}
    except Exception as e:
        return {'ok': False, 'error': str(e)}


def _candidate_agent_soul_paths(agent_id):
    requested_agent_id = str(agent_id or '').strip()
    if not _SAFE_NAME_RE.match(requested_agent_id):
        return None, None, []
    normalized = _normalize_agent_id(requested_agent_id)
    cfg = read_json(DATA / 'agent_config.json', {})
    agents = cfg.get('agents', []) if isinstance(cfg, dict) else []
    ag = next((a for a in agents if a.get('id') == normalized), None)

    candidates = []
    if ag and ag.get('workspace'):
        ws = pathlib.Path(str(ag.get('workspace'))).expanduser()
        candidates.extend([ws / 'soul.md', ws / 'SOUL.md'])
    ws_default = OCLAW_HOME / f'workspace-{normalized}'
    candidates.extend([ws_default / 'soul.md', ws_default / 'SOUL.md'])
    return requested_agent_id, normalized, candidates


def _resolve_agent_soul_path(agent_id, must_exist=True):
    requested_agent_id, normalized, candidates = _candidate_agent_soul_paths(agent_id)
    if not requested_agent_id:
        return {'ok': False, 'error': f'agent_id 非法: {agent_id}'}

    allowed_roots = (OCLAW_HOME.resolve(), BASE.parent.resolve())
    for p in candidates:
        try:
            pr = p.resolve()
        except Exception:
            continue
        if not any(str(pr).startswith(str(root)) for root in allowed_roots):
            continue
        if must_exist and (not pr.exists() or not pr.is_file()):
            continue
        return {'ok': True, 'agentId': normalized, 'path': str(pr)}

    if must_exist:
        return {'ok': False, 'error': f'未找到 {requested_agent_id} 的 SOUL 文件'}
    # 兜底：默认写入 workspace-<agent>/SOUL.md
    fallback = (OCLAW_HOME / f'workspace-{normalized}' / 'SOUL.md').resolve()
    if any(str(fallback).startswith(str(root)) for root in allowed_roots):
        return {'ok': True, 'agentId': normalized, 'path': str(fallback)}
    return {'ok': False, 'error': f'未找到 {requested_agent_id} 的 SOUL 写入路径'}


def read_agent_soul(agent_id):
    """Read agent SOUL.md/soul.md from runtime workspace."""
    resolved = _resolve_agent_soul_path(agent_id, must_exist=True)
    if not resolved.get('ok'):
        return resolved
    p = pathlib.Path(resolved.get('path', ''))
    try:
        content = p.read_text(encoding='utf-8', errors='ignore')
        mtime = datetime.datetime.utcfromtimestamp(p.stat().st_mtime).isoformat() + 'Z'
        return {
            'ok': True,
            'agentId': resolved.get('agentId'),
            'path': str(p),
            'updatedAt': mtime,
            'content': content[:60000],
        }
    except Exception as e:
        return {'ok': False, 'error': f'读取 SOUL 失败: {e}'}


def write_agent_soul(agent_id, content):
    """Write agent SOUL.md content to runtime workspace."""
    text = str(content if content is not None else '')
    if len(text) > 120000:
        return {'ok': False, 'error': 'SOUL 内容过长（最多 120000 字符）'}
    resolved = _resolve_agent_soul_path(agent_id, must_exist=False)
    if not resolved.get('ok'):
        return resolved
    p = pathlib.Path(resolved.get('path', ''))
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding='utf-8')
        mtime = datetime.datetime.utcfromtimestamp(p.stat().st_mtime).isoformat() + 'Z'
        return {'ok': True, 'agentId': resolved.get('agentId'), 'path': str(p), 'updatedAt': mtime}
    except Exception as e:
        return {'ok': False, 'error': f'保存 SOUL 失败: {e}'}


def _strip_markdown_fence(text):
    raw = str(text or '').strip()
    if not raw:
        return ''
    m = re.match(r'^```(?:markdown|md|text)?\s*([\s\S]*?)\s*```$', raw, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return raw


def _collect_agent_trigger_summary(agent_id):
    resp = get_agent_sessions(agent_id)
    sessions = resp.get('sessions', []) if isinstance(resp, dict) else []
    if not isinstance(sessions, list):
        sessions = []
    agg = {}
    for s in sessions:
        if not isinstance(s, dict):
            continue
        reason = str(s.get('triggerReason') or '').strip() or '主会话（未标注来源）'
        row = agg.get(reason) or {'trigger': reason, 'count': 0, 'lastTalkAtTs': 0}
        row['count'] += 1
        ts = int(s.get('lastTalkAtTs') or 0) if str(s.get('lastTalkAtTs') or '').strip() else 0
        if ts > row['lastTalkAtTs']:
            row['lastTalkAtTs'] = ts
        agg[reason] = row
    rows = list(agg.values())
    rows.sort(key=lambda x: (-(x.get('count') or 0), -(x.get('lastTalkAtTs') or 0), str(x.get('trigger') or '')))
    return rows


def _collect_agent_scope_rows(agent_id):
    scopes = _load_agent_work_scopes().get('scopes', {})
    rows = scopes.get(str(agent_id or '').strip(), []) if isinstance(scopes, dict) else []
    if not isinstance(rows, list):
        rows = []
    out = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        entry = str(item.get('entry') or '').strip()
        service = str(item.get('service') or '').strip()
        if not entry and not service:
            continue
        button = entry.split('·', 1)[0].strip() if '·' in entry else entry
        out.append({
            'entry': entry,
            'button': button,
            'service': service,
            'match': item.get('match') if isinstance(item.get('match'), list) else [],
        })
    return out


def reorganize_agent_soul_by_hr(target_agent_id):
    requested = str(target_agent_id or '').strip()
    if not requested or not _SAFE_NAME_RE.match(requested):
        return {'ok': False, 'error': 'invalid agentId'}
    target_id = _normalize_agent_id(requested)
    if target_id == 'main':
        return {'ok': False, 'error': 'main 不支持 SOUL 重整'}
    # 由人事经理执行
    if not _check_agent_workspace('libu_hr'):
        return {'ok': False, 'error': 'libu_hr 工作空间不存在，请先配置人事经理 Agent'}
    if not _check_gateway_alive():
        return {'ok': False, 'error': 'Gateway 未启动，请先运行 openclaw gateway start'}

    target_soul = read_agent_soul(target_id)
    target_soul_text = str(target_soul.get('content') or '').strip() if isinstance(target_soul, dict) and target_soul.get('ok') else ''
    hr_soul = read_agent_soul('libu_hr')
    hr_soul_text = str(hr_soul.get('content') or '').strip() if isinstance(hr_soul, dict) and hr_soul.get('ok') else ''
    scope_rows = _collect_agent_scope_rows(target_id)
    trigger_rows = _collect_agent_trigger_summary(target_id)

    scope_json = json.dumps(scope_rows, ensure_ascii=False, indent=2)
    trigger_json = json.dumps(trigger_rows, ensure_ascii=False, indent=2)
    prompt = (
        "你是人事经理 Agent（libu_hr）。\n"
        "任务：根据你自己的 SOUL 执行步骤，重整目标 Agent 的 SOUL 文档。\n\n"
        "严格要求：\n"
        "1) 必须优先依据“真实触发记录”与“工作范畴配置”来写，不可虚构页面按钮与能力。\n"
        "2) 只输出可直接写入 SOUL.md 的 Markdown 正文，不要解释、不要前后缀。\n"
        "3) 输出结构必须包含以下标题：\n"
        "   - # 角色定位\n"
        "   - # 工作范畴\n"
        "   - # 接口触发映射\n"
        "   - # 协作边界\n"
        "   - # 执行规范\n"
        "   - # 会话策略\n"
        "   - # 完成定义\n"
        "4) “接口触发映射”里请用表格列出：页面按钮/入口、触发来源、触发后职能输出。\n"
        "5) 若某项在真实触发中没有记录，明确标注“暂无真实触发”。\n\n"
        f"目标 Agent: {target_id}\n\n"
        "【人事经理当前 SOUL（供你遵循自己的步骤）】\n"
        f"{hr_soul_text[:50000]}\n\n"
        "【目标 Agent 当前 SOUL】\n"
        f"{target_soul_text[:50000]}\n\n"
        "【目标 Agent 工作范畴配置（权威）】\n"
        f"{scope_json}\n\n"
        "【目标 Agent 会话触发统计（真实记录）】\n"
        f"{trigger_json}\n\n"
        "现在开始输出最终 SOUL 正文："
    )
    ai = _run_agent_sync('libu_hr', prompt, timeout_sec=420)
    if not ai.get('ok'):
        return {'ok': False, 'error': ai.get('error') or '人事经理重整失败'}
    content = _strip_markdown_fence(ai.get('raw', ''))
    if not content:
        return {'ok': False, 'error': '人事经理未返回可用 SOUL 文本'}
    if len(content) > 120000:
        content = content[:120000]
    return {
        'ok': True,
        'agentId': target_id,
        'content': content,
        'meta': {
            'scopeCount': len(scope_rows),
            'triggerCount': len(trigger_rows),
            'generatedBy': 'libu_hr',
            'generatedAt': now_iso(),
        }
    }


def add_skill_to_agent(agent_id, skill_name, description, trigger=''):
    """Create a new skill for an agent with a standardised SKILL.md template."""
    if not _SAFE_SKILL_RE.match(skill_name):
        return {'ok': False, 'error': f'skill_name 含非法字符: {skill_name}'}
    requested_agent_id = str(agent_id or '').strip()
    if not _SAFE_NAME_RE.match(requested_agent_id):
        return {'ok': False, 'error': f'agentId 含非法字符: {requested_agent_id}'}
    agent_id = _normalize_agent_id(requested_agent_id)
    workspace = OCLAW_HOME / f'workspace-{agent_id}' / 'skills' / skill_name
    workspace.mkdir(parents=True, exist_ok=True)
    skill_md = workspace / 'SKILL.md'
    desc_line = description or skill_name
    trigger_section = f'\n## 触发条件\n{trigger}\n' if trigger else ''
    template = (f'---\n'
                f'name: {skill_name}\n'
                f'description: {desc_line}\n'
                f'---\n\n'
                f'# {skill_name}\n\n'
                f'{desc_line}\n'
                f'{trigger_section}\n'
                f'## 输入\n\n'
                f'<!-- 说明此技能接收什么输入 -->\n\n'
                f'## 处理流程\n\n'
                f'1. 步骤一\n'
                f'2. 步骤二\n\n'
                f'## 输出规范\n\n'
                f'<!-- 说明产出物格式与交付要求 -->\n\n'
                f'## 注意事项\n\n'
                f'- (在此补充约束、限制或特殊规则)\n')
    skill_md.write_text(template)
    # Re-sync agent config
    try:
        subprocess.run(['python3', str(SCRIPTS / 'sync_agent_config.py')], timeout=10)
    except Exception:
        pass
    return {'ok': True, 'message': f'技能 {skill_name} 已添加到 {requested_agent_id}', 'path': str(skill_md)}


def add_remote_skill(agent_id, skill_name, source_url, description=''):
    """从远程 URL 或本地路径为 Agent 添加 skill SKILL.md 文件。
    
    支持的源：
    - HTTPS URLs: https://raw.githubusercontent.com/...
    - 本地路径: /path/to/SKILL.md 或 file:///path/to/SKILL.md
    """
    # 输入校验
    requested_agent_id = str(agent_id or '').strip()
    if not _SAFE_NAME_RE.match(requested_agent_id):
        return {'ok': False, 'error': f'agentId 含非法字符: {requested_agent_id}'}
    agent_id = _normalize_agent_id(requested_agent_id)
    if not _SAFE_SKILL_RE.match(skill_name):
        return {'ok': False, 'error': f'skillName 含非法字符: {skill_name}'}
    if not source_url or not isinstance(source_url, str):
        return {'ok': False, 'error': 'sourceUrl 必须是有效的字符串'}
    
    source_url = source_url.strip()
    
    # 检查 Agent 是否存在
    cfg = read_json(DATA / 'agent_config.json', {})
    agents = cfg.get('agents', [])
    if not any(a.get('id') == agent_id for a in agents):
        return {'ok': False, 'error': f'Agent {requested_agent_id} 不存在'}
    
    # 下载或读取文件内容
    try:
        if source_url.startswith('http://') or source_url.startswith('https://'):
            # HTTPS URL 校验
            if not validate_url(source_url, allowed_schemes=('https',)):
                return {'ok': False, 'error': 'URL 无效或不安全（仅支持 HTTPS）'}
            
            # 从 URL 下载，带超时保护
            req = Request(source_url, headers={'User-Agent': 'OpenClaw-SkillManager/1.0'})
            try:
                resp = urlopen(req, timeout=10)
                content = resp.read(10 * 1024 * 1024).decode('utf-8')  # 最多 10MB
                if len(content) > 10 * 1024 * 1024:
                    return {'ok': False, 'error': '文件过大（最大 10MB）'}
            except Exception as e:
                return {'ok': False, 'error': f'URL 无法访问: {str(e)[:100]}'}
        
        elif source_url.startswith('file://'):
            # file:// URL 格式
            local_path = pathlib.Path(source_url[7:])
            if not local_path.exists():
                return {'ok': False, 'error': f'本地文件不存在: {local_path}'}
            content = local_path.read_text()
        
        elif source_url.startswith('/') or source_url.startswith('.'):
            # 本地绝对或相对路径
            local_path = pathlib.Path(source_url).resolve()
            if not local_path.exists():
                return {'ok': False, 'error': f'本地文件不存在: {local_path}'}
            # 路径遍历防护
            allowed_roots = (OCLAW_HOME.resolve(), OPENCLAW_SKILLS_HOME.resolve(), BASE.parent.resolve())
            if not any(str(local_path).startswith(str(root)) for root in allowed_roots):
                return {'ok': False, 'error': '路径不在允许的目录范围内'}
            content = local_path.read_text()
        
        else:
            return {'ok': False, 'error': '不支持的 URL 格式（仅支持 https://, file://, 或本地路径）'}
    except Exception as e:
        return {'ok': False, 'error': f'文件读取失败: {str(e)[:100]}'}
    
    # 基础验证：检查是否为 Markdown 且包含 YAML frontmatter
    if not content.startswith('---'):
        return {'ok': False, 'error': '文件格式无效（缺少 YAML frontmatter）'}
    
    # 验证 frontmatter 结构（先做字符串检查，再尝试 YAML 解析）
    parts = content.split('---', 2)
    if len(parts) < 3:
        return {'ok': False, 'error': '文件格式无效（YAML frontmatter 结构错误）'}
    if 'name:' not in content[:500]:
        return {'ok': False, 'error': '文件格式无效：frontmatter 缺少 name 字段'}
    try:
        import yaml
        yaml.safe_load(parts[1])  # 严格校验 YAML 语法
    except ImportError:
        pass  # PyYAML 未安装，跳过严格验证，字符串检查已通过
    except Exception as e:
        return {'ok': False, 'error': f'YAML 格式无效: {str(e)[:100]}'}
    
    # 创建本地目录
    workspace = OCLAW_HOME / f'workspace-{agent_id}' / 'skills' / skill_name
    workspace.mkdir(parents=True, exist_ok=True)
    skill_md = workspace / 'SKILL.md'
    
    # 写入 SKILL.md
    skill_md.write_text(content)
    
    # 保存源信息到 .source.json
    source_info = {
        'skillName': skill_name,
        'sourceUrl': source_url,
        'description': description,
        'addedAt': now_iso(),
        'lastUpdated': now_iso(),
        'checksum': _compute_checksum(content),
        'status': 'valid',
    }
    source_json = workspace / '.source.json'
    source_json.write_text(json.dumps(source_info, ensure_ascii=False, indent=2))
    
    # Re-sync agent config
    try:
        subprocess.run(['python3', str(SCRIPTS / 'sync_agent_config.py')], timeout=10)
    except Exception:
        pass
    
    return {
        'ok': True,
        'message': f'技能 {skill_name} 已从远程源添加到 {agent_id}',
        'skillName': skill_name,
        'agentId': agent_id,
        'source': source_url,
        'localPath': str(skill_md),
        'size': len(content),
        'addedAt': now_iso(),
    }


def get_remote_skills_list():
    """列表所有已添加的远程 skills 及其源信息"""
    remote_skills = []
    
    # 遍历所有 workspace
    for ws_dir in OCLAW_HOME.glob('workspace-*'):
        agent_id = ws_dir.name.replace('workspace-', '')
        skills_dir = ws_dir / 'skills'
        if not skills_dir.exists():
            continue
        
        for skill_dir in skills_dir.iterdir():
            if not skill_dir.is_dir():
                continue
            skill_name = skill_dir.name
            source_json = skill_dir / '.source.json'
            skill_md = skill_dir / 'SKILL.md'
            
            if not source_json.exists():
                # 本地创建的 skill，跳过
                continue
            
            try:
                source_info = json.loads(source_json.read_text())
                # 检查 SKILL.md 是否存在
                status = 'valid' if skill_md.exists() else 'not-found'
                remote_skills.append({
                    'skillName': skill_name,
                    'agentId': agent_id,
                    'sourceUrl': source_info.get('sourceUrl', ''),
                    'description': source_info.get('description', ''),
                    'localPath': str(skill_md),
                    'addedAt': source_info.get('addedAt', ''),
                    'lastUpdated': source_info.get('lastUpdated', ''),
                    'status': status,
                })
            except Exception:
                pass
    
    return {
        'ok': True,
        'remoteSkills': remote_skills,
        'count': len(remote_skills),
        'listedAt': now_iso(),
    }


def update_remote_skill(agent_id, skill_name):
    """更新已添加的远程 skill 为最新版本（重新从源 URL 下载）"""
    requested_agent_id = str(agent_id or '').strip()
    if not _SAFE_NAME_RE.match(requested_agent_id):
        return {'ok': False, 'error': f'agentId 含非法字符: {requested_agent_id}'}
    agent_id = _normalize_agent_id(requested_agent_id)
    if not _SAFE_SKILL_RE.match(skill_name):
        return {'ok': False, 'error': f'skillName 含非法字符: {skill_name}'}
    
    workspace = OCLAW_HOME / f'workspace-{agent_id}' / 'skills' / skill_name
    source_json = workspace / '.source.json'
    skill_md = workspace / 'SKILL.md'
    
    if not source_json.exists():
        return {'ok': False, 'error': f'技能 {skill_name} 不是远程 skill（无 .source.json）'}
    
    try:
        source_info = json.loads(source_json.read_text())
        source_url = source_info.get('sourceUrl', '')
        if not source_url:
            return {'ok': False, 'error': '源 URL 不存在'}
        
        # 重新下载
        result = add_remote_skill(agent_id, skill_name, source_url, 
                                  source_info.get('description', ''))
        if result['ok']:
            result['message'] = f'技能已更新'
            source_info_updated = json.loads(source_json.read_text())
            result['newVersion'] = source_info_updated.get('checksum', 'unknown')
        return result
    except Exception as e:
        return {'ok': False, 'error': f'更新失败: {str(e)[:100]}'}


def remove_remote_skill(agent_id, skill_name):
    """移除已添加的远程 skill"""
    requested_agent_id = str(agent_id or '').strip()
    if not _SAFE_NAME_RE.match(requested_agent_id):
        return {'ok': False, 'error': f'agentId 含非法字符: {requested_agent_id}'}
    agent_id = _normalize_agent_id(requested_agent_id)
    if not _SAFE_SKILL_RE.match(skill_name):
        return {'ok': False, 'error': f'skillName 含非法字符: {skill_name}'}
    
    workspace = OCLAW_HOME / f'workspace-{agent_id}' / 'skills' / skill_name
    if not workspace.exists():
        return {'ok': False, 'error': f'技能不存在: {skill_name}'}
    
    # 检查是否为远程 skill
    source_json = workspace / '.source.json'
    if not source_json.exists():
        return {'ok': False, 'error': f'技能 {skill_name} 不是远程 skill，无法通过此 API 移除'}
    
    try:
        # 删除整个 skill 目录
        import shutil
        shutil.rmtree(workspace)
        
        # Re-sync agent config
        try:
            subprocess.run(['python3', str(SCRIPTS / 'sync_agent_config.py')], timeout=10)
        except Exception:
            pass
        
        return {'ok': True, 'message': f'技能 {skill_name} 已从 {agent_id} 移除'}
    except Exception as e:
        return {'ok': False, 'error': f'移除失败: {str(e)[:100]}'}


def _compute_checksum(content: str) -> str:
    import hashlib
    return hashlib.sha256(content.encode()).hexdigest()[:16]


def migrate_notification_config():
    """自动迁移旧配置 (feishu_webhook) 到新结构 (notification)"""
    cfg_path = DATA / 'morning_brief_config.json'
    cfg = read_json(cfg_path, {})
    if not cfg:
        return
    if 'notification' in cfg:
        return
    if 'feishu_webhook' not in cfg:
        return
    webhook = cfg.get('feishu_webhook', '').strip()
    cfg['notification'] = {
        'enabled': bool(webhook),
        'channel': 'feishu',
        'webhook': webhook
    }
    try:
        atomic_json_write(cfg_path, cfg)
        log.info('已自动迁移 feishu_webhook 到 notification 配置')
    except Exception as e:
        log.warning(f'迁移配置失败: {e}')


def push_notification():
    """通用消息推送 (支持多渠道)"""
    cfg = read_json(DATA / 'morning_brief_config.json', {})
    notification = cfg.get('notification', {})
    if not notification and cfg.get('feishu_webhook'):
        notification = {'enabled': True, 'channel': 'feishu', 'webhook': cfg['feishu_webhook']}
    if not notification.get('enabled', True):
        return
    channel_type = notification.get('channel', 'feishu')
    webhook = notification.get('webhook', '').strip()
    if not webhook:
        return
    channel_cls = get_channel(channel_type)
    if not channel_cls:
        log.warning(f'未知的通知渠道: {channel_type}')
        return
    if not channel_cls.validate_webhook(webhook):
        log.warning(f'{channel_cls.label} Webhook URL 不合法: {webhook}')
        return
    brief = read_json(DATA / 'morning_brief.json', {})
    date_str = brief.get('date', '')
    total = sum(len(v) for v in (brief.get('categories') or {}).values())
    if not total:
        return
    cat_lines = []
    for cat, items in (brief.get('categories') or {}).items():
        if items:
            cat_lines.append(f'  {cat}: {len(items)} 条')
    summary = '\n'.join(cat_lines)
    date_fmt = date_str[:4] + '年' + date_str[4:6] + '月' + date_str[6:] + '日' if len(date_str) == 8 else date_str
    title = f'📰 天下要闻 · {date_fmt}'
    content = f'共 **{total}** 条要闻已更新\n{summary}'
    url = f'http://127.0.0.1:{_DASHBOARD_PORT}'
    success = channel_cls.send(webhook, title, content, url)
    print(f'[{channel_cls.label}] 推送{"成功" if success else "失败"}')


def push_to_feishu():
    """Push morning brief link to Feishu via webhook. (已弃用，使用 push_notification)"""
    push_notification()


def _default_learning_questions(topic: str):
    topic = (topic or '').strip() or '该主题'
    return [
        {'id': 'Q1', 'question': f'你学习「{topic}」的核心目标是什么？（如求职/创业/落地项目）', 'why': '明确终局目标，避免无效学习'},
        {'id': 'Q2', 'question': '你希望在多长时间内达到可用水平？每周可投入多少小时？', 'why': '决定节奏与里程碑密度'},
        {'id': 'Q3', 'question': '你当前的基础水平如何？是否已有相关项目或经验？', 'why': '确定起点与是否需要补前置知识'},
        {'id': 'Q4', 'question': '你更偏好哪种学习方式？（阅读/视频/实战/导师反馈）', 'why': '匹配学习媒介，提高完成率'},
        {'id': 'Q5', 'question': '你可使用的资源有哪些？（预算、课程平台、导师、设备）', 'why': '约束方案可执行性'},
        {'id': 'Q6', 'question': '你最容易卡住的环节是什么？（理解慢/执行弱/坚持难）', 'why': '提前设计防卡机制'},
        {'id': 'Q7', 'question': '你希望产出哪些可验证结果？（作品、证书、报告、上线系统）', 'why': '用成果驱动学习闭环'},
        {'id': 'Q8', 'question': '你目前有哪些关联任务会与学习冲突？', 'why': '规划时间与优先级，降低中断'},
        {'id': 'Q9', 'question': '你偏好的评估方式是什么？（周测、项目复盘、口头讲解）', 'why': '建立可量化反馈机制'},
        {'id': 'Q10', 'question': '你希望我在学习中扮演什么角色？（教练/审查官/结对执行）', 'why': '确定协作模式与节奏'},
    ]


def _extract_json_payload(text: str):
    raw = str(text or '').strip()
    if not raw:
        return None
    # 1) 直接 JSON
    try:
        return json.loads(raw)
    except Exception:
        pass
    # 2) fenced json
    m = re.search(r'```(?:json)?\s*([\s\S]*?)```', raw, re.IGNORECASE)
    if m:
        fenced = m.group(1).strip()
        try:
            return json.loads(fenced)
        except Exception:
            pass
    # 3) 平衡花括号扫描
    for i, ch in enumerate(raw):
        if ch != '{':
            continue
        depth = 0
        for j in range(i, len(raw)):
            c = raw[j]
            if c == '{':
                depth += 1
            elif c == '}':
                depth -= 1
                if depth == 0:
                    chunk = raw[i:j + 1]
                    try:
                        return json.loads(chunk)
                    except Exception:
                        break
    return None


def _extract_pm_review_text_payload(text: str):
    raw = str(text or '').strip()
    if not raw:
        return None
    lines = [str(x or '').strip() for x in raw.splitlines()]
    lines = [x for x in lines if x]
    if not lines:
        return None

    def _norm_item_line(ln: str):
        s = str(ln or '').strip()
        if not s:
            return ''
        s = re.sub(r'^\s*(\d+[\.\)、]|[-*•])\s*', '', s).strip()
        return s.strip(' \t-•')

    def _split_sentences(text_block: str):
        src = str(text_block or '').strip()
        if not src:
            return []
        parts = re.split(r'[；;。]\s*', src)
        return [p.strip() for p in parts if p and p.strip()]

    def _extract_bullets(text_block: str, limit: int = 20):
        out = []
        for ln in str(text_block or '').splitlines():
            item = _norm_item_line(ln)
            if not item:
                continue
            if item.startswith('【') and item.endswith('】'):
                continue
            out.append(item[:500])
            if len(out) >= limit:
                break
        return out

    # 优先解析四段格式： 【任务判断】【标题建议】【执行要点】【风险与回执建议】
    sec_pattern = re.compile(r'【\s*([^】]{1,40})\s*】')
    matches = list(sec_pattern.finditer(raw))
    section_map = {}
    if matches:
        for idx, m in enumerate(matches):
            name = str(m.group(1) or '').strip()
            start = m.end()
            end = matches[idx + 1].start() if idx + 1 < len(matches) else len(raw)
            content = raw[start:end].strip()
            if name:
                section_map[name] = content

    if section_map:
        def _pick_section(*keys):
            for name, content in section_map.items():
                if any(k in name for k in keys):
                    return str(content or '').strip()
            return ''

        judge_text = _pick_section('任务判断', '判断', '结论')
        title_text = _pick_section('标题建议', '标题')
        plan_text = _pick_section('执行要点', '执行计划', '要点', '步骤')
        clarify_text = _pick_section('待澄清', '澄清')
        risk_text = _pick_section('风险', '回执')

        title = ''
        for ln in str(title_text or '').splitlines():
            item = _norm_item_line(ln)
            if not item:
                continue
            title = item[:200]
            break
        if not title:
            # 四段文本里若无单独标题，回退到首个非标题行
            for ln in lines:
                if ln.startswith('【') and ln.endswith('】'):
                    continue
                item = _norm_item_line(ln)
                if not item:
                    continue
                if len(item) >= 6:
                    title = item[:200]
                    break

        plan = _extract_bullets(plan_text, 20)
        questions = _extract_bullets(clarify_text, 5)
        if not questions:
            # 若未给“待澄清问题”，则从风险段提炼可追问项，避免前端为空
            for sent in _split_sentences(risk_text):
                q = sent[:420]
                if not q:
                    continue
                if ('?' not in q) and ('？' not in q):
                    q = f"请确认：{q}"
                questions.append(q[:500])
                if len(questions) >= 5:
                    break

        desc_parts = []
        if judge_text:
            desc_parts.append(judge_text.strip())
        if risk_text:
            desc_parts.append(risk_text.strip())
        desc = '\n\n'.join([x for x in desc_parts if x]).strip()
        if not desc:
            desc = '\n'.join([_norm_item_line(x) for x in lines if _norm_item_line(x)])[:4000]

        if any([title, desc, questions, plan]):
            return {
                'summary': (judge_text or lines[0] or '已根据当前信息生成建议')[:280],
                'status': 'in_progress',
                'optimizedTitle': (title or '').strip()[:300],
                'optimizedDescription': (desc or '').strip()[:4000],
                'questions': questions[:5],
                'plan': plan[:20],
            }

    def _pick_value(prefixes):
        for ln in lines:
            for p in prefixes:
                if ln.startswith(p):
                    return ln[len(p):].strip(' ：:').strip()
        return ''

    title = _pick_value(['标题：', '标题:', '优化标题：', '优化标题:'])
    desc = _pick_value(['问题描述：', '问题描述:', '优化描述：', '优化描述:', '描述：', '描述:'])
    summary = lines[0][:280] if lines else ''

    if not title:
        for ln in lines[:6]:
            if ln.startswith(('【', '#', '-', '*', '1.', '2.')):
                continue
            if any(k in ln for k in ('建议', '优化', '任务', '问题', '界面', '功能', '修复')):
                title = ln[:200]
                break

    questions = []
    plan = []
    section = ''
    for ln in lines:
        if any(k in ln for k in ('待澄清问题', '澄清问题', '需澄清')):
            section = 'q'
            continue
        if any(k in ln for k in ('执行计划', '实现步骤', '拆分步骤', '步骤建议')):
            section = 'p'
            continue
        m = re.match(r'^(\d+[\.\)、]|[-*•])\s*(.+)$', ln)
        item = ''
        if m:
            item = str(m.group(2) or '').strip()
        elif section in {'q', 'p'} and len(ln) >= 2 and not ln.endswith('：') and not ln.endswith(':'):
            item = ln.strip()
        if not item:
            continue
        if section == 'q':
            questions.append(item[:500])
        elif section == 'p':
            plan.append(item[:500])

    if not desc:
        desc_candidates = []
        for ln in lines:
            if ln.startswith(('【', '#')):
                continue
            if '标题' in ln and ('：' in ln or ':' in ln):
                continue
            if re.match(r'^\s*(\d+[\.\)、]|[-*•])\s*', ln):
                continue
            if len(ln) < 8:
                continue
            desc_candidates.append(ln)
            if len(''.join(desc_candidates)) > 900:
                break
        desc = '\n'.join(desc_candidates)[:4000]

    if not any([title, desc, questions, plan]):
        return None
    return {
        'summary': summary or '已根据当前信息生成建议',
        'status': 'in_progress',
        'optimizedTitle': (title or '').strip()[:300],
        'optimizedDescription': (desc or '').strip()[:4000],
        'questions': questions[:5],
        'plan': plan[:20],
    }


def _repair_pm_review_json_from_text(raw_text: str):
    raw = str(raw_text or '').strip()
    if not raw:
        return None
    fallback = _extract_pm_review_text_payload(raw)
    if fallback:
        return fallback
    brief_lines = [ln.strip() for ln in raw.splitlines() if ln.strip()][:30]
    if not brief_lines:
        return None
    brief = '\n'.join(brief_lines)[:3800]
    return {
        'summary': brief_lines[0][:280],
        'status': 'in_progress',
        'optimizedTitle': brief_lines[0][:120],
        'optimizedDescription': brief,
        'questions': [],
        'plan': [],
    }


def _normalize_agent_id(agent_id):
    return str(agent_id or '').strip()


def _run_agent_sync(agent_id: str, message: str, timeout_sec: int = 420, session_id: str = ''):
    requested_agent_id = str(agent_id or '').strip()
    if not _SAFE_NAME_RE.match(requested_agent_id):
        return {'ok': False, 'error': f'agent_id 非法: {requested_agent_id}'}
    agent_id = _normalize_agent_id(requested_agent_id)
    if not _check_agent_workspace(agent_id):
        return {'ok': False, 'error': f'{requested_agent_id} 工作空间不存在'}
    if not _check_gateway_alive():
        return {'ok': False, 'error': 'Gateway 未启动'}
    timeout_sec = max(120, min(900, int(timeout_sec or 420)))
    cmd = ['openclaw', 'agent', '--agent', agent_id, '-m', message, '--timeout', str(timeout_sec), '--json']
    sid = str(session_id or '').strip()
    if sid:
        cmd.extend(['--session-id', sid])
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_sec + 30)
        stdout = (result.stdout or '').strip()
        stderr = (result.stderr or '').strip()
        if result.returncode != 0:
            raw = (stdout + ('\n' + stderr if stderr else '')).strip()
            # 有些 provider 在返回正文时仍带非零退出码，优先尝试提取结构化 payload
            parsed = _extract_json_payload(raw)
            if isinstance(parsed, dict):
                payloads = (((parsed.get('result') or {}).get('payloads')) or [])
                texts = []
                for p in payloads:
                    t = str((p or {}).get('text') or '').strip()
                    if t:
                        texts.append(t)
                if texts:
                    return {'ok': True, 'raw': '\n'.join(texts).strip()}
            return {'ok': False, 'error': (stderr or stdout or '执行失败').strip()[:500], 'raw': raw[:2000]}
        # 优先解析 --json 结构，避免 stdout 混入其他日志导致解析失败
        try:
            obj = json.loads(stdout or '{}')
            payloads = (((obj.get('result') or {}).get('payloads')) or [])
            texts = []
            for p in payloads:
                t = str((p or {}).get('text') or '').strip()
                if t:
                    texts.append(t)
            raw = '\n'.join(texts).strip()
            if raw:
                return {'ok': True, 'raw': raw}
            status = str(obj.get('status') or '').strip().lower()
            summary = str(obj.get('summary') or '').strip()
            # 部分 provider 会把正文落在 summary 且 status 非 ok，这里按正文兼容
            if summary:
                low = summary.lower()
                bad = ('error' in low) or ('failed' in low) or ('timeout' in low) or _looks_like_context_overflow(summary)
                if not bad:
                    return {'ok': True, 'raw': summary}
            if status and status != 'ok':
                err = str(obj.get('error') or obj.get('summary') or '执行失败').strip()
                return {'ok': False, 'error': err[:500], 'raw': stdout[:2000]}
            # 兜底取 summary，避免空文本
            if summary:
                return {'ok': True, 'raw': summary}
            return {'ok': False, 'error': '模型未返回有效文本', 'raw': stdout[:2000]}
        except Exception:
            raw = (stdout + ('\n' + stderr if stderr else '')).strip()
            return {'ok': True, 'raw': raw}
    except subprocess.TimeoutExpired:
        return {'ok': False, 'error': f'扫地僧响应超时（>{timeout_sec}s）'}
    except Exception as e:
        return {'ok': False, 'error': str(e)}


def _extract_between_markers(text: str, begin_marker: str, end_marker: str) -> str:
    raw = str(text or '')
    if not raw:
        return ''
    b = raw.find(begin_marker)
    if b < 0:
        return ''
    b += len(begin_marker)
    e = raw.find(end_marker, b)
    if e < 0:
        return raw[b:].strip()
    return raw[b:e].strip()


def _run_codex_delegate_sync(task_id: str, prompt: str, agent_id: str = 'rnd', timeout_sec: int = 300):
    """
    通过 scripts/codex_delegate.py 调用本机 Codex CLI。
    成功时返回 {'ok': True, 'raw': <final_message>, 'runFile': ...}
    """
    tid = str(task_id or '').strip()
    if not tid:
        tid = f'PM-{datetime.datetime.now():%Y%m%d%H%M%S}'
    aid = str(agent_id or '').strip() or 'rnd'
    timeout_sec = max(120, min(900, int(timeout_sec or 300)))
    delegate_script = SCRIPTS / 'codex_delegate.py'
    if not delegate_script.exists():
        return {'ok': False, 'error': f'codex_delegate 脚本不存在: {delegate_script}'}

    cmd = [
        sys.executable,
        str(delegate_script),
        tid,
        str(prompt or ''),
        '--model', 'gpt-5.4',
        '--cwd', str(BASE.parent),
        '--agent-id', aid,
        '--timeout', str(timeout_sec),
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_sec + 40,
        )
    except subprocess.TimeoutExpired:
        return {'ok': False, 'error': f'codex_delegate 超时（>{timeout_sec}s）'}
    except Exception as e:
        return {'ok': False, 'error': f'codex_delegate 执行异常: {e}'}

    stdout = str(result.stdout or '').strip()
    stderr = str(result.stderr or '').strip()
    merged = (stdout + ('\n' + stderr if stderr else '')).strip()

    run_file = ''
    m = re.search(r'RUN_FILE:\s*(.+)', merged)
    if m:
        run_file = m.group(1).strip()

    if result.returncode != 0 or 'CODEX_DELEGATE_OK' not in merged:
        reason = ''
        m_reason = re.search(r'REASON:\s*(.+)', merged)
        if m_reason:
            reason = m_reason.group(1).strip()
        err = reason or (stderr or stdout or f'codex_delegate exit={result.returncode}')
        return {'ok': False, 'error': err[:500], 'raw': merged[:3000], 'runFile': run_file}

    final_message = _extract_between_markers(merged, 'FINAL_MESSAGE_BEGIN', 'FINAL_MESSAGE_END')
    if not final_message:
        return {'ok': False, 'error': 'codex_delegate 未返回 FINAL_MESSAGE', 'raw': merged[:3000], 'runFile': run_file}
    return {'ok': True, 'raw': final_message, 'runFile': run_file}


def _load_learning_plans():
    data = atomic_json_read(LEARNING_PLAN_FILE, {'plans': []})
    if not isinstance(data, dict):
        return {'plans': []}
    plans = data.get('plans')
    if not isinstance(plans, list):
        data['plans'] = []
    return data


def _save_learning_plans(data):
    if not isinstance(data, dict):
        data = {'plans': []}
    if not isinstance(data.get('plans'), list):
        data['plans'] = []
    atomic_json_write(LEARNING_PLAN_FILE, data)


def _new_learning_id():
    ms = int(time.time() * 1000)
    return f'LRN-{datetime.datetime.fromtimestamp(ms/1000.0):%Y%m%d-%H%M%S}{ms % 1000:03d}'


def _build_libu_question_prompt(topic: str):
    topic = (topic or '').strip()
    return (
        "你是扫地僧，负责藏经阁学习体系设计。\n"
        "请先做第一阶段：仅产出 10 个高质量澄清问题，用于后续制定个人学习路径。\n"
        "要求：\n"
        "1) 只输出 JSON 对象，不要 Markdown，不要解释。\n"
        "2) JSON 格式固定为："
        "{\"questions\":[{\"id\":\"Q1\",\"question\":\"...\",\"why\":\"...\"},...]}。\n"
        "3) questions 必须正好 10 条，id 必须是 Q1~Q10。\n"
        "4) 问题应覆盖：目标、时间、基础、资源、偏好、约束、评估、成果。\n\n"
        f"学习主题：{topic}\n"
    )


def _build_libu_plan_prompt(topic: str, qa_pairs):
    qa_text = '\n'.join([f"- {x.get('id')}: {x.get('question')}\n  回答: {x.get('answer')}" for x in qa_pairs])
    return (
        "你是扫地僧，负责藏经阁学习体系设计。\n"
        "现在进入第二阶段：基于主题和用户对10问的回答，生成“目录式学习计划（像书本目录）”和知识架构。\n"
        "只输出 JSON 对象，不要 Markdown，不要解释。\n"
        "JSON 格式：\n"
        "{\n"
        "  \"learner_profile\": \"...\",\n"
        "  \"curriculum\": [\n"
        "    {\n"
        "      \"id\": \"T1\",\n"
        "      \"title\": \"主题标题\",\n"
        "      \"phase\": \"所属阶段（可选）\",\n"
        "      \"objective\": \"该主题学习目标\",\n"
        "      \"content\": \"扫地僧准备的核心学习内容（结构化文本）\",\n"
        "      \"key_points\": [\"要点1\", \"要点2\"],\n"
        "      \"resources\": [{\"title\":\"资源名\",\"type\":\"course|article|book|video|tool\",\"link\":\"https://...\"}],\n"
        "      \"practice\": [\"练习1\", \"练习2\"]\n"
        "    }\n"
        "  ],\n"
        "  \"knowledge_map_mermaid\": \"flowchart LR ...\",\n"
        "  \"learning_path\": [\n"
        "    {\"phase\":\"阶段名\",\"goal\":\"阶段目标\",\"duration\":\"建议时长\",\"milestones\":[\"里程碑\"],\"deliverables\":[\"产出\"]}\n"
        "  ]\n"
        "}\n"
        "约束：\n"
        "1) curriculum 必须细化到至少 12 个主题（建议 12~24），禁止只给 3 个大阶段。\n"
        "2) 每个主题都要能独立学习并支持后续问答扩展。\n"
        "3) 每个主题的 content 要具体，不少于 120 字，不能只写一句话。\n"
        "4) phase 允许重复（多个主题属于同一阶段），但展示时应体现细分主题而非粗粒度阶段。\n"
        "5) link 若不确定可留空字符串，但字段必须存在。\n\n"
        "6) knowledge_map_mermaid 必须使用 Mermaid flowchart（推荐 flowchart LR），必须包含箭头 -->，并体现层级关系。\n"
        "7) 图中至少包含：总目标节点、阶段节点、主题节点、输出节点；可用 subgraph 提升结构清晰度。\n\n"
        f"学习主题：{topic}\n"
        "用户问答：\n"
        f"{qa_text}\n"
    )


def _normalize_questions(payload, topic):
    questions = []
    if isinstance(payload, dict) and isinstance(payload.get('questions'), list):
        for i, q in enumerate(payload.get('questions')[:10], start=1):
            if not isinstance(q, dict):
                continue
            qq = str(q.get('question') or '').strip()
            if not qq:
                continue
            questions.append({
                'id': f'Q{i}',
                'question': qq,
                'why': str(q.get('why') or '').strip(),
            })
    if len(questions) != 10:
        return _default_learning_questions(topic)
    return questions


def _normalize_plan_payload(payload):
    if not isinstance(payload, dict):
        return None
    learner_profile = str(payload.get('learner_profile') or '').strip()
    knowledge_map_mermaid = str(payload.get('knowledge_map_mermaid') or '').strip()
    learning_path = payload.get('learning_path')
    if not isinstance(learning_path, list):
        learning_path = []

    curriculum = payload.get('curriculum')
    if isinstance(curriculum, list) and curriculum:
        normalized = []
        for i, item in enumerate(curriculum, start=1):
            if not isinstance(item, dict):
                continue
            title = str(item.get('title') or '').strip()
            if not title:
                continue
            tid = str(item.get('id') or f'T{i}').strip() or f'T{i}'
            key_points = item.get('key_points') if isinstance(item.get('key_points'), list) else []
            practice = item.get('practice') if isinstance(item.get('practice'), list) else []
            resources = item.get('resources') if isinstance(item.get('resources'), list) else []
            normalized.append({
                'id': tid,
                'order': i,
                'title': title,
                'phase': str(item.get('phase') or '').strip(),
                'objective': str(item.get('objective') or '').strip(),
                'content': str(item.get('content') or '').strip(),
                'key_points': [str(x).strip() for x in key_points if str(x).strip()],
                'resources': [r for r in resources if isinstance(r, dict)],
                'practice': [str(x).strip() for x in practice if str(x).strip()],
                'supplements': [],
            })
        if normalized:
            return {
                'learner_profile': learner_profile,
                'knowledge_map_mermaid': knowledge_map_mermaid,
                'learning_path': learning_path,
                'curriculum': normalized,
            }

    # 兼容旧格式：learning_path + key_points
    key_points = payload.get('key_points')
    if not isinstance(learning_path, list) or len(learning_path) < 1:
        return None
    if not isinstance(key_points, list) or len(key_points) < 1:
        return None

    curriculum_fallback = []
    kp = [x for x in key_points if isinstance(x, dict)]
    per = max(1, len(kp) // max(1, len(learning_path)))
    idx = 0
    for i, ph in enumerate(learning_path, start=1):
        if not isinstance(ph, dict):
            continue
        phase_name = str(ph.get('phase') or f'阶段{i}')
        group = kp[idx: idx + per] if i < len(learning_path) else kp[idx:]
        idx += per
        content_lines = []
        for g in group[:6]:
            topic = str(g.get('topic') or '').strip()
            details = str(g.get('details') or '').strip()
            if topic or details:
                content_lines.append(f'{topic}: {details}'.strip(': '))
        curriculum_fallback.append({
            'id': f'T{i}',
            'order': i,
            'title': phase_name,
            'phase': phase_name,
            'objective': str(ph.get('goal') or '').strip(),
            'content': '\n'.join(content_lines)[:4000],
            'key_points': [str(g.get('topic') or '').strip() for g in group if str(g.get('topic') or '').strip()],
            'resources': [r for r in ph.get('resources', []) if isinstance(r, dict)] if isinstance(ph.get('resources'), list) else [],
            'practice': [str(x).strip() for x in (ph.get('deliverables') or []) if str(x).strip()],
            'supplements': [],
        })
    if not curriculum_fallback:
        return None
    return {
        'learner_profile': learner_profile,
        'knowledge_map_mermaid': knowledge_map_mermaid,
        'learning_path': learning_path,
        'curriculum': curriculum_fallback,
    }


def list_learning_plans():
    data = _load_learning_plans()
    changed = False
    for p in data.get('plans', []):
        if not isinstance(p, dict):
            continue
        if p.get('status') == 'planned':
            result = p.get('result') or {}
            if isinstance(result, dict) and not isinstance(result.get('curriculum'), list):
                upgraded = _normalize_plan_payload(result)
                if upgraded and isinstance(upgraded.get('curriculum'), list):
                    p['result'] = upgraded
                    changed = True
            if 'topicChats' not in p or not isinstance(p.get('topicChats'), dict):
                p['topicChats'] = {}
                changed = True
    if changed:
        _save_learning_plans(data)
    plans = data.get('plans', [])
    plans_sorted = sorted(plans, key=lambda x: x.get('updatedAt', ''), reverse=True)
    return {'ok': True, 'plans': plans_sorted}


def get_learning_plan(plan_id):
    data = _load_learning_plans()
    plan = next((p for p in data.get('plans', []) if p.get('id') == plan_id), None)
    if not plan:
        return {'ok': False, 'error': f'学习计划 {plan_id} 不存在'}
    changed = False
    if plan.get('status') == 'planned':
        result = plan.get('result') or {}
        if isinstance(result, dict) and not isinstance(result.get('curriculum'), list):
            upgraded = _normalize_plan_payload(result)
            if upgraded and isinstance(upgraded.get('curriculum'), list):
                plan['result'] = upgraded
                changed = True
        if 'topicChats' not in plan or not isinstance(plan.get('topicChats'), dict):
            plan['topicChats'] = {}
            changed = True
    if changed:
        _save_learning_plans(data)
    return {'ok': True, 'plan': plan}


def start_learning_plan(topic):
    topic = str(topic or '').strip()
    if not topic:
        return {'ok': False, 'error': 'topic 不能为空'}
    if len(topic) > 200:
        topic = topic[:200]

    ai_result = _run_agent_sync('libu', _build_libu_question_prompt(topic), timeout_sec=420)
    raw = ai_result.get('raw', '')
    parsed = _extract_json_payload(raw) if ai_result.get('ok') else None
    questions = _normalize_questions(parsed, topic)

    plan = {
        'id': _new_learning_id(),
        'topic': topic,
        'status': 'questioning',
        'questions': questions,
        'answers': [''] * 10,
        'result': {},
        'source': 'libu' if ai_result.get('ok') else 'fallback',
        'rawQuestionOutput': raw[:12000] if raw else '',
        'createdAt': now_iso(),
        'updatedAt': now_iso(),
    }
    data = _load_learning_plans()
    plans = data.get('plans', [])
    plans.insert(0, plan)
    data['plans'] = plans
    _save_learning_plans(data)
    return {'ok': True, 'plan': plan, 'message': '扫地僧10问已生成'}


def answer_learning_plan(plan_id, answers):
    data = _load_learning_plans()
    plans = data.get('plans', [])
    plan = next((p for p in plans if p.get('id') == plan_id), None)
    if not plan:
        return {'ok': False, 'error': f'学习计划 {plan_id} 不存在'}
    if not isinstance(answers, list) or len(answers) != 10:
        return {'ok': False, 'error': 'answers 必须是长度为10的数组'}
    norm_answers = [str(a or '').strip() for a in answers]
    if sum(1 for a in norm_answers if a) < 6:
        return {'ok': False, 'error': '请至少回答 6 个问题后再生成路径'}

    qa_pairs = []
    for i, q in enumerate(plan.get('questions', [])[:10], start=1):
        qa_pairs.append({
            'id': f'Q{i}',
            'question': str((q or {}).get('question') or ''),
            'answer': norm_answers[i - 1] if i - 1 < len(norm_answers) else '',
        })

    ai_result = _run_agent_sync('libu', _build_libu_plan_prompt(plan.get('topic', ''), qa_pairs), timeout_sec=600)
    raw = ai_result.get('raw', '')
    parsed = _extract_json_payload(raw) if ai_result.get('ok') else None
    norm = _normalize_plan_payload(parsed)
    if not norm:
        return {'ok': False, 'error': '扫地僧返回格式无法解析，请重试一次', 'raw': raw[:1000]}

    plan['answers'] = norm_answers
    plan['status'] = 'planned'
    plan['result'] = norm
    plan.setdefault('topicChats', {})
    plan['rawPlanOutput'] = raw[:20000] if raw else ''
    plan['updatedAt'] = now_iso()
    _save_learning_plans(data)
    return {'ok': True, 'plan': plan, 'message': '学习路径与知识架构已生成'}


def _find_plan_and_topic(plan, topic_id):
    result = (plan or {}).get('result', {})
    curriculum = result.get('curriculum', []) if isinstance(result, dict) else []
    topic = next((t for t in curriculum if str(t.get('id')) == str(topic_id)), None)
    return topic, curriculum


def _build_topic_chat_prompt(plan, topic, message, chat_history):
    profile = str((plan.get('result') or {}).get('learner_profile') or '')
    resources_text = '\n'.join([
        f"- {str(r.get('title') or '').strip()} ({str(r.get('type') or '').strip()}): {str(r.get('link') or '').strip()}"
        for r in (topic.get('resources') or []) if isinstance(r, dict)
    ])
    history_lines = []
    for x in chat_history[-8:]:
        role = '用户' if x.get('role') == 'user' else '扫地僧'
        history_lines.append(f"{role}: {str(x.get('text') or '').strip()}")
    return (
        "你是扫地僧，正在进行单主题教学辅导。请直接回答用户问题，要求清晰、可执行、贴合该主题。\n"
        "输出要求：\n"
        "1) 先给结论，再给步骤。\n"
        "2) 尽量给一个小练习和一个常见误区。\n"
        "3) 不要输出 JSON，直接输出自然语言。\n\n"
        f"学习总主题: {plan.get('topic', '')}\n"
        f"学习者画像: {profile}\n"
        f"当前小主题: {topic.get('title', '')}\n"
        f"主题目标: {topic.get('objective', '')}\n"
        f"主题内容:\n{topic.get('content', '')}\n"
        f"主题要点: {'；'.join(topic.get('key_points', []) or [])}\n"
        f"主题资源:\n{resources_text}\n"
        "最近对话:\n"
        f"{chr(10).join(history_lines)}\n\n"
        f"用户本次问题: {message}\n"
    )


def chat_learning_topic(plan_id, topic_id, message):
    text = str(message or '').strip()
    if not text:
        return {'ok': False, 'error': 'message 不能为空'}

    data = _load_learning_plans()
    plans = data.get('plans', [])
    plan = next((p for p in plans if p.get('id') == plan_id), None)
    if not plan:
        return {'ok': False, 'error': f'学习计划 {plan_id} 不存在'}
    if plan.get('status') != 'planned':
        return {'ok': False, 'error': '该计划尚未生成目录与学习内容'}

    topic, _ = _find_plan_and_topic(plan, topic_id)
    if not topic:
        return {'ok': False, 'error': f'主题 {topic_id} 不存在'}

    topic_chats = plan.setdefault('topicChats', {})
    chat = topic_chats.setdefault(str(topic_id), [])
    chat.append({'role': 'user', 'text': text, 'at': now_iso()})

    prompt = _build_topic_chat_prompt(plan, topic, text, chat)
    ai_result = _run_agent_sync('libu', prompt, timeout_sec=360)
    if not ai_result.get('ok'):
        return {'ok': False, 'error': ai_result.get('error', '扫地僧回复失败')}
    reply = str(ai_result.get('raw') or '').strip()[:10000]
    if not reply:
        reply = '我已理解你的问题。请你再具体一点，我会按步骤解答。'
    chat.append({'role': 'assistant', 'text': reply, 'at': now_iso()})
    plan['updatedAt'] = now_iso()
    _save_learning_plans(data)
    return {'ok': True, 'reply': reply, 'chat': chat, 'plan': plan}


def summarize_learning_topic(plan_id, topic_id):
    data = _load_learning_plans()
    plans = data.get('plans', [])
    plan = next((p for p in plans if p.get('id') == plan_id), None)
    if not plan:
        return {'ok': False, 'error': f'学习计划 {plan_id} 不存在'}
    if plan.get('status') != 'planned':
        return {'ok': False, 'error': '该计划尚未生成目录与学习内容'}

    topic, _ = _find_plan_and_topic(plan, topic_id)
    if not topic:
        return {'ok': False, 'error': f'主题 {topic_id} 不存在'}

    chat = ((plan.get('topicChats') or {}).get(str(topic_id)) or [])
    if len(chat) < 2:
        return {'ok': False, 'error': '该主题暂无可总结问答'}

    chat_text = '\n'.join([f"{'用户' if x.get('role')=='user' else '扫地僧'}: {x.get('text','')}" for x in chat[-20:]])
    prompt = (
        "你是扫地僧。请根据以下用户问答，提炼为可并入学习内容的补充笔记。\n"
        "输出要求：\n"
        "1) 直接输出自然语言，不要 JSON。\n"
        "2) 结构包含：关键补充点、易错点、建议练习、下一步学习建议。\n"
        "3) 200-500 字。\n\n"
        f"总主题: {plan.get('topic', '')}\n"
        f"当前小主题: {topic.get('title', '')}\n"
        f"原有主题内容: {topic.get('content', '')}\n"
        "问答记录:\n"
        f"{chat_text}\n"
    )
    ai_result = _run_agent_sync('libu', prompt, timeout_sec=360)
    if not ai_result.get('ok'):
        return {'ok': False, 'error': ai_result.get('error', '扫地僧总结失败')}
    summary = str(ai_result.get('raw') or '').strip()[:10000]
    if not summary:
        return {'ok': False, 'error': '扫地僧未返回总结内容'}

    supplements = topic.setdefault('supplements', [])
    supplements.append({
        'at': now_iso(),
        'source': 'qa-summary',
        'content': summary,
    })
    plan['updatedAt'] = now_iso()
    _save_learning_plans(data)
    return {'ok': True, 'summary': summary, 'plan': plan}


def delete_learning_topic(plan_id, topic_id):
    data = _load_learning_plans()
    plans = data.get('plans', [])
    plan = next((p for p in plans if p.get('id') == plan_id), None)
    if not plan:
        return {'ok': False, 'error': f'学习计划 {plan_id} 不存在'}
    if plan.get('status') != 'planned':
        return {'ok': False, 'error': '该计划尚未生成目录与学习内容'}
    result = plan.get('result') or {}
    curriculum = result.get('curriculum', []) if isinstance(result, dict) else []
    idx = next((i for i, t in enumerate(curriculum) if str(t.get('id')) == str(topic_id)), -1)
    if idx < 0:
        return {'ok': False, 'error': f'主题 {topic_id} 不存在'}
    removed = curriculum.pop(idx)
    result['curriculum'] = curriculum
    plan['result'] = result
    topic_chats = plan.get('topicChats') or {}
    topic_chats.pop(str(topic_id), None)
    plan['topicChats'] = topic_chats
    plan['updatedAt'] = now_iso()
    _save_learning_plans(data)
    return {'ok': True, 'plan': plan, 'topic': removed}

def delete_learning_plan(plan_id):
    data = _load_learning_plans()
    plans = data.get('plans', [])
    idx = next((i for i, p in enumerate(plans) if p.get('id') == plan_id), -1)
    if idx < 0:
        return {'ok': False, 'error': f'学习计划 {plan_id} 不存在'}
    removed = plans.pop(idx)
    data['plans'] = plans
    _save_learning_plans(data)
    return {'ok': True, 'deletedPlanId': plan_id, 'plan': removed}


def _load_pm_data():
    data = atomic_json_read(PM_FILE, {'projects': []})
    if not isinstance(data, dict):
        return {'projects': []}
    if not isinstance(data.get('projects'), list):
        data['projects'] = []
    return data


def _save_pm_data(data):
    if not isinstance(data, dict):
        data = {'projects': []}
    if not isinstance(data.get('projects'), list):
        data['projects'] = []
    atomic_json_write(PM_FILE, data)


def _load_automation_data():
    data = atomic_json_read(AUTOMATION_FILE, {'tasks': []})
    if not isinstance(data, dict):
        data = {'tasks': []}
    tasks = data.get('tasks')
    if not isinstance(tasks, list):
        tasks = []
    normalized = []
    for it in tasks:
        if not isinstance(it, dict):
            continue
        task_id = str(it.get('id') or '').strip()
        if not task_id:
            continue
        target_agent = str(it.get('targetAgent') or 'shangshu').strip()
        if target_agent not in AUTOMATION_ALLOWED_AGENTS:
            target_agent = 'shangshu'
        target_session = str(it.get('targetSession') or '').strip()
        logs = it.get('logs') if isinstance(it.get('logs'), list) else []
        normalized.append({
            'id': task_id,
            'title': str(it.get('title') or '未命名任务').strip() or '未命名任务',
            'requestText': str(it.get('requestText') or '').strip(),
            'scheduleExpr': str(it.get('scheduleExpr') or '').strip(),
            'targetAgent': target_agent,
            'targetSession': target_session,
            'prompt': str(it.get('prompt') or '').strip(),
            'statusFeedback': str(it.get('statusFeedback') or '').strip(),
            'experienceFeedback': str(it.get('experienceFeedback') or '').strip(),
            'enabled': bool(it.get('enabled', True)),
            'createdAt': str(it.get('createdAt') or now_iso()),
            'updatedAt': str(it.get('updatedAt') or now_iso()),
            'lastRunAt': str(it.get('lastRunAt') or ''),
            'logs': [x for x in logs if isinstance(x, dict)][-50:],
        })
    return {'tasks': normalized}


def _save_automation_data(data):
    if not isinstance(data, dict):
        data = {'tasks': []}
    if not isinstance(data.get('tasks'), list):
        data['tasks'] = []
    atomic_json_write(AUTOMATION_FILE, data)


def automation_list_tasks():
    data = _load_automation_data()
    tasks = sorted(data.get('tasks', []), key=lambda x: str(x.get('updatedAt') or ''), reverse=True)
    return {'ok': True, 'tasks': tasks}


def _parse_automation_request(text):
    raw = str(text or '').strip()
    lower = raw.lower()
    schedule_expr = ''
    m = re.search(r'每天\s*([01]?\d|2[0-3])[:：]([0-5]\d)', raw)
    if m:
        schedule_expr = f'每日 {int(m.group(1)):02d}:{m.group(2)}'
    if not schedule_expr:
        m = re.search(r'每周([一二三四五六日天])\s*([01]?\d|2[0-3])[:：]([0-5]\d)', raw)
        if m:
            schedule_expr = f'每周{m.group(1)} {int(m.group(2)):02d}:{m.group(3)}'
    if not schedule_expr:
        m = re.search(r'每(\d+)\s*分钟', raw)
        if m:
            schedule_expr = f'每{m.group(1)}分钟'
    if not schedule_expr:
        m = re.search(r'每(\d+)\s*小时', raw)
        if m:
            schedule_expr = f'每{m.group(1)}小时'
    if not schedule_expr and ('每小时' in raw or '每 1 小时' in raw):
        schedule_expr = '每1小时'
    if not schedule_expr:
        m = re.search(r'每工作日\s*([01]?\d|2[0-3])[:：]([0-5]\d)', raw)
        if m:
            schedule_expr = f'每工作日 {int(m.group(1)):02d}:{m.group(2)}'

    target_agent = 'shangshu'
    if any(k in raw for k in ('研发', '研发部', '研发总监', 'rnd')):
        target_agent = 'rnd'
    elif any(k in raw for k in ('PM', '项目经理', 'bingbu', '兵部')):
        target_agent = 'bingbu'
    elif any(k in raw for k in ('人事', '人事部', '人事经理', 'libu_hr', '吏部')):
        target_agent = 'libu_hr'
    elif any(k in raw for k in ('藏经阁', '扫地僧', 'libu', '礼部')):
        target_agent = 'libu'
    elif any(k in raw for k in ('尚书', 'shangshu')):
        target_agent = 'shangshu'
    elif any(k in raw for k in ('中书', 'zhongshu')):
        target_agent = 'zhongshu'
    elif any(k in raw for k in ('门下', 'menxia')):
        target_agent = 'menxia'
    elif any(k in raw for k in ('太子', 'taizi')):
        target_agent = 'taizi'
    elif any(k in raw for k in ('刑部', 'xingbu')):
        target_agent = 'xingbu'
    elif any(k in raw for k in ('户部', 'hubu')):
        target_agent = 'hubu'
    elif any(k in raw for k in ('钦天监', 'zaochao')):
        target_agent = 'zaochao'
    parsed_prompt = raw
    return {
        'scheduleExpr': schedule_expr,
        'targetAgent': target_agent,
        'targetSession': '',
        'prompt': parsed_prompt,
    }


def _normalize_automation_parsed_payload(payload, fallback=None):
    fb = fallback if isinstance(fallback, dict) else {}
    if not isinstance(payload, dict):
        payload = {}
    schedule_expr = str(payload.get('scheduleExpr') or fb.get('scheduleExpr') or '').strip()
    target_agent = str(payload.get('targetAgent') or fb.get('targetAgent') or 'shangshu').strip()
    target_session = str(payload.get('targetSession') or fb.get('targetSession') or '').strip()
    prompt = str(payload.get('prompt') or fb.get('prompt') or '').strip()
    if target_agent not in AUTOMATION_ALLOWED_AGENTS:
        target_agent = str(fb.get('targetAgent') or 'shangshu').strip()
        if target_agent not in AUTOMATION_ALLOWED_AGENTS:
            target_agent = 'shangshu'
    if not prompt:
        prompt = str(fb.get('prompt') or '').strip()
    return {
        'scheduleExpr': schedule_expr,
        'targetAgent': target_agent,
        'targetSession': target_session,
        'prompt': prompt,
    }


def _parse_automation_request_with_shangshu(text, fallback):
    raw_text = str(text or '').strip()
    if not raw_text:
        return {'ok': False, 'error': 'text empty'}
    prompt = (
        "你是能效部长 Agent（shangshu），请把用户的自动化任务描述解析为执行配置。\n"
        "只输出 JSON 对象，不要 markdown，不要解释。\n"
        "JSON 格式固定：\n"
        "{\n"
        "  \"scheduleExpr\":\"\",\n"
        "  \"targetAgent\":\"\",\n"
        "  \"targetSession\":\"\",\n"
        "  \"prompt\":\"\"\n"
        "}\n"
        "约束：\n"
        "1) scheduleExpr 尽量输出中文规则，如“每日 09:30”“每周一 10:00”“每1小时”。\n"
        "2) targetAgent 必须在集合内：taizi, zhongshu, menxia, shangshu, libu, hubu, bingbu, xingbu, rnd, libu_hr, zaochao。\n"
        "3) targetSession 可为空字符串。\n"
        "4) prompt 要写成可直接执行的完整提示词，不要太短。\n\n"
        f"用户任务描述：\n{raw_text}\n"
    )
    ai = _run_agent_sync('shangshu', prompt, timeout_sec=180)
    if not ai.get('ok'):
        return {'ok': False, 'error': str(ai.get('error') or 'shangshu parse failed').strip()}
    parsed = _extract_json_payload(str(ai.get('raw') or ''))
    if not isinstance(parsed, dict):
        return {'ok': False, 'error': 'shangshu 输出非JSON'}
    normalized = _normalize_automation_parsed_payload(parsed, fallback=fallback)
    if not normalized.get('prompt'):
        return {'ok': False, 'error': 'shangshu 未生成 prompt'}
    return {'ok': True, 'parsed': normalized}


def automation_parse_request(text):
    fallback = _parse_automation_request(text)
    ai_parsed = _parse_automation_request_with_shangshu(text, fallback=fallback)
    if ai_parsed.get('ok'):
        return {'ok': True, 'parsed': ai_parsed.get('parsed') or fallback, 'source': 'shangshu'}
    return {
        'ok': True,
        'parsed': fallback,
        'source': 'rule_fallback',
        'warning': f"shangshu 解析失败，已回退规则解析：{str(ai_parsed.get('error') or 'unknown')[:160]}",
    }


def automation_create_task(title, request_text, schedule_expr='', target_agent='shangshu', target_session='', prompt=''):
    title = str(title or '').strip() or '未命名任务'
    request_text = str(request_text or '').strip()
    if not request_text:
        return {'ok': False, 'error': 'requestText required'}
    parsed = _parse_automation_request(request_text)
    if not schedule_expr:
        schedule_expr = parsed.get('scheduleExpr', '')
    if not target_agent:
        target_agent = parsed.get('targetAgent', 'shangshu')
    if not prompt:
        prompt = parsed.get('prompt', request_text)
    if target_agent not in AUTOMATION_ALLOWED_AGENTS:
        target_agent = 'shangshu'

    data = _load_automation_data()
    task = {
        'id': 'AUTO-' + datetime.datetime.now().strftime('%Y%m%d%H%M%S') + str(uuid.uuid4().hex[:4]),
        'title': title,
        'requestText': request_text,
        'scheduleExpr': str(schedule_expr or '').strip(),
        'targetAgent': target_agent,
        'targetSession': str(target_session or '').strip(),
        'prompt': str(prompt or '').strip(),
        'statusFeedback': '',
        'experienceFeedback': '',
        'enabled': False,
        'createdAt': now_iso(),
        'updatedAt': now_iso(),
        'lastRunAt': '',
        'logs': [],
    }
    data['tasks'].append(task)
    _save_automation_data(data)
    return {'ok': True, 'task': task}


def automation_update_task(task_id, patch):
    tid = str(task_id or '').strip()
    if not tid:
        return {'ok': False, 'error': 'taskId required'}
    data = _load_automation_data()
    tasks = data.get('tasks', [])
    t = next((x for x in tasks if str(x.get('id') or '') == tid), None)
    if not t:
        return {'ok': False, 'error': 'task not found'}
    if not isinstance(patch, dict):
        patch = {}
    for k in ('title', 'requestText', 'scheduleExpr', 'targetSession', 'prompt', 'statusFeedback', 'experienceFeedback'):
        if k in patch and patch[k] is not None:
            t[k] = str(patch[k]).strip()
    if 'enabled' in patch:
        t['enabled'] = bool(patch.get('enabled'))
    if 'targetAgent' in patch and patch.get('targetAgent') is not None:
        ag = str(patch.get('targetAgent')).strip()
        if ag in AUTOMATION_ALLOWED_AGENTS:
            t['targetAgent'] = ag
    t['updatedAt'] = now_iso()
    _save_automation_data(data)
    return {'ok': True, 'task': t}


def automation_delete_task(task_id):
    tid = str(task_id or '').strip()
    if not tid:
        return {'ok': False, 'error': 'taskId required'}
    data = _load_automation_data()
    tasks = data.get('tasks', [])
    idx = next((i for i, x in enumerate(tasks) if str(x.get('id') or '') == tid), -1)
    if idx < 0:
        return {'ok': False, 'error': 'task not found'}
    removed = tasks.pop(idx)
    data['tasks'] = tasks
    _save_automation_data(data)
    return {'ok': True, 'task': removed}


def automation_run_task(task_id, status_feedback='', experience_feedback=''):
    tid = str(task_id or '').strip()
    if not tid:
        return {'ok': False, 'error': 'taskId required'}
    data = _load_automation_data()
    t = next((x for x in data.get('tasks', []) if str(x.get('id') or '') == tid), None)
    if not t:
        return {'ok': False, 'error': 'task not found'}
    run_at = now_iso()
    if status_feedback is not None:
        t['statusFeedback'] = str(status_feedback).strip()
    if experience_feedback is not None:
        t['experienceFeedback'] = str(experience_feedback).strip()
    t['lastRunAt'] = run_at
    t['updatedAt'] = run_at
    t_logs = t.get('logs') if isinstance(t.get('logs'), list) else []
    t_logs.append({
        'at': run_at,
        'statusFeedback': t.get('statusFeedback', ''),
        'experienceFeedback': t.get('experienceFeedback', ''),
        'result': 'manual_run',
    })
    t['logs'] = t_logs[-50:]
    _save_automation_data(data)
    return {'ok': True, 'task': t}


def _load_agent_isolation_registry():
    data = atomic_json_read(PM_ISOLATION_FILE, {'version': 1, 'scopes': {}})
    if not isinstance(data, dict):
        return {'version': 1, 'scopes': {}}
    scopes = data.get('scopes')
    if not isinstance(scopes, dict):
        scopes = {}
    data['version'] = int(data.get('version') or 1)
    data['scopes'] = scopes
    return data


def _save_agent_isolation_registry(data):
    if not isinstance(data, dict):
        data = {'version': 1, 'scopes': {}}
    if not isinstance(data.get('scopes'), dict):
        data['scopes'] = {}
    data['updatedAt'] = now_iso()
    atomic_json_write(PM_ISOLATION_FILE, data)


def _normalize_work_scope_items(items):
    if not isinstance(items, list):
        return []
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        entry = str(it.get('entry', '')).strip()
        service = str(it.get('service', '')).strip()
        invoke = str(it.get('invoke', 'agent')).strip().lower()
        if invoke not in {'agent', 'ui'}:
            invoke = 'agent'
        binding_id = str(it.get('bindingId', '')).strip()
        if binding_id and not _SAFE_NAME_RE.match(binding_id):
            binding_id = ''
        match_list = it.get('match') if isinstance(it.get('match'), list) else []
        match_list = [str(x).strip() for x in match_list if str(x).strip()]
        if not entry and not service:
            continue
        out.append({
            'entry': entry or service[:32],
            'service': service or entry,
            'invoke': invoke,
            'bindingId': binding_id,
            'match': match_list,
        })
    return out


def _normalize_work_scope_payload(payload):
    scopes = {}
    if isinstance(payload, dict):
        raw = payload.get('scopes') if 'scopes' in payload else payload
        if isinstance(raw, dict):
            for agent_id, items in raw.items():
                aid = str(agent_id or '').strip()
                if not aid or not _SAFE_NAME_RE.match(aid):
                    continue
                normalized = _normalize_work_scope_items(items)
                if normalized:
                    scopes[aid] = normalized
    if not scopes:
        scopes = json.loads(json.dumps(DEFAULT_AGENT_WORK_SCOPES, ensure_ascii=False))
    return scopes


def _load_agent_work_scopes():
    data = atomic_json_read(AGENT_WORK_SCOPE_FILE, {'scopes': DEFAULT_AGENT_WORK_SCOPES})
    scopes = _normalize_work_scope_payload(data)
    return {'scopes': scopes}


def _save_agent_work_scopes(scopes):
    payload = {
        'updatedAt': now_iso(),
        'scopes': _normalize_work_scope_payload({'scopes': scopes}),
    }
    atomic_json_write(AGENT_WORK_SCOPE_FILE, payload)


def _normalize_agent_work_bindings(payload):
    raw = payload.get('bindings') if isinstance(payload, dict) and 'bindings' in payload else payload
    if not isinstance(raw, dict):
        raw = {}
    out = {}
    for k, v in raw.items():
        key = str(k or '').strip()
        if not key or not _SAFE_NAME_RE.match(key):
            continue
        item = v if isinstance(v, dict) else {}
        aid = str(item.get('agentId', '')).strip()
        src = str(item.get('source', '')).strip()
        if not aid or not _SAFE_NAME_RE.match(aid):
            continue
        if not src:
            continue
        out[key] = {'agentId': aid, 'source': src}
    if not out:
        out = json.loads(json.dumps(DEFAULT_AGENT_WORK_BINDINGS, ensure_ascii=False))
    return out


def _load_agent_work_bindings():
    data = atomic_json_read(AGENT_WORK_BINDINGS_FILE, {'bindings': DEFAULT_AGENT_WORK_BINDINGS})
    bindings = _normalize_agent_work_bindings(data)
    return {'bindings': bindings}


def _safe_slug(text, limit=24):
    s = re.sub(r'[^a-zA-Z0-9]+', '-', str(text or '').strip().lower())
    s = re.sub(r'-+', '-', s).strip('-')
    if not s:
        s = 'x'
    return s[:max(6, int(limit))]


def _safe_fs_segment(text, default='x', limit=80):
    seg = re.sub(r'[\\/:*?"<>|]+', '_', str(text or '').strip())
    seg = re.sub(r'\s+', ' ', seg).strip().strip('.')
    if not seg:
        seg = default
    return seg[:max(8, int(limit))]


def _path_within(child: pathlib.Path, parent: pathlib.Path):
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except Exception:
        return False


def _jzg_external_project_dir(project_id):
    return JZG_EXTERNAL_DOCS_DIR / _safe_fs_segment(project_id, default='project', limit=64)


def _jzg_write_external_doc_file(project_id, doc_id, file_name, file_base64):
    if not file_base64:
        return ''
    try:
        payload = base64.b64decode(str(file_base64), validate=True)
    except Exception:
        raise ValueError('fileBase64 非法或已损坏')
    if not payload:
        raise ValueError('文件内容为空')
    proj_dir = _jzg_external_project_dir(project_id)
    proj_dir.mkdir(parents=True, exist_ok=True)
    safe_name = _safe_fs_segment(file_name, default='document', limit=180)
    out_path = proj_dir / f"{_safe_fs_segment(doc_id, default='doc', limit=48)}__{safe_name}"
    with open(out_path, 'wb') as f:
        f.write(payload)
    return str(out_path)


def _build_isolation_scope_key(project_id, domain, action):
    return f"{str(project_id or '').strip()}:{_safe_slug(domain, 20)}:{_safe_slug(action, 32)}"


def _build_isolated_agent_id(base_agent, project_id, domain, action):
    digest = hashlib.sha1(_build_isolation_scope_key(project_id, domain, action).encode('utf-8')).hexdigest()[:8]
    base = _safe_slug(base_agent, 18)
    dom = _safe_slug(domain, 12)
    act = _safe_slug(action, 20)
    # 统一命名规范：<base>__<domain>__<action>__<hash>
    return f"{base}__{dom}__{act}__{digest}"


def _list_registered_agents():
    try:
        result = subprocess.run(
            ['openclaw', 'agents', 'list', '--json'],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except Exception:
        return []
    if result.returncode != 0:
        return []
    try:
        rows = json.loads(result.stdout or '[]')
    except Exception:
        return []
    return rows if isinstance(rows, list) else []


def _agent_exists(agent_id):
    rows = _list_registered_agents()
    return any(str((r or {}).get('id') or '').strip() == agent_id for r in rows if isinstance(r, dict))


def _agent_workspace_exists(agent_id):
    rows = _list_registered_agents()
    for r in rows:
        if not isinstance(r, dict):
            continue
        if str(r.get('id') or '').strip() != str(agent_id or '').strip():
            continue
        workspace = str(r.get('workspace') or '').strip()
        return bool(workspace and pathlib.Path(workspace).is_dir())
    return False


def _get_agent_model(agent_id):
    rows = _list_registered_agents()
    for r in rows:
        if not isinstance(r, dict):
            continue
        if str(r.get('id') or '').strip() == agent_id:
            model = str(r.get('model') or '').strip()
            if model:
                return model
    return ''


def _ensure_isolated_runtime_agent(base_agent, runtime_agent):
    if _agent_exists(runtime_agent):
        return {'ok': True, 'created': False}

    base_workspace = OCLAW_HOME / f'workspace-{base_agent}'
    if not base_workspace.is_dir():
        return {'ok': False, 'error': f'base workspace 不存在: {base_workspace}'}

    runtime_workspace = OCLAW_HOME / f'workspace-{runtime_agent}'
    if runtime_workspace.exists() and not runtime_workspace.is_dir():
        return {'ok': False, 'error': f'runtime workspace 非目录: {runtime_workspace}'}
    if not runtime_workspace.exists():
        shutil.copytree(
            base_workspace,
            runtime_workspace,
            dirs_exist_ok=False,
            symlinks=True,
            ignore_dangling_symlinks=True,
        )

    cmd = [
        'openclaw',
        'agents',
        'add',
        runtime_agent,
        '--non-interactive',
        '--workspace',
        str(runtime_workspace),
        '--json',
    ]
    model = _get_agent_model(base_agent)
    if model:
        cmd.extend(['--model', model])
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
    except Exception as e:
        return {'ok': False, 'error': f'创建隔离 agent 失败: {e}'}
    if result.returncode != 0:
        # 并发/重入时，可能已被其他请求创建；再次确认
        if _agent_exists(runtime_agent):
            return {'ok': True, 'created': False}
        err = (result.stderr or result.stdout or 'unknown error').strip()
        return {'ok': False, 'error': f'创建隔离 agent 失败: {err[:220]}'}
    return {'ok': True, 'created': True}


def _resolve_isolated_agent(base_agent, project_id, domain, action):
    scope_key = _build_isolation_scope_key(project_id, domain, action)
    registry = _load_agent_isolation_registry()
    scopes = registry.get('scopes') or {}
    rec = scopes.get(scope_key) if isinstance(scopes, dict) else None

    runtime_agent = ''
    if isinstance(rec, dict):
        runtime_agent = str(rec.get('runtimeAgentId') or '').strip()
        rec_base_agent = str(rec.get('baseAgentId') or '').strip()
        expected_prefix = f"{_safe_slug(base_agent, 18)}__"
        healthy_runtime = bool(
            runtime_agent
            and rec_base_agent == str(base_agent or '').strip()
            and runtime_agent.startswith(expected_prefix)
            and _agent_exists(runtime_agent)
            and _agent_workspace_exists(runtime_agent)
        )
        if healthy_runtime:
            rec['lastUsedAt'] = now_iso()
            scopes[scope_key] = rec
            registry['scopes'] = scopes
            _save_agent_isolation_registry(registry)
            return {'ok': True, 'agentId': runtime_agent, 'created': False, 'scope': scope_key}

    runtime_agent = _build_isolated_agent_id(base_agent, project_id, domain, action)
    ensured = _ensure_isolated_runtime_agent(base_agent, runtime_agent)
    if not ensured.get('ok'):
        return {'ok': False, 'error': ensured.get('error', '创建隔离路由失败')}

    scopes[scope_key] = {
        'scopeKey': scope_key,
        'baseAgentId': base_agent,
        'runtimeAgentId': runtime_agent,
        'projectId': str(project_id or '').strip(),
        'domain': _safe_slug(domain, 24),
        'action': _safe_slug(action, 40),
        'createdAt': now_iso(),
        'lastUsedAt': now_iso(),
    }
    registry['scopes'] = scopes
    _save_agent_isolation_registry(registry)
    return {'ok': True, 'agentId': runtime_agent, 'created': bool(ensured.get('created')), 'scope': scope_key}


def _new_pm_id(prefix='PM'):
    ms = int(time.time() * 1000)
    dt = datetime.datetime.fromtimestamp(ms / 1000.0)
    return f'{prefix}-{dt:%Y%m%d%H%M%S}{ms % 1000:03d}'


def _ensure_pm_project_folders(project):
    if not isinstance(project, dict):
        return
    folders = project.get('folders')
    if not isinstance(folders, list):
        folders = []
    normalized = []
    seen = set()
    for f in folders:
        if isinstance(f, dict):
            fid = str(f.get('id') or '').strip()
            name = str(f.get('name') or '').strip()
        else:
            fid = ''
            name = ''
        if not fid:
            fid = _new_pm_id('FLD')
        if not name:
            name = '默认文件夹'
        if fid in seen:
            continue
        seen.add(fid)
        normalized.append({'id': fid, 'name': name[:120]})
    if not any(f.get('id') == PM_DESIGN_FOLDER_ID for f in normalized):
        normalized.insert(0, {'id': PM_DESIGN_FOLDER_ID, 'name': PM_DESIGN_FOLDER_NAME})
    if not any(f.get('id') == PM_VERSION_FOLDER_ID for f in normalized):
        normalized.insert(1, {'id': PM_VERSION_FOLDER_ID, 'name': PM_VERSION_FOLDER_NAME})
    normalized = [
        {'id': PM_DESIGN_FOLDER_ID, 'name': PM_DESIGN_FOLDER_NAME},
        {'id': PM_VERSION_FOLDER_ID, 'name': PM_VERSION_FOLDER_NAME},
    ] + [f for f in normalized if f.get('id') not in {PM_DESIGN_FOLDER_ID, PM_VERSION_FOLDER_ID}]
    non_system = [f for f in normalized if f.get('id') not in {PM_DESIGN_FOLDER_ID, PM_VERSION_FOLDER_ID}]
    if not non_system:
        normalized.append({'id': 'FLD-DEFAULT', 'name': '默认文件夹'})
    project['folders'] = normalized
    valid_ids = {f['id'] for f in normalized}
    issue_folder_id = next(
        (f['id'] for f in normalized if f['id'] not in {PM_DESIGN_FOLDER_ID, PM_VERSION_FOLDER_ID}),
        normalized[0]['id']
    )
    for item in (project.get('items') or []):
        fid = str(item.get('folderId') or '').strip()
        if fid not in valid_ids or fid in {PM_DESIGN_FOLDER_ID, PM_VERSION_FOLDER_ID}:
            item['folderId'] = issue_folder_id


def _ensure_pm_project_design(project):
    if not isinstance(project, dict):
        return
    now = now_iso()
    design = project.get('design')
    if not isinstance(design, dict):
        design = {}
    brief = design.get('brief')
    if not isinstance(brief, dict):
        brief = {}
    brief.setdefault('content', '')
    brief.setdefault('updatedAt', now)
    brief.setdefault('updatedBy', '')
    design['brief'] = brief
    sections = design.get('sections')
    if not isinstance(sections, dict):
        sections = {}
    for sec in PM_DESIGN_SECTIONS:
        item = sections.get(sec)
        if not isinstance(item, dict):
            item = {}
        item.setdefault('content', '')
        item.setdefault('updatedAt', now)
        item.setdefault('updatedBy', '')
        raw_suggestions = item.get('suggestions')
        if not isinstance(raw_suggestions, list):
            raw_suggestions = []
        normalized_suggestions = []
        for s in raw_suggestions:
            if not isinstance(s, dict):
                continue
            sid = str(s.get('id') or '').strip() or _new_pm_id('DGN')
            text = str(s.get('text') or '').strip()
            if not text:
                continue
            st = str(s.get('status') or 'pending').strip().lower()
            if st not in PM_SUGGESTION_STATUS:
                st = 'pending'
            normalized_suggestions.append({
                'id': sid,
                'text': text[:4000],
                'status': st,
                'createdAt': str(s.get('createdAt') or now),
                'updatedAt': str(s.get('updatedAt') or now),
            })
        item['suggestions'] = normalized_suggestions
        sections[sec] = item
    design['sections'] = sections
    project['design'] = design


def _ensure_pm_project_versions(project):
    if not isinstance(project, dict):
        return
    raw = project.get('versions')
    if not isinstance(raw, list):
        raw = []
    normalized = []
    for v in raw:
        if not isinstance(v, dict):
            continue
        vid = str(v.get('id') or '').strip() or _new_pm_id('VER')
        system_tag = str(v.get('systemVersion') or v.get('version') or '').strip()
        github_tag = str(v.get('githubVersion') or '').strip()
        st = str(v.get('status') or '').strip().lower()
        if st not in PM_VERSION_STATUS:
            st = 'draft'
        normalized.append({
            'id': vid,
            'systemVersion': system_tag[:40],
            'githubVersion': github_tag[:80],
            'status': st,
            'summary': str(v.get('summary') or '').strip()[:400],
            'content': str(v.get('content') or '').strip()[:20000],
            'issueIds': [str(x).strip() for x in (v.get('issueIds') or []) if str(x).strip()][:500],
            'createdAt': str(v.get('createdAt') or now_iso()),
            'updatedAt': str(v.get('updatedAt') or now_iso()),
            'createdBy': str(v.get('createdBy') or 'rnd')[:30],
        })
    normalized.sort(key=lambda x: x.get('createdAt', ''), reverse=True)
    project['versions'] = normalized


def _ensure_pm_project_runtime(project):
    if not isinstance(project, dict):
        return
    rt = project.get('runtime')
    if not isinstance(rt, dict):
        rt = {}
    sid = str(rt.get('rndSessionId') or '').strip()
    if not sid:
        pid = str(project.get('id') or 'unknown')
        sid = f"pm-rnd-{pid}-{uuid.uuid4().hex[:8]}"
    rt['rndSessionId'] = sid
    rt['updatedAt'] = now_iso()
    project['runtime'] = rt


def _get_pm_rnd_session_id(project):
    _ensure_pm_project_runtime(project)
    rt = project.get('runtime') or {}
    return str(rt.get('rndSessionId') or '').strip()


def _reset_pm_rnd_session_id(project):
    _ensure_pm_project_runtime(project)
    rt = project.get('runtime') or {}
    pid = str(project.get('id') or 'unknown')
    sid = f"pm-rnd-{pid}-{uuid.uuid4().hex[:8]}"
    rt['rndSessionId'] = sid
    rt['updatedAt'] = now_iso()
    project['runtime'] = rt
    return sid


def _next_pm_version_tag(project):
    now = datetime.datetime.now()
    prefix = f"v{now:%Y.%m.%d}"
    existed = set()
    for x in (project.get('versions') or []):
        for key in ('systemVersion', 'version'):
            tag = str((x or {}).get(key) or '').strip()
            if tag:
                existed.add(tag)
    seq = 1
    while True:
        tag = f"{prefix}.{seq}"
        if tag not in existed:
            return tag
        seq += 1


def _build_pm_version_fallback_markdown(items):
    groups = {
        'BUG修复': [],
        '需求交付': [],
        '优化改进': [],
    }
    for it in (items or []):
        tp = str(it.get('type') or '').strip().lower()
        if tp in {'bug', '缺陷'}:
            bucket = 'BUG修复'
        elif tp in {'opt', '优化'}:
            bucket = '优化改进'
        else:
            bucket = '需求交付'
        title = str(it.get('title') or '未命名事项').strip()[:120]
        resolution = str(it.get('resolution') or '').strip().replace('\n', ' ')
        if not resolution:
            resolution = str(it.get('description') or '').strip().replace('\n', ' ')
        resolution = resolution[:140]
        groups[bucket].append((title, resolution))

    lines = ['# 版本更新日志（系统兜底生成）', '']
    for sec in ('BUG修复', '需求交付', '优化改进'):
        lines.append(f'## {sec}')
        rows = groups.get(sec) or []
        if not rows:
            lines.append('- 本次无')
        else:
            for title, desc in rows:
                if desc:
                    lines.append(f'- **{title}**：{desc}')
                else:
                    lines.append(f'- **{title}**')
        lines.append('')
    return '\n'.join(lines).strip()


def pm_update_version(project_id, version_id, version=None, status=None):
    data = _load_pm_data()
    project = _find_project(data, project_id)
    if not project:
        return {'ok': False, 'error': f'项目 {project_id} 不存在'}
    _ensure_pm_project_folders(project)
    _ensure_pm_project_design(project)
    _ensure_pm_project_versions(project)
    _ensure_pm_project_runtime(project)
    vid = str(version_id or '').strip()
    if not vid:
        return {'ok': False, 'error': 'versionId required'}
    versions = project.get('versions') or []
    target = next((x for x in versions if str(x.get('id') or '').strip() == vid), None)
    if not target:
        return {'ok': False, 'error': f'版本记录 {version_id} 不存在'}

    changed = False
    github_version = version
    if isinstance(version, dict):
        github_version = None
    if github_version is not None:
        target['githubVersion'] = str(github_version or '').strip()[:80]
        changed = True
    if status is not None:
        st = str(status or '').strip().lower()
        if st not in PM_VERSION_STATUS:
            return {'ok': False, 'error': f'不支持的版本状态: {status}'}
        target['status'] = st
        changed = True
    if changed:
        now = now_iso()
        target['updatedAt'] = now
        project['updatedAt'] = now
        # 问题单的“版本编号”固定使用系统版本号，不跟随 GitHub 版本号变动
        item_tag = str(target.get('systemVersion') or '').strip()
        for it in (project.get('items') or []):
            if str(it.get('versionRefId') or '') == str(target.get('id') or '') and str(it.get('status') or '').lower() == 'done':
                it['versionTag'] = item_tag
                it['updatedAt'] = now
        _save_pm_data(data)
    return {'ok': True, 'project': project, 'version': target}


def pm_list_projects():
    data = _load_pm_data()
    projects = data.get('projects', [])
    for p in projects:
        _ensure_pm_project_folders(p)
        _ensure_pm_project_design(p)
        _ensure_pm_project_versions(p)
        _ensure_pm_project_runtime(p)
    projects_sorted = sorted(projects, key=lambda x: x.get('updatedAt', ''), reverse=True)
    _save_pm_data(data)
    return {'ok': True, 'projects': projects_sorted}


def pm_create_project(name, description=''):
    name = str(name or '').strip()
    if not name:
        return {'ok': False, 'error': '项目名称不能为空'}
    data = _load_pm_data()
    proj = {
        'id': _new_pm_id('PRJ'),
        'name': name[:120],
        'description': str(description or '').strip()[:2000],
        'owner': 'rnd',
        'folders': [
            {'id': PM_DESIGN_FOLDER_ID, 'name': PM_DESIGN_FOLDER_NAME},
            {'id': PM_VERSION_FOLDER_ID, 'name': PM_VERSION_FOLDER_NAME},
            {'id': 'FLD-DEFAULT', 'name': '默认文件夹'},
        ],
        'design': {
            'brief': {'content': '', 'updatedAt': now_iso(), 'updatedBy': ''},
            'sections': {
                'requirements': {'content': '', 'updatedAt': now_iso(), 'updatedBy': ''},
                'architecture': {'content': '', 'updatedAt': now_iso(), 'updatedBy': ''},
                'function': {'content': '', 'updatedAt': now_iso(), 'updatedBy': ''},
            }
        },
        'items': [],
        'versions': [],
        'runtime': {
            'rndSessionId': '',
            'updatedAt': now_iso(),
        },
        'createdAt': now_iso(),
        'updatedAt': now_iso(),
    }
    _ensure_pm_project_runtime(proj)
    data['projects'].insert(0, proj)
    _save_pm_data(data)
    return {'ok': True, 'project': proj}


def pm_update_project(project_id, name=None, description=None):
    data = _load_pm_data()
    project = _find_project(data, project_id)
    if not project:
        return {'ok': False, 'error': f'项目 {project_id} 不存在'}
    if name is not None:
        n = str(name or '').strip()
        if not n:
            return {'ok': False, 'error': '项目名称不能为空'}
        project['name'] = n[:120]
    if description is not None:
        project['description'] = str(description or '').strip()[:2000]
    project['updatedAt'] = now_iso()
    _save_pm_data(data)
    return {'ok': True, 'project': project}


def pm_delete_project(project_id):
    data = _load_pm_data()
    projects = data.get('projects', [])
    idx = next((i for i, p in enumerate(projects) if p.get('id') == project_id), -1)
    if idx < 0:
        return {'ok': False, 'error': f'项目 {project_id} 不存在'}
    removed = projects.pop(idx)
    _save_pm_data(data)
    return {'ok': True, 'project': removed}


def _find_project(data, project_id):
    return next((p for p in data.get('projects', []) if p.get('id') == project_id), None)


def _find_item(project, item_id):
    return next((i for i in (project.get('items') or []) if i.get('id') == item_id), None)


def pm_create_item(project_id, title, item_type='bug', priority='P2', description=''):
    data = _load_pm_data()
    project = _find_project(data, project_id)
    if not project:
        return {'ok': False, 'error': f'项目 {project_id} 不存在'}
    _ensure_pm_project_folders(project)
    _ensure_pm_project_design(project)
    title = str(title or '').strip()
    if not title:
        return {'ok': False, 'error': '标题不能为空'}
    item_type = str(item_type or 'bug').lower()
    if item_type not in {'bug', 'req', 'opt'}:
        item_type = 'bug'
    priority = str(priority or 'P2').upper()
    if priority not in {'P0', 'P1', 'P2', 'P3'}:
        priority = 'P2'
    item = {
        'id': _new_pm_id('ISS'),
        'title': title[:200],
        'type': item_type,
        'priority': priority,
        'status': 'pending_release',
        'folderId': next(
            (f['id'] for f in project['folders'] if f['id'] not in {PM_DESIGN_FOLDER_ID, PM_VERSION_FOLDER_ID}),
            project['folders'][0]['id']
        ),
        'description': str(description or '').strip()[:6000],
        'owner': 'rnd',
        'qa': [],
        'plan': [],
        'questions': [],
        'clarifyReplies': {},
        'resolution': '',
        'createdAt': now_iso(),
        'updatedAt': now_iso(),
    }
    project.setdefault('items', []).insert(0, item)
    project['updatedAt'] = now_iso()
    _save_pm_data(data)
    return {'ok': True, 'item': item, 'project': project}


def pm_update_item(
    project_id,
    item_id,
    status=None,
    priority=None,
    resolution=None,
    item_type=None,
    description=None,
    folder_id=None,
    questions=None,
    clarify_replies=None,
    title=None,
    plan=None,
    review_suggested_title=None,
    review_suggested_description=None,
    review_suggested_by=None,
):
    data = _load_pm_data()
    project = _find_project(data, project_id)
    if not project:
        return {'ok': False, 'error': f'项目 {project_id} 不存在'}
    _ensure_pm_project_folders(project)
    _ensure_pm_project_design(project)
    item = _find_item(project, item_id)
    if not item:
        return {'ok': False, 'error': f'问题单 {item_id} 不存在'}
    if status is not None:
        s = str(status).strip().lower()
        if s in {'pending_release', 'open', 'clarify', 'in_progress', 'pending_acceptance', 'blocked', 'done'}:
            item['status'] = s
    if priority is not None:
        p = str(priority).strip().upper()
        if p in {'P0', 'P1', 'P2', 'P3'}:
            item['priority'] = p
    if item_type is not None:
        t = str(item_type).strip().lower()
        if t in {'bug', 'req', 'opt'}:
            item['type'] = t
    if description is not None:
        item['description'] = str(description or '').strip()[:6000]
    if title is not None:
        t = str(title or '').strip()
        if t:
            item['title'] = t[:200]
    if folder_id is not None:
        fid = str(folder_id or '').strip()
        valid_ids = {f.get('id') for f in (project.get('folders') or [])}
        if fid in valid_ids and fid not in {PM_DESIGN_FOLDER_ID, PM_VERSION_FOLDER_ID}:
            item['folderId'] = fid
    if resolution is not None:
        item['resolution'] = str(resolution or '').strip()[:8000]
    if questions is not None:
        if isinstance(questions, list):
            normalized_questions = []
            for q in questions:
                s = str(q or '').strip()
                if not s:
                    continue
                normalized_questions.append(s[:500])
            item['questions'] = normalized_questions[:20]
            if isinstance(item.get('clarifyReplies'), dict):
                allowed = set(item['questions'])
                item['clarifyReplies'] = {
                    str(k): str(v)
                    for k, v in item.get('clarifyReplies', {}).items()
                    if str(k) in allowed and str(v).strip()
                }
    if clarify_replies is not None:
        if isinstance(clarify_replies, dict):
            allowed_questions = set(
                str(q or '').strip()
                for q in (item.get('questions') or [])
                if str(q or '').strip()
            )
            normalized_replies = {}
            for k, v in clarify_replies.items():
                q = str(k or '').strip()[:500]
                a = str(v or '').strip()[:3000]
                if not q or not a:
                    continue
                if allowed_questions and q not in allowed_questions:
                    continue
                normalized_replies[q] = a
                if len(normalized_replies) >= 30:
                    break
            item['clarifyReplies'] = normalized_replies
    if plan is not None:
        if isinstance(plan, list):
            normalized_plan = []
            for step in plan:
                s = str(step or '').strip()
                if not s:
                    continue
                normalized_plan.append(s[:500])
                if len(normalized_plan) >= 20:
                    break
            item['plan'] = normalized_plan
    if review_suggested_title is not None:
        t = str(review_suggested_title or '').strip()
        if t:
            item['reviewSuggestedTitle'] = t[:300]
        else:
            item.pop('reviewSuggestedTitle', None)
    if review_suggested_description is not None:
        d = str(review_suggested_description or '').strip()
        if d:
            item['reviewSuggestedDescription'] = d[:4000]
        else:
            item.pop('reviewSuggestedDescription', None)
    if review_suggested_by is not None:
        b = str(review_suggested_by or '').strip().lower()
        if b in {'codex', 'rnd'}:
            item['reviewSuggestedBy'] = b
        elif not b:
            item.pop('reviewSuggestedBy', None)
    item['updatedAt'] = now_iso()
    project['updatedAt'] = now_iso()
    _save_pm_data(data)
    return {'ok': True, 'item': item, 'project': project}


def pm_delete_item(project_id, item_id):
    data = _load_pm_data()
    project = _find_project(data, project_id)
    if not project:
        return {'ok': False, 'error': f'项目 {project_id} 不存在'}
    items = project.get('items') or []
    idx = next((i for i, it in enumerate(items) if it.get('id') == item_id), -1)
    if idx < 0:
        return {'ok': False, 'error': f'问题单 {item_id} 不存在'}
    removed = items.pop(idx)
    project['items'] = items
    project['updatedAt'] = now_iso()
    _save_pm_data(data)
    return {'ok': True, 'item': removed, 'project': project}


def pm_create_folder(project_id, name):
    data = _load_pm_data()
    project = _find_project(data, project_id)
    if not project:
        return {'ok': False, 'error': f'项目 {project_id} 不存在'}
    _ensure_pm_project_folders(project)
    _ensure_pm_project_design(project)
    nm = str(name or '').strip()
    if not nm:
        return {'ok': False, 'error': '文件夹名称不能为空'}
    if nm in {PM_DESIGN_FOLDER_NAME, PM_VERSION_FOLDER_NAME}:
        return {'ok': False, 'error': f'文件夹名称 {nm} 为系统保留'}
    if any(str(f.get('name') or '').strip() == nm for f in (project.get('folders') or [])):
        return {'ok': False, 'error': '文件夹名称已存在'}
    folder = {'id': _new_pm_id('FLD'), 'name': nm[:120]}
    project.setdefault('folders', []).append(folder)
    project['updatedAt'] = now_iso()
    _save_pm_data(data)
    return {'ok': True, 'folder': folder, 'project': project}


def pm_update_folder(project_id, folder_id, name):
    data = _load_pm_data()
    project = _find_project(data, project_id)
    if not project:
        return {'ok': False, 'error': f'项目 {project_id} 不存在'}
    _ensure_pm_project_folders(project)
    _ensure_pm_project_design(project)
    fid = str(folder_id or '').strip()
    if fid in {PM_DESIGN_FOLDER_ID, PM_VERSION_FOLDER_ID}:
        return {'ok': False, 'error': '系统目录不可修改'}
    folder = next((f for f in (project.get('folders') or []) if f.get('id') == fid), None)
    if not folder:
        return {'ok': False, 'error': f'文件夹 {folder_id} 不存在'}
    nm = str(name or '').strip()
    if not nm:
        return {'ok': False, 'error': '文件夹名称不能为空'}
    if nm in {PM_DESIGN_FOLDER_NAME, PM_VERSION_FOLDER_NAME}:
        return {'ok': False, 'error': f'文件夹名称 {nm} 为系统保留'}
    if any((f.get('id') != fid and str(f.get('name') or '').strip() == nm) for f in (project.get('folders') or [])):
        return {'ok': False, 'error': '文件夹名称已存在'}
    folder['name'] = nm[:120]
    project['updatedAt'] = now_iso()
    _save_pm_data(data)
    return {'ok': True, 'folder': folder, 'project': project}


def pm_delete_folder(project_id, folder_id):
    data = _load_pm_data()
    project = _find_project(data, project_id)
    if not project:
        return {'ok': False, 'error': f'项目 {project_id} 不存在'}
    _ensure_pm_project_folders(project)
    _ensure_pm_project_design(project)
    fid = str(folder_id or '').strip()
    if fid in {PM_DESIGN_FOLDER_ID, PM_VERSION_FOLDER_ID}:
        return {'ok': False, 'error': '系统目录不可删除'}
    folders = project.get('folders') or []
    folder = next((f for f in folders if f.get('id') == fid), None)
    if not folder:
        return {'ok': False, 'error': f'文件夹 {folder_id} 不存在'}
    if len(folders) <= 1:
        return {'ok': False, 'error': '至少保留一个文件夹，无法删除'}
    items = project.get('items') or []
    used = [it for it in items if (it.get('folderId') or (folders[0] or {}).get('id')) == fid]
    if used:
        return {'ok': False, 'error': f'该文件夹下仍有 {len(used)} 条问题，无法删除'}
    project['folders'] = [f for f in folders if f.get('id') != fid]
    project['updatedAt'] = now_iso()
    _save_pm_data(data)
    return {'ok': True, 'folder': folder, 'project': project}


def pm_reorder_folder(project_id, source_folder_id, target_folder_id, place='before'):
    data = _load_pm_data()
    project = _find_project(data, project_id)
    if not project:
        return {'ok': False, 'error': f'项目 {project_id} 不存在'}
    _ensure_pm_project_folders(project)
    _ensure_pm_project_design(project)

    source_id = str(source_folder_id or '').strip()
    target_id = str(target_folder_id or '').strip()
    if not source_id or not target_id:
        return {'ok': False, 'error': 'sourceFolderId 和 targetFolderId 不能为空'}
    if source_id == target_id:
        return {'ok': True, 'project': project}
    if source_id in {PM_DESIGN_FOLDER_ID, PM_VERSION_FOLDER_ID}:
        return {'ok': False, 'error': '系统目录不可拖拽排序'}
    if target_id in {PM_DESIGN_FOLDER_ID, PM_VERSION_FOLDER_ID}:
        return {'ok': False, 'error': '不可拖拽到系统目录位置'}

    folders = project.get('folders') or []
    non_system = [f for f in folders if str(f.get('id') or '') not in {PM_DESIGN_FOLDER_ID, PM_VERSION_FOLDER_ID}]

    src_idx = next((i for i, f in enumerate(non_system) if str(f.get('id') or '') == source_id), -1)
    tgt_idx = next((i for i, f in enumerate(non_system) if str(f.get('id') or '') == target_id), -1)
    if src_idx < 0 or tgt_idx < 0:
        return {'ok': False, 'error': '拖拽目标文件夹不存在'}

    src_folder = non_system.pop(src_idx)
    tgt_idx = next((i for i, f in enumerate(non_system) if str(f.get('id') or '') == target_id), -1)
    if tgt_idx < 0:
        non_system.append(src_folder)
    else:
        insert_after = str(place or '').strip().lower() == 'after'
        insert_idx = tgt_idx + (1 if insert_after else 0)
        non_system.insert(insert_idx, src_folder)

    design_folder = next((f for f in folders if str(f.get('id') or '') == PM_DESIGN_FOLDER_ID), {'id': PM_DESIGN_FOLDER_ID, 'name': PM_DESIGN_FOLDER_NAME})
    version_folder = next((f for f in folders if str(f.get('id') or '') == PM_VERSION_FOLDER_ID), {'id': PM_VERSION_FOLDER_ID, 'name': PM_VERSION_FOLDER_NAME})
    project['folders'] = [design_folder, version_folder] + non_system
    project['updatedAt'] = now_iso()
    _save_pm_data(data)
    return {'ok': True, 'project': project}


def pm_update_design(project_id, section, content, updated_by='user'):
    data = _load_pm_data()
    project = _find_project(data, project_id)
    if not project:
        return {'ok': False, 'error': f'项目 {project_id} 不存在'}
    _ensure_pm_project_folders(project)
    _ensure_pm_project_design(project)
    sec = str(section or '').strip().lower()
    if sec == 'brief':
        node = project['design']['brief']
        node['content'] = str(content or '')[:2000]
        node['updatedAt'] = now_iso()
        node['updatedBy'] = str(updated_by or 'user')[:30]
        project['updatedAt'] = now_iso()
        _save_pm_data(data)
        return {'ok': True, 'project': project, 'section': sec, 'design': node}
    if sec not in PM_DESIGN_SECTIONS:
        return {'ok': False, 'error': f'不支持的设计章节: {section}'}
    node = project['design']['sections'][sec]
    node['content'] = str(content or '')[:30000]
    node['updatedAt'] = now_iso()
    node['updatedBy'] = str(updated_by or 'user')[:30]
    project['updatedAt'] = now_iso()
    _save_pm_data(data)
    return {'ok': True, 'project': project, 'section': sec, 'design': node}


def pm_create_design_suggestion(project_id, section, text):
    data = _load_pm_data()
    project = _find_project(data, project_id)
    if not project:
        return {'ok': False, 'error': f'项目 {project_id} 不存在'}
    _ensure_pm_project_folders(project)
    _ensure_pm_project_design(project)
    sec = str(section or '').strip().lower()
    if sec not in PM_DESIGN_SECTIONS:
        return {'ok': False, 'error': f'不支持的设计章节: {section}'}
    msg = str(text or '').strip()
    if not msg:
        return {'ok': False, 'error': '整改建议不能为空'}
    node = project['design']['sections'][sec]
    now = now_iso()
    item = {
        'id': _new_pm_id('DGN'),
        'text': msg[:4000],
        'status': 'pending',
        'createdAt': now,
        'updatedAt': now,
    }
    node.setdefault('suggestions', []).insert(0, item)
    node['updatedAt'] = now
    project['updatedAt'] = now
    _save_pm_data(data)
    return {'ok': True, 'project': project, 'section': sec, 'suggestion': item, 'design': node}


def pm_update_design_suggestion(project_id, section, suggestion_id, text=None, status=None):
    data = _load_pm_data()
    project = _find_project(data, project_id)
    if not project:
        return {'ok': False, 'error': f'项目 {project_id} 不存在'}
    _ensure_pm_project_folders(project)
    _ensure_pm_project_design(project)
    sec = str(section or '').strip().lower()
    if sec not in PM_DESIGN_SECTIONS:
        return {'ok': False, 'error': f'不支持的设计章节: {section}'}
    sid = str(suggestion_id or '').strip()
    if not sid:
        return {'ok': False, 'error': 'suggestionId required'}
    node = project['design']['sections'][sec]
    suggestions = node.get('suggestions') or []
    target = next((x for x in suggestions if str(x.get('id')) == sid), None)
    if not target:
        return {'ok': False, 'error': f'整改建议 {sid} 不存在'}
    changed = False
    if text is not None:
        msg = str(text or '').strip()
        if not msg:
            return {'ok': False, 'error': '整改建议不能为空'}
        target['text'] = msg[:4000]
        changed = True
    if status is not None:
        st = str(status or '').strip().lower()
        if st not in PM_SUGGESTION_STATUS:
            return {'ok': False, 'error': f'不支持的状态: {status}'}
        target['status'] = st
        changed = True
    if changed:
        now = now_iso()
        target['updatedAt'] = now
        node['updatedAt'] = now
        project['updatedAt'] = now
        _save_pm_data(data)
    return {'ok': True, 'project': project, 'section': sec, 'suggestion': target, 'design': node}


def pm_delete_design_suggestion(project_id, section, suggestion_id):
    data = _load_pm_data()
    project = _find_project(data, project_id)
    if not project:
        return {'ok': False, 'error': f'项目 {project_id} 不存在'}
    _ensure_pm_project_folders(project)
    _ensure_pm_project_design(project)
    sec = str(section or '').strip().lower()
    if sec not in PM_DESIGN_SECTIONS:
        return {'ok': False, 'error': f'不支持的设计章节: {section}'}
    sid = str(suggestion_id or '').strip()
    if not sid:
        return {'ok': False, 'error': 'suggestionId required'}
    node = project['design']['sections'][sec]
    suggestions = node.get('suggestions') or []
    idx = next((i for i, x in enumerate(suggestions) if str(x.get('id')) == sid), -1)
    if idx < 0:
        return {'ok': False, 'error': f'整改建议 {sid} 不存在'}
    removed = suggestions.pop(idx)
    now = now_iso()
    node['updatedAt'] = now
    project['updatedAt'] = now
    _save_pm_data(data)
    return {'ok': True, 'project': project, 'section': sec, 'suggestion': removed, 'design': node}


def pm_generate_design(project_id, section):
    data = _load_pm_data()
    project = _find_project(data, project_id)
    if not project:
        return {'ok': False, 'error': f'项目 {project_id} 不存在'}
    _ensure_pm_project_folders(project)
    _ensure_pm_project_design(project)
    _ensure_pm_project_runtime(project)
    sec = str(section or '').strip().lower()
    if sec not in PM_DESIGN_SECTIONS:
        return {'ok': False, 'error': f'不支持的设计章节: {section}'}

    titles = {
        'requirements': '需求说明（PRD）',
        'architecture': '架构设计（含流程图）',
        'function': '功能设计（FSD）',
    }
    hints = {
        'requirements': '请输出结构化 PRD，至少包含背景/目标、用户与场景、功能范围、非功能需求、里程碑与验收标准。',
        'architecture': '请输出顶层架构设计，包含 Mermaid 流程图/结构图代码块，以及关键模块职责、数据流、边界与风险。',
        'function': '请基于 PRD 输出 FSD，包含功能拆解、流程、接口与字段、状态机/异常、测试要点。',
    }
    latest_items = project.get('items') or []
    latest_txt = '\n'.join([f"- [{it.get('status')}] {it.get('title')}" for it in latest_items[:20]])
    brief_text = str((((project.get('design') or {}).get('brief') or {}).get('content') or '')).strip()
    node = project['design']['sections'][sec]
    current_content = str(node.get('content') or '').strip()
    suggestions = node.get('suggestions') or []
    pending_suggestions = [s for s in suggestions if str(s.get('status') or '').lower() == 'pending']
    pending_txt = '\n'.join([f"- {s.get('text')}" for s in pending_suggestions])
    rewrite_keywords = ('重新编写', '重写', '推倒重写', '从零编写', '全量重写', '完全重写')
    rewrite_requested = any(any(k in str(s.get('text') or '') for k in rewrite_keywords) for s in pending_suggestions)
    generate_mode = 'rewrite' if rewrite_requested else 'incremental'
    mode_rule = (
        "本次为【全量重写模式】：存在“重新编写/重写”类明确要求，请重构整篇文档，但仍需覆盖待采纳整改建议。"
        if rewrite_requested else
        "本次为【增量修订模式】：必须先理解当前已有文档，再在其基础上做小步修改；禁止无故整篇重写。"
    )
    prompt = (
        "你是研发总监，负责项目设计文档产出。\n"
        f"项目名称：{project.get('name')}\n"
        f"项目说明：{project.get('description')}\n"
        f"用户给定的一句话方向：{brief_text or '（未提供）'}\n"
        f"目标章节：{titles[sec]}\n"
        f"生成模式：{generate_mode}\n"
        f"{mode_rule}\n"
        f"问题清单参考（节选）：\n{latest_txt or '- 暂无'}\n\n"
        f"当前已有文档（请先学习后再修改）：\n{current_content[:12000] or '- 当前为空'}\n\n"
        f"待采纳整改建议（必须逐条落实）：\n{pending_txt or '- 暂无'}\n\n"
        f"{hints[sec]}\n"
        "输出要求：仅输出 Markdown 正文，不要输出解释。"
    )
    lane = _resolve_isolated_agent('rnd', project.get('id', ''), 'pm', f'design-{sec}')
    if not lane.get('ok'):
        return {'ok': False, 'error': lane.get('error', '隔离路由失败')}
    ai = _run_agent_sync(lane['agentId'], prompt, timeout_sec=300)
    if not ai.get('ok'):
        return {'ok': False, 'error': ai.get('error', '研发部生成失败')}
    content = str(ai.get('raw') or '').strip()
    if not content:
        return {'ok': False, 'error': '研发部未返回有效内容'}

    node['content'] = content[:30000]
    now = now_iso()
    node['updatedAt'] = now
    node['updatedBy'] = 'rnd'
    for s in pending_suggestions:
        s['status'] = 'adopted'
        s['updatedAt'] = now
    project['updatedAt'] = now
    _save_pm_data(data)
    return {'ok': True, 'project': project, 'section': sec, 'design': node}


def pm_generate_version(project_id):
    data = _load_pm_data()
    project = _find_project(data, project_id)
    if not project:
        return {'ok': False, 'error': f'项目 {project_id} 不存在'}
    _ensure_pm_project_folders(project)
    _ensure_pm_project_design(project)
    _ensure_pm_project_versions(project)
    _ensure_pm_project_runtime(project)

    versions = project.get('versions') or []
    latest = versions[0] if versions else None
    latest_is_draft = bool(latest and str(latest.get('status') or '').lower() == 'draft')
    existing_issue_ids = set()
    if latest_is_draft:
        existing_issue_ids = {str(x).strip() for x in (latest.get('issueIds') or []) if str(x).strip()}

    done_items = []
    for it in (project.get('items') or []):
        if str(it.get('status') or '').lower() != 'done':
            continue
        # 以“版本编号（versionTag）是否为空”为准；空即纳入本次版本并回填
        if str(it.get('versionTag') or '').strip():
            continue
        done_items.append(it)

    item_map = {str(it.get('id') or '').strip(): it for it in (project.get('items') or []) if str(it.get('id') or '').strip()}
    final_ids = []
    for iid in existing_issue_ids:
        if iid in item_map:
            final_ids.append(iid)
    for it in done_items:
        iid = str(it.get('id') or '').strip()
        if iid and iid not in final_ids:
            final_ids.append(iid)
    if not final_ids:
        # 自愈：若当前草稿版本日志是 overflow 文本，则允许基于该草稿已纳入的问题重建日志
        if latest_is_draft and _looks_like_context_overflow((latest or {}).get('content') or ''):
            reuse_ids = [str(x).strip() for x in ((latest or {}).get('issueIds') or []) if str(x).strip()]
            final_ids = [iid for iid in reuse_ids if iid in item_map]
            if not final_ids:
                return {'ok': False, 'error': '暂无可汇总的问题（已完成且未标记版本）'}
        else:
            return {'ok': False, 'error': '暂无可汇总的问题（已完成且未标记版本）'}
    final_items = [item_map[iid] for iid in final_ids if iid in item_map][:180]

    brief = str((((project.get('design') or {}).get('brief') or {}).get('content') or '')).strip()
    issue_txt = []
    for it in final_items:
        desc = str(it.get('description') or '').strip().replace('\n', ' ')
        reso = str(it.get('resolution') or '').strip().replace('\n', ' ')
        issue_txt.append(
            f"- [{it.get('type','-').upper()}|{it.get('priority','-')}] {it.get('title','')}\n"
            f"  描述: {desc[:160]}\n"
            f"  结论: {reso[:160]}"
        )
    issue_txt = issue_txt[:120]
    prompt = (
        "你是研发总监，负责输出版本更改清单。\n"
        "请基于已完成的问题，生成一份简洁、可读的版本更新日志。\n"
        "要求：\n"
        "1) 输出 Markdown。\n"
        "2) 优先按类别分组（BUG修复/需求交付/优化改进）。\n"
        "3) 每条 1-2 句，突出用户可感知变化。\n"
        "4) 不要编造未提供的内容。\n\n"
        f"项目名称：{project.get('name','')}\n"
        f"项目方向：{brief or '（未提供）'}\n"
        "待汇总的问题：\n"
        + '\n'.join(issue_txt)
    )
    lane = _resolve_isolated_agent('rnd', project.get('id', ''), 'pm', 'version-generate')
    if not lane.get('ok'):
        return {'ok': False, 'error': lane.get('error', '隔离路由失败')}
    ai = _run_agent_sync(lane['agentId'], prompt, timeout_sec=300)
    if ai.get('ok') and _looks_like_context_overflow(ai.get('raw') or ''):
        ai = {'ok': False, 'error': str(ai.get('raw') or '').strip()}
    if (not ai.get('ok')) and _looks_like_context_overflow(ai.get('error') or ''):
        # 出现上下文溢出后，降载重试
        mini_prompt = (
            "你是研发总监，负责输出版本更改清单。\n"
            "请严格输出 Markdown，并按以下三段：\n"
            "## BUG修复\n## 需求交付\n## 优化改进\n"
            "每条 1 句，禁止编造。\n\n"
            f"项目: {project.get('name','')}\n"
            f"纳入问题数: {len(final_items)}\n"
            "问题摘要:\n" + '\n'.join(
                f"- [{str(it.get('type') or '').upper()}|{str(it.get('priority') or '')}] "
                f"{str(it.get('title') or '')[:120]}"
                for it in final_items[:80]
            )
        )
        ai = _run_agent_sync(lane['agentId'], mini_prompt, timeout_sec=240)
        if ai.get('ok') and _looks_like_context_overflow(ai.get('raw') or ''):
            ai = {'ok': False, 'error': str(ai.get('raw') or '').strip()}
    if not ai.get('ok'):
        err_text = str(ai.get('error') or '').strip()
        # provider 兼容：非零退出但正文在 error 字段里时，按正文继续
        if err_text and ('## ' in err_text or '- ' in err_text):
            content = err_text
        else:
            return {'ok': False, 'error': f"研发部版本汇总失败: {str(ai.get('error') or '未知错误')[:220]}"}
    else:
        content = str(ai.get('raw') or '').strip()
    if (not content) or _looks_like_context_overflow(content):
        return {'ok': False, 'error': '研发部版本汇总失败: 模型返回无效内容（疑似上下文过长）'}

    now = now_iso()
    if latest_is_draft:
        ver = latest
        # 若草稿版本号为空或与其他版本重复，自动重排为下一个可用版本号
        cur_tag = str(ver.get('systemVersion') or '').strip()
        dup = False
        if cur_tag:
            for vv in (project.get('versions') or []):
                if vv is ver:
                    continue
                if str(vv.get('systemVersion') or vv.get('version') or '').strip() == cur_tag:
                    dup = True
                    break
        if (not cur_tag) or dup:
            ver['systemVersion'] = _next_pm_version_tag(project)
        ver.setdefault('githubVersion', '')
        ver['summary'] = f"本次纳入 {len(final_items)} 项已完成问题"
        ver['content'] = content[:20000]
        ver['issueIds'] = [str(it.get('id') or '').strip() for it in final_items if str(it.get('id') or '').strip()]
        ver['updatedAt'] = now
        mode = 'updated'
    else:
        ver = {
            'id': _new_pm_id('VER'),
            'systemVersion': _next_pm_version_tag(project),
            'githubVersion': '',
            'status': 'draft',
            'summary': f"本次纳入 {len(final_items)} 项已完成问题",
            'content': content[:20000],
            'issueIds': [str(it.get('id') or '').strip() for it in final_items if str(it.get('id') or '').strip()],
            'createdAt': now,
            'updatedAt': now,
            'createdBy': 'rnd',
        }
        project.setdefault('versions', []).insert(0, ver)
        mode = 'created'
    # 点击“更新版本”后，统一给无版本编号的问题打上当前系统版本号
    item_tag = str(ver.get('systemVersion') or '').strip()
    for it in final_items:
        it['versionRefId'] = ver['id']
        it['versionTag'] = item_tag
        it['versionedAt'] = now
        it['updatedAt'] = now
    project['updatedAt'] = now
    _save_pm_data(data)
    return {'ok': True, 'project': project, 'version': ver, 'mode': mode}


def pm_add_reply(project_id, item_id, text, role='user'):
    data = _load_pm_data()
    project = _find_project(data, project_id)
    if not project:
        return {'ok': False, 'error': f'项目 {project_id} 不存在'}
    item = _find_item(project, item_id)
    if not item:
        return {'ok': False, 'error': f'问题单 {item_id} 不存在'}
    msg = str(text or '').strip()
    if not msg:
        return {'ok': False, 'error': 'reply 不能为空'}
    r = str(role or 'user').strip().lower()
    if r not in {'user', 'codex', 'rnd'}:
        r = 'user'
    item.setdefault('qa', []).append({
        'id': _new_pm_id('QAR'),
        'role': r,
        'text': msg[:8000],
        'at': now_iso(),
    })
    item['updatedAt'] = now_iso()
    project['updatedAt'] = now_iso()
    _save_pm_data(data)
    return {'ok': True, 'item': item, 'project': project}


def pm_delete_reply(project_id, item_id, reply_index):
    data = _load_pm_data()
    project = _find_project(data, project_id)
    if not project:
        return {'ok': False, 'error': f'项目 {project_id} 不存在'}
    item = _find_item(project, item_id)
    if not item:
        return {'ok': False, 'error': f'问题单 {item_id} 不存在'}
    qa = item.get('qa') if isinstance(item.get('qa'), list) else []
    try:
        idx = int(reply_index)
    except Exception:
        return {'ok': False, 'error': 'replyIndex 非法'}
    if idx < 0 or idx >= len(qa):
        return {'ok': False, 'error': '留言不存在或已删除'}
    qa.pop(idx)
    item['qa'] = qa
    item['updatedAt'] = now_iso()
    project['updatedAt'] = now_iso()
    _save_pm_data(data)
    return {'ok': True, 'item': item, 'project': project}


def _build_pm_rnd_prompt(project, item, mode='review'):
    qas = item.get('qa') or []

    def _clip_text(t, limit=500):
        s = str(t or '').strip()
        if len(s) <= limit:
            return s
        return s[:limit] + f"...(截断{len(s)-limit}字)"

    def _qa_role_label(x):
        role = str((x or {}).get('role') or '').strip().lower()
        if role == 'user':
            return '用户'
        if role == 'codex':
            return 'Codex'
        return '研发部'

    # 动态裁剪留言：严格限长，优先保留最新内容
    qtxt_lines = []
    qtxt_budget = 1200
    for x in reversed(qas[-8:]):
        line = f"{_qa_role_label(x)}: {_clip_text(x.get('text',''), 180)}"
        if sum(len(i) + 1 for i in qtxt_lines) + len(line) > qtxt_budget:
            break
        qtxt_lines.append(line)
    qtxt_lines.reverse()
    qtxt = '\n'.join(qtxt_lines)

    desc = _clip_text(item.get('description', ''), 900)
    resolution = _clip_text(item.get('resolution', ''), 500)
    return (
        "你是研发总监，负责软件项目的问题治理、流程优化和推进提醒。\n"
        "请根据项目问题单直接给出优化后的标题、问题描述、待澄清问题与执行计划。\n"
        "只输出 JSON 对象，不要 markdown。\n"
        "格式：\n"
        "{\n"
        "  \"summary\":\"一句话结论\",\n"
        "  \"status\":\"in_progress|done|blocked\",\n"
        "  \"optimizedTitle\":\"优化后的标题\",\n"
        "  \"optimizedDescription\":\"优化后的问题描述\",\n"
        "  \"questions\":[\"待澄清问题1\"],\n"
        "  \"plan\":[\"下一步1\",\"下一步2\"]\n"
        "}\n"
        "约束：\n"
        "1) 优先给出如何实现需求的建议与可落地拆分步骤。\n"
        "2) questions 最多 5 条，必须具体可回答；信息充分时可返回空数组。\n"
        "3) status 默认用 in_progress；仅在确有结论时用 done；确实无法推进时才用 blocked。\n"
        "4) 不要输出修复结论，不要写 resolution 字段。\n\n"
        f"模式: {mode}\n"
        f"项目: {project.get('name','')}\n"
        f"问题单ID: {item.get('id','')}\n"
        f"类型: {item.get('type','')}\n"
        f"优先级: {item.get('priority','')}\n"
        f"标题: {item.get('title','')}\n"
        f"描述: {desc}\n"
        f"当前修复结论(仅供参考，不要写入输出): {resolution}\n"
        f"留言信息:\n{qtxt}\n"
    )


def _looks_like_context_overflow(text):
    s = str(text or '').strip().lower()
    if not s:
        return False
    # 仅匹配“明确报错”场景，避免把正常业务文本里的术语误判为溢出
    if 'prompt too large for the model' in s:
        return True
    if s.startswith('context overflow'):
        return True
    if s.startswith('error: context overflow'):
        return True
    if 'maximum context length' in s:
        return True
    if 'token limit exceeded' in s:
        return True
    if ('/reset' in s) and ('context' in s or 'prompt' in s):
        return True
    if '上下文溢出' in s or '上下文过长' in s:
        return True
    return False


def pm_rnd_review(project_id, item_id, mode='review'):
    data = _load_pm_data()
    project = _find_project(data, project_id)
    if not project:
        return {'ok': False, 'error': f'项目 {project_id} 不存在'}
    item = _find_item(project, item_id)
    if not item:
        return {'ok': False, 'error': f'问题单 {item_id} 不存在'}

    _ensure_pm_project_runtime(project)
    prompt = _build_pm_rnd_prompt(project, item, mode=mode)
    lane = _resolve_isolated_agent('rnd', project.get('id', ''), 'pm', f'rnd-review-{mode}')
    if not lane.get('ok'):
        return {'ok': False, 'error': lane.get('error', '隔离路由失败')}

    review_author = 'codex'
    warning = ''
    delegate_task_id = f"{item.get('id','PM-RND')}-{str(mode or 'review').strip()}"
    ai = _run_codex_delegate_sync(
        task_id=delegate_task_id,
        prompt=prompt,
        agent_id='rnd',
        timeout_sec=300,
    )
    if ai.get('ok') and _looks_like_context_overflow(ai.get('raw') or ''):
        ai = {'ok': False, 'error': str(ai.get('raw') or '').strip()}

    # codex_delegate 不可用时，自动回退到研发部隔离会话
    if not ai.get('ok'):
        review_author = 'rnd'
        warning = f"Codex不可用，已回退研发部agent：{str(ai.get('error') or 'unknown')[:120]}"
        ai = _run_agent_sync(lane['agentId'], prompt, timeout_sec=480)
        if ai.get('ok') and _looks_like_context_overflow(ai.get('raw') or ''):
            ai = {'ok': False, 'error': str(ai.get('raw') or '').strip()}

        # 自动降载重试：出现上下文溢出时，用极简上下文再试一次
        if (not ai.get('ok')) and _looks_like_context_overflow(ai.get('error') or ''):
            mini_prompt = (
                "你是研发总监。请只输出 JSON。\n"
                "{\n"
                "  \"summary\":\"一句话结论\",\n"
                "  \"status\":\"in_progress|done|blocked\",\n"
                "  \"optimizedTitle\":\"优化后的标题\",\n"
                "  \"optimizedDescription\":\"优化后的问题描述\",\n"
                "  \"questions\":[\"待澄清问题1\"],\n"
                "  \"plan\":[\"步骤1\"]\n"
                "}\n"
                "要求：优先给出实现建议与拆分步骤；questions 最多5条。\n"
                f"模式: {mode}\n"
                f"项目: {project.get('name','')}\n"
                f"问题单ID: {item.get('id','')}\n"
                f"类型: {item.get('type','')}\n"
                f"优先级: {item.get('priority','')}\n"
                f"标题: {str(item.get('title',''))[:200]}\n"
                f"描述: {str(item.get('description',''))[:900]}\n"
                f"最近留言: {str(((item.get('qa') or [])[-1].get('text') if (item.get('qa') or []) else '') or '')[:500]}\n"
            )
            ai = _run_agent_sync(lane['agentId'], mini_prompt, timeout_sec=240)
            if ai.get('ok') and _looks_like_context_overflow(ai.get('raw') or ''):
                ai = {'ok': False, 'error': str(ai.get('raw') or '').strip()}

    if not ai.get('ok'):
        return {'ok': False, 'error': f"研发部复审失败: {str(ai.get('error') or '未知错误')[:220]}"}
    raw = str(ai.get('raw') or '').strip()
    parsed = _extract_json_payload(raw)
    if not isinstance(parsed, dict):
        if _looks_like_context_overflow(raw):
            return {'ok': False, 'error': '研发部复审失败: 输出仍发生上下文溢出'}
        # 兜底：当模型返回非 JSON（常见为固定四段文本）时，自动提取为结构化结果
        parsed = _repair_pm_review_json_from_text(raw)
        if not isinstance(parsed, dict):
            return {'ok': False, 'error': f"研发部复审失败: 输出非JSON（{raw[:120]}）"}

    # 复审仅更新建议内容，不改变任务状态
    optimized_title = str(parsed.get('optimizedTitle') or '').strip()
    optimized_description = str(parsed.get('optimizedDescription') or '').strip()
    questions = parsed.get('questions') if isinstance(parsed.get('questions'), list) else []
    plan = parsed.get('plan') if isinstance(parsed.get('plan'), list) else []
    summary = str(parsed.get('summary') or '').strip()

    item['questions'] = [str(x).strip()[:500] for x in questions if str(x).strip()][:5]
    old_replies = item.get('clarifyReplies') if isinstance(item.get('clarifyReplies'), dict) else {}
    item['clarifyReplies'] = {q: str(old_replies.get(q, '')).strip()[:3000] for q in item['questions'] if str(old_replies.get(q, '')).strip()}
    item['plan'] = [str(x).strip() for x in plan if str(x).strip()][:20]
    if optimized_title:
        item['reviewSuggestedTitle'] = optimized_title[:300]
    if optimized_description:
        item['reviewSuggestedDescription'] = optimized_description[:4000]
    item['reviewSuggestedBy'] = review_author
    item.setdefault('qa', []).append({
        'role': review_author,
        'text': (summary + ('\n\n' if summary else '') + '\n'.join(item['plan']))[:9000],
        'at': now_iso()
    })
    item['lastGongbuRaw'] = raw[:20000]
    item['updatedAt'] = now_iso()
    project['updatedAt'] = now_iso()
    _save_pm_data(data)
    resp = {'ok': True, 'item': item, 'project': project}
    if warning:
        resp['warning'] = warning
    return resp


def _load_jzg_data():
    data = atomic_json_read(JZG_FILE, {'projects': []})
    if not isinstance(data, dict):
        return {'projects': []}
    if not isinstance(data.get('projects'), list):
        data['projects'] = []
    return data


def _save_jzg_data(data):
    if not isinstance(data, dict):
        data = {'projects': []}
    if not isinstance(data.get('projects'), list):
        data['projects'] = []
    atomic_json_write(JZG_FILE, data)


def _ensure_jzg_project(project):
    if not isinstance(project, dict):
        return
    now = now_iso()
    project.setdefault('id', _new_pm_id('JZG'))
    project.setdefault('name', '未命名项目')
    project.setdefault('description', '')
    owner_raw = str(project.get('owner') or '').strip().lower()
    if owner_raw in {'libu_hr', 'libu', '吏部'}:
        project['owner'] = 'bingbu'
    project.setdefault('owner', 'bingbu')
    project['folders'] = [
        {'id': JZG_FOLLOWUP_FOLDER_ID, 'name': JZG_FOLLOWUP_FOLDER_NAME},
        {'id': JZG_STRATEGY_FOLDER_ID, 'name': JZG_STRATEGY_FOLDER_NAME},
        {'id': JZG_BOARD_FOLDER_ID, 'name': JZG_BOARD_FOLDER_NAME},
    ]
    followups = project.get('followups')
    if not isinstance(followups, dict):
        followups = {}
    if not isinstance(followups.get('items'), list):
        followups['items'] = []
    for item in (followups.get('items') or []):
        if not isinstance(item, dict):
            continue
        item.setdefault('completedAt', '')
        item.setdefault('description', '')
        item.setdefault('memo', '')
        item.setdefault('priority', 'P2')
        item.setdefault('category', '通用')
        item.setdefault('dueDate', '')
    if not isinstance(followups.get('daily'), list):
        followups['daily'] = []
    if not isinstance(followups.get('dailyReports'), list):
        followups['dailyReports'] = []
    for rep in (followups.get('dailyReports') or []):
        if not isinstance(rep, dict):
            continue
        rep.setdefault('id', _new_pm_id('JDR'))
        rep.setdefault('date', '')
        rep.setdefault('report', '')
        rep.setdefault('createdAt', now)
    tpl = followups.get('reportTemplates')
    if not isinstance(tpl, dict):
        tpl = {}
    tpl.setdefault('daily', JZG_DEFAULT_DAILY_TEMPLATE)
    tpl.setdefault('weekly', JZG_DEFAULT_WEEKLY_TEMPLATE)
    followups['reportTemplates'] = tpl
    if not isinstance(followups.get('plan'), dict):
        followups['plan'] = {}
    plan = followups.get('plan') or {}
    if not isinstance(plan.get('rows'), list):
        plan['rows'] = [
            {'name': '需求梳理', 'start': '', 'end': '', 'owner': '', 'progress': 0},
            {'name': '方案设计', 'start': '', 'end': '', 'owner': '', 'progress': 0},
            {'name': '开发实现', 'start': '', 'end': '', 'owner': '', 'progress': 0},
            {'name': '测试验收', 'start': '', 'end': '', 'owner': '', 'progress': 0},
            {'name': '上线发布', 'start': '', 'end': '', 'owner': '', 'progress': 0},
        ]
    plan.setdefault('updatedAt', now)
    followups['plan'] = plan
    followups.setdefault('updatedAt', now)
    project['followups'] = followups

    strategy = project.get('strategy')
    if not isinstance(strategy, dict):
        strategy = {}
    if not isinstance(strategy.get('topics'), list):
        strategy['topics'] = []
    docs = strategy.get('docs')
    if not isinstance(docs, dict):
        docs = {}
    if not isinstance(docs.get('folders'), list):
        docs['folders'] = []
    if not docs['folders']:
        docs['folders'] = [{
            'id': JZG_DOC_DEFAULT_FOLDER_ID,
            'name': JZG_DOC_DEFAULT_FOLDER_NAME,
            'order': 0,
            'createdAt': now,
            'updatedAt': now,
        }]
    normalized_folders = []
    for idx, fd in enumerate(docs.get('folders') or []):
        if not isinstance(fd, dict):
            continue
        fid = str(fd.get('id') or '').strip() or _new_pm_id('JFD')
        name = str(fd.get('name') or '').strip() or f'目录{idx+1}'
        normalized_folders.append({
            'id': fid[:80],
            'name': name[:80],
            'order': idx,
            'createdAt': str(fd.get('createdAt') or now),
            'updatedAt': str(fd.get('updatedAt') or now),
        })
    if not any(str(x.get('id') or '') == JZG_DOC_DEFAULT_FOLDER_ID for x in normalized_folders):
        normalized_folders.insert(0, {
            'id': JZG_DOC_DEFAULT_FOLDER_ID,
            'name': JZG_DOC_DEFAULT_FOLDER_NAME,
            'order': 0,
            'createdAt': now,
            'updatedAt': now,
        })
    for idx, fd in enumerate(normalized_folders):
        fd['order'] = idx
    docs['folders'] = normalized_folders
    valid_folder_ids = {str(fd.get('id') or '') for fd in normalized_folders}
    if not isinstance(docs.get('items'), list):
        docs['items'] = []
    normalized_items = []
    for it in (docs.get('items') or []):
        if not isinstance(it, dict):
            continue
        did = str(it.get('id') or '').strip() or _new_pm_id('JDC')
        name = str(it.get('name') or '').strip() or '未命名文档'
        folder_id = str(it.get('folderId') or '').strip()
        if folder_id not in valid_folder_ids:
            folder_id = JZG_DOC_DEFAULT_FOLDER_ID
        try:
            size = int(it.get('size') or 0)
        except Exception:
            size = 0
        size = max(0, size)
        tags = it.get('tags')
        if not isinstance(tags, list):
            tags = []
        clean_tags = []
        for tg in tags:
            s = str(tg or '').strip()
            if not s:
                continue
            clean_tags.append(s[:40])
            if len(clean_tags) >= 20:
                break
        status = str(it.get('analysisStatus') or 'pending').strip().lower()
        if status not in {'pending', 'done', 'failed'}:
            status = 'pending'
        normalized_items.append({
            'id': did[:80],
            'name': name[:240],
            'folderId': folder_id,
            'ext': str(it.get('ext') or '').strip()[:20],
            'size': size,
            'uploader': str(it.get('uploader') or 'user').strip()[:60] or 'user',
            'content': str(it.get('content') or '').strip()[:30000],
            'storagePath': str(it.get('storagePath') or '').strip()[:600],
            'summary': str(it.get('summary') or '').strip()[:8000],
            'tags': clean_tags,
            'analysisStatus': status,
            'analysisBy': str(it.get('analysisBy') or '').strip()[:60],
            'analysisAt': str(it.get('analysisAt') or '').strip()[:40],
            'createdAt': str(it.get('createdAt') or now),
            'updatedAt': str(it.get('updatedAt') or now),
        })
    docs['items'] = sorted(normalized_items, key=lambda x: str(x.get('updatedAt') or ''), reverse=True)
    docs['updatedAt'] = str(docs.get('updatedAt') or now)
    strategy['docs'] = docs
    strategy.setdefault('updatedAt', now)
    project['strategy'] = strategy

    board = project.get('board')
    if not isinstance(board, dict):
        board = {}
    if not isinstance(board.get('reminders'), list):
        board['reminders'] = []
    board.setdefault('updatedAt', now)
    project['board'] = board
    project.setdefault('createdAt', now)
    project.setdefault('updatedAt', now)


def _jzg_find_project(data, project_id):
    return next((p for p in (data.get('projects') or []) if str(p.get('id') or '') == str(project_id or '')), None)


def jzg_list_projects():
    data = _load_jzg_data()
    projects = data.get('projects') or []
    for p in projects:
        _ensure_jzg_project(p)
    projects = sorted(projects, key=lambda x: str(x.get('updatedAt') or ''), reverse=True)
    data['projects'] = projects
    _save_jzg_data(data)
    return {'ok': True, 'projects': projects}


def jzg_create_project(name, description=''):
    nm = str(name or '').strip()
    if not nm:
        return {'ok': False, 'error': '项目名称不能为空'}
    data = _load_jzg_data()
    proj = {
        'id': _new_pm_id('JZG'),
        'name': nm[:120],
        'description': str(description or '').strip()[:2000],
        'owner': 'bingbu',
        'followups': {'items': [], 'plan': {'rows': [], 'updatedAt': now_iso()}, 'updatedAt': now_iso()},
        'strategy': {'topics': [], 'docs': {'folders': [], 'items': [], 'updatedAt': now_iso()}, 'updatedAt': now_iso()},
        'board': {'reminders': [], 'updatedAt': now_iso()},
        'createdAt': now_iso(),
        'updatedAt': now_iso(),
    }
    _ensure_jzg_project(proj)
    data.setdefault('projects', []).insert(0, proj)
    _save_jzg_data(data)
    return {'ok': True, 'project': proj}


def jzg_add_followup(project_id, title):
    data = _load_jzg_data()
    project = _jzg_find_project(data, project_id)
    if not project:
        return {'ok': False, 'error': f'项目 {project_id} 不存在'}
    _ensure_jzg_project(project)
    t = str(title or '').strip()
    if not t:
        return {'ok': False, 'error': '跟进项标题不能为空'}
    now = now_iso()
    item = {
        'id': _new_pm_id('JTG'),
        'title': t[:240],
        'status': 'todo',
        'completedAt': '',
        'description': '',
        'memo': '',
        'priority': 'P2',
        'category': '通用',
        'dueDate': '',
        'createdAt': now,
        'updatedAt': now,
    }
    project['followups']['items'].insert(0, item)
    project['followups']['updatedAt'] = now
    project['updatedAt'] = now
    _save_jzg_data(data)
    return {'ok': True, 'project': project, 'item': item}


def jzg_toggle_followup(project_id, item_id, status):
    data = _load_jzg_data()
    project = _jzg_find_project(data, project_id)
    if not project:
        return {'ok': False, 'error': f'项目 {project_id} 不存在'}
    _ensure_jzg_project(project)
    sid = str(item_id or '').strip()
    item = next((x for x in (project['followups'].get('items') or []) if str(x.get('id') or '') == sid), None)
    if not item:
        return {'ok': False, 'error': f'跟进项 {item_id} 不存在'}
    st = str(status or '').strip().lower()
    if st not in {'todo', 'done'}:
        st = 'todo'
    now = now_iso()
    item['status'] = st
    item['completedAt'] = now if st == 'done' else ''
    item['updatedAt'] = now
    project['followups']['updatedAt'] = now
    project['updatedAt'] = now
    _save_jzg_data(data)
    return {'ok': True, 'project': project, 'item': item}


def jzg_update_followup(project_id, item_id, title=None, description=None, memo=None, priority=None, category=None, due_date=None, status=None):
    data = _load_jzg_data()
    project = _jzg_find_project(data, project_id)
    if not project:
        return {'ok': False, 'error': f'项目 {project_id} 不存在'}
    _ensure_jzg_project(project)
    sid = str(item_id or '').strip()
    item = next((x for x in (project['followups'].get('items') or []) if str(x.get('id') or '') == sid), None)
    if not item:
        return {'ok': False, 'error': f'跟进项 {item_id} 不存在'}
    if title is not None:
        t = str(title or '').strip()
        if not t:
            return {'ok': False, 'error': 'title 不能为空'}
        item['title'] = t[:240]
    if description is not None:
        item['description'] = str(description or '').strip()[:8000]
    if memo is not None:
        item['memo'] = str(memo or '').strip()[:8000]
    if priority is not None:
        p = str(priority or '').strip().upper()
        if p not in {'P0', 'P1', 'P2', 'P3'}:
            return {'ok': False, 'error': f'不支持的优先级: {priority}'}
        item['priority'] = p
    if category is not None:
        item['category'] = str(category or '').strip()[:80] or '通用'
    if due_date is not None:
        item['dueDate'] = str(due_date or '').strip()[:10]
    if status is not None:
        st = str(status or '').strip().lower()
        if st not in {'todo', 'done'}:
            return {'ok': False, 'error': f'不支持的状态: {status}'}
        item['status'] = st
        item['completedAt'] = now_iso() if st == 'done' else ''
    now = now_iso()
    item['updatedAt'] = now
    project['followups']['updatedAt'] = now
    project['updatedAt'] = now
    _save_jzg_data(data)
    return {'ok': True, 'project': project, 'item': item}


def jzg_delete_followup(project_id, item_id):
    data = _load_jzg_data()
    project = _jzg_find_project(data, project_id)
    if not project:
        return {'ok': False, 'error': f'项目 {project_id} 不存在'}
    _ensure_jzg_project(project)
    sid = str(item_id or '').strip()
    items = project['followups'].get('items') or []
    idx = next((i for i, x in enumerate(items) if str((x or {}).get('id') or '') == sid), -1)
    if idx < 0:
        return {'ok': False, 'error': f'跟进项 {item_id} 不存在'}
    removed = items.pop(idx)
    now = now_iso()
    project['followups']['updatedAt'] = now
    project['updatedAt'] = now
    _save_jzg_data(data)
    return {'ok': True, 'project': project, 'item': removed}


def jzg_archive_daily_report(project_id, date, report):
    data = _load_jzg_data()
    project = _jzg_find_project(data, project_id)
    if not project:
        return {'ok': False, 'error': f'项目 {project_id} 不存在'}
    _ensure_jzg_project(project)
    dt = str(date or '').strip()[:10]
    txt = str(report or '').strip()
    if not dt:
        return {'ok': False, 'error': 'date required'}
    if not txt:
        return {'ok': False, 'error': 'report required'}
    now = now_iso()
    rec = {
        'id': _new_pm_id('JDR'),
        'date': dt,
        'report': txt[:30000],
        'createdAt': now,
    }
    project['followups']['dailyReports'].insert(0, rec)
    project['followups']['updatedAt'] = now
    project['updatedAt'] = now
    _save_jzg_data(data)
    return {'ok': True, 'project': project, 'record': rec}


def jzg_update_daily_report(project_id, record_id, report, date=None):
    data = _load_jzg_data()
    project = _jzg_find_project(data, project_id)
    if not project:
        return {'ok': False, 'error': f'项目 {project_id} 不存在'}
    _ensure_jzg_project(project)
    rid = str(record_id or '').strip()
    if not rid:
        return {'ok': False, 'error': 'recordId required'}
    txt = str(report or '').strip()
    if not txt:
        return {'ok': False, 'error': 'report required'}
    recs = project['followups'].get('dailyReports') or []
    target = next((r for r in recs if str((r or {}).get('id') or '') == rid), None)
    if not target:
        return {'ok': False, 'error': f'留档记录 {record_id} 不存在'}
    target['report'] = txt[:30000]
    if date is not None:
        target['date'] = str(date or '').strip()[:10]
    target['updatedAt'] = now_iso()
    now = now_iso()
    project['followups']['updatedAt'] = now
    project['updatedAt'] = now
    _save_jzg_data(data)
    return {'ok': True, 'project': project, 'record': target}


def jzg_update_report_template(project_id, mode, template):
    data = _load_jzg_data()
    project = _jzg_find_project(data, project_id)
    if not project:
        return {'ok': False, 'error': f'项目 {project_id} 不存在'}
    _ensure_jzg_project(project)
    md = str(mode or '').strip().lower()
    if md not in {'daily', 'weekly'}:
        return {'ok': False, 'error': f'不支持的模版类型: {mode}'}
    text = str(template or '').strip()
    if not text:
        text = JZG_DEFAULT_DAILY_TEMPLATE if md == 'daily' else JZG_DEFAULT_WEEKLY_TEMPLATE
    now = now_iso()
    project['followups']['reportTemplates'][md] = text[:12000]
    project['followups']['updatedAt'] = now
    project['updatedAt'] = now
    _save_jzg_data(data)
    return {
        'ok': True,
        'project': project,
        'mode': md,
        'template': project['followups']['reportTemplates'][md],
    }


def jzg_generate_report_template(project_id, mode='daily', requirement='', current_template=''):
    data = _load_jzg_data()
    project = _jzg_find_project(data, project_id)
    if not project:
        return {'ok': False, 'error': f'项目 {project_id} 不存在'}
    _ensure_jzg_project(project)
    md = str(mode or '').strip().lower()
    if md not in {'daily', 'weekly'}:
        return {'ok': False, 'error': f'不支持的模版类型: {mode}'}
    req = str(requirement or '').strip()
    if not req:
        return {'ok': False, 'error': 'requirement required'}

    default_tpl = JZG_DEFAULT_DAILY_TEMPLATE if md == 'daily' else JZG_DEFAULT_WEEKLY_TEMPLATE
    existing_tpl = str((((project.get('followups') or {}).get('reportTemplates') or {}).get(md) or '')).strip()
    base_tpl = str(current_template or '').strip() or existing_tpl or default_tpl
    vars_hint = (
        "{project_name}, {date}, {done_items}, {todo_items}"
        if md == 'daily' else
        "{project_name}, {start_date}, {end_date}, {done_items}, {todo_items}"
    )
    prompt = (
        "你是兵部尚书，负责为将作监输出可复用的报告模版。\n"
        "请根据用户要求，生成一份“可填充变量”的中文报告模版。\n"
        "要求：\n"
        "1) 只输出 JSON 对象，不要 Markdown，不要解释。\n"
        "2) JSON 格式固定为：{\"template\":\"...\"}\n"
        "3) 必须保留并合理使用变量占位符，允许调整结构和措辞。\n"
        f"4) 本次可用变量：{vars_hint}\n\n"
        f"模版类型：{'日报' if md == 'daily' else '周报'}\n"
        f"项目名称：{project.get('name') or project.get('id') or '未命名项目'}\n"
        f"用户要求：{req}\n\n"
        "当前模版（参考）：\n"
        f"{base_tpl[:12000]}\n"
    )
    ai = _run_agent_sync('bingbu', prompt, timeout_sec=300)
    if not ai.get('ok'):
        return {'ok': False, 'error': ai.get('error', '兵部生成模版失败')}
    raw = str(ai.get('raw') or '').strip()
    payload = _extract_json_payload(raw)
    template = ''
    if isinstance(payload, dict):
        template = str(payload.get('template') or '').strip()
    if not template:
        template = raw
    if not template:
        return {'ok': False, 'error': '兵部未返回有效模版内容'}
    return {'ok': True, 'mode': md, 'template': template[:12000]}


def _jzg_render_items_for_prompt(items):
    out = []
    for idx, it in enumerate(items, 1):
        title = str((it or {}).get('title') or (it or {}).get('id') or '').strip()
        done_at = str((it or {}).get('completedAt') or '').replace('T', ' ').strip()
        if done_at:
            out.append(f"{idx}. {title}（完成时间：{done_at[:16]}）")
        else:
            out.append(f"{idx}. {title}")
    return '\n'.join(out) if out else '1. 无'


def jzg_generate_followup_report(project_id, mode='daily', date='', start_date='', end_date='', template=''):
    data = _load_jzg_data()
    project = _jzg_find_project(data, project_id)
    if not project:
        return {'ok': False, 'error': f'项目 {project_id} 不存在'}
    _ensure_jzg_project(project)
    md = str(mode or '').strip().lower()
    if md not in {'daily', 'weekly'}:
        return {'ok': False, 'error': f'不支持的报告类型: {mode}'}
    if md == 'daily':
        dt = str(date or '').strip()
        if not dt:
            return {'ok': False, 'error': 'date required'}
        start = dt
        end = dt
    else:
        start = str(start_date or '').strip()
        end = str(end_date or '').strip()
        if not start or not end:
            return {'ok': False, 'error': 'startDate and endDate required'}
        if start > end:
            return {'ok': False, 'error': 'startDate must be <= endDate'}

    followups = ((project.get('followups') or {}).get('items') or [])

    def _done_at(it):
        v = str((it or {}).get('completedAt') or '').strip()
        if v:
            return v
        if str((it or {}).get('status') or '').strip().lower() == 'done':
            return str((it or {}).get('updatedAt') or '').strip()
        return ''

    done_items = []
    todo_items = []
    for it in followups:
        if not isinstance(it, dict):
            continue
        st = str(it.get('status') or '').strip().lower()
        if st == 'done':
            d = _done_at(it)[:10]
            if d and start <= d <= end:
                done_items.append(it)
        else:
            todo_items.append(it)

    tpl = str(template or '').strip()
    if not tpl:
        tpl = str((((project.get('followups') or {}).get('reportTemplates') or {}).get(md) or '')).strip()
    if not tpl:
        tpl = JZG_DEFAULT_DAILY_TEMPLATE if md == 'daily' else JZG_DEFAULT_WEEKLY_TEMPLATE

    project_name = str(project.get('name') or project.get('id') or '未命名项目')
    done_text = _jzg_render_items_for_prompt(done_items)
    todo_text = _jzg_render_items_for_prompt(todo_items)
    range_label = start if md == 'daily' else f'{start} ~ {end}'

    prompt = (
        "你是兵部尚书，擅长把任务清单整理成正式日报/周报。\n"
        "请严格参考“模版格式”，并基于已完成/未完成任务生成一版表达清晰、可直接发送的中文报告。\n"
        "要求：\n"
        "1) 必须遵循模版结构，不要丢段落标题。\n"
        "2) 可润色措辞，但不要编造不存在的任务。\n"
        "3) 只输出 JSON 对象，不要 Markdown，不要解释。\n"
        "4) JSON 格式固定为：{\"report\":\"...\"}\n\n"
        f"报告类型：{'日报' if md == 'daily' else '周报'}\n"
        f"项目名称：{project_name}\n"
        f"统计区间：{range_label}\n\n"
        "模版：\n"
        f"{tpl[:12000]}\n\n"
        "本次已完成任务：\n"
        f"{done_text}\n\n"
        "当前未完成任务：\n"
        f"{todo_text}\n"
    )
    ai = _run_agent_sync('bingbu', prompt, timeout_sec=300)
    if not ai.get('ok'):
        return {'ok': False, 'error': ai.get('error', '兵部生成失败')}
    raw = str(ai.get('raw') or '').strip()
    payload = _extract_json_payload(raw)
    report = ''
    if isinstance(payload, dict):
        report = str(payload.get('report') or '').strip()
    if not report:
        report = raw
    if not report:
        return {'ok': False, 'error': '兵部未返回有效日报内容'}
    return {
        'ok': True,
        'mode': md,
        'projectId': project_id,
        'startDate': start,
        'endDate': end,
        'report': report[:30000],
    }


def jzg_add_daily_note(project_id, text):
    data = _load_jzg_data()
    project = _jzg_find_project(data, project_id)
    if not project:
        return {'ok': False, 'error': f'项目 {project_id} 不存在'}
    _ensure_jzg_project(project)
    msg = str(text or '').strip()
    if not msg:
        return {'ok': False, 'error': '记录内容不能为空'}
    now = now_iso()
    note = {'id': _new_pm_id('JDN'), 'text': msg[:4000], 'at': now}
    project['followups']['daily'].insert(0, note)
    project['followups']['updatedAt'] = now
    project['updatedAt'] = now
    _save_jzg_data(data)
    return {'ok': True, 'project': project, 'note': note}


def _normalize_jzg_plan_rows(rows):
    out = []
    if not isinstance(rows, list):
        return out
    for r in rows[:80]:
        if not isinstance(r, dict):
            continue
        name = str(r.get('name') or '').strip()
        if not name:
            continue
        start = str(r.get('start') or '').strip()[:10]
        end = str(r.get('end') or '').strip()[:10]
        owner = str(r.get('owner') or '').strip()[:60]
        try:
            progress = int(r.get('progress') or 0)
        except Exception:
            progress = 0
        progress = max(0, min(100, progress))
        out.append({'name': name[:200], 'start': start, 'end': end, 'owner': owner, 'progress': progress})
    return out


def jzg_update_plan(project_id, rows):
    data = _load_jzg_data()
    project = _jzg_find_project(data, project_id)
    if not project:
        return {'ok': False, 'error': f'项目 {project_id} 不存在'}
    _ensure_jzg_project(project)
    normalized = _normalize_jzg_plan_rows(rows)
    if not normalized:
        return {'ok': False, 'error': '计划表至少保留一行有效数据'}
    now = now_iso()
    project['followups']['plan'] = {
        'rows': normalized,
        'updatedAt': now,
    }
    project['followups']['updatedAt'] = now
    project['updatedAt'] = now
    _save_jzg_data(data)
    return {'ok': True, 'project': project, 'plan': project['followups']['plan']}


def jzg_create_strategy_topic(project_id, title, context=''):
    data = _load_jzg_data()
    project = _jzg_find_project(data, project_id)
    if not project:
        return {'ok': False, 'error': f'项目 {project_id} 不存在'}
    _ensure_jzg_project(project)
    t = str(title or '').strip()
    if not t:
        return {'ok': False, 'error': '主题标题不能为空'}
    now = now_iso()
    topic = {
        'id': _new_pm_id('JTP'),
        'title': t[:240],
        'context': str(context or '').strip()[:10000],
        'qa': [],
        'createdAt': now,
        'updatedAt': now,
    }
    project['strategy']['topics'].insert(0, topic)
    project['strategy']['updatedAt'] = now
    project['updatedAt'] = now
    _save_jzg_data(data)
    return {'ok': True, 'project': project, 'topic': topic}


def jzg_add_strategy_message(project_id, topic_id, message, role='user'):
    data = _load_jzg_data()
    project = _jzg_find_project(data, project_id)
    if not project:
        return {'ok': False, 'error': f'项目 {project_id} 不存在'}
    _ensure_jzg_project(project)
    tid = str(topic_id or '').strip()
    topic = next((x for x in (project['strategy'].get('topics') or []) if str(x.get('id') or '') == tid), None)
    if not topic:
        return {'ok': False, 'error': f'主题 {topic_id} 不存在'}
    msg = str(message or '').strip()
    if not msg:
        return {'ok': False, 'error': '消息不能为空'}
    r = str(role or 'user').strip().lower()
    if r not in {'user', 'codex', 'bingbu', 'libu_hr'}:
        r = 'user'
    now = now_iso()
    topic.setdefault('qa', []).append({'role': r, 'text': msg[:8000], 'at': now})
    topic['updatedAt'] = now
    project['strategy']['updatedAt'] = now
    project['updatedAt'] = now
    _save_jzg_data(data)
    return {'ok': True, 'project': project, 'topic': topic}


def _jzg_docs_data(project):
    _ensure_jzg_project(project)
    return ((project.get('strategy') or {}).get('docs') or {})


def jzg_doc_folder_create(project_id, name):
    data = _load_jzg_data()
    project = _jzg_find_project(data, project_id)
    if not project:
        return {'ok': False, 'error': f'项目 {project_id} 不存在'}
    docs = _jzg_docs_data(project)
    nm = str(name or '').strip()
    if not nm:
        return {'ok': False, 'error': '目录名称不能为空'}
    folders = docs.get('folders') or []
    if any(str((f or {}).get('name') or '').strip() == nm for f in folders):
        return {'ok': False, 'error': '目录名称已存在'}
    now = now_iso()
    folder = {
        'id': _new_pm_id('JFD'),
        'name': nm[:80],
        'order': len(folders),
        'createdAt': now,
        'updatedAt': now,
    }
    folders.append(folder)
    docs['folders'] = folders
    docs['updatedAt'] = now
    project['strategy']['updatedAt'] = now
    project['updatedAt'] = now
    _save_jzg_data(data)
    return {'ok': True, 'project': project, 'folder': folder}


def jzg_doc_folder_update(project_id, folder_id, name):
    data = _load_jzg_data()
    project = _jzg_find_project(data, project_id)
    if not project:
        return {'ok': False, 'error': f'项目 {project_id} 不存在'}
    docs = _jzg_docs_data(project)
    fid = str(folder_id or '').strip()
    nm = str(name or '').strip()
    if not fid or not nm:
        return {'ok': False, 'error': 'folderId and name required'}
    if fid == JZG_DOC_DEFAULT_FOLDER_ID:
        return {'ok': False, 'error': '系统默认目录不支持改名'}
    folders = docs.get('folders') or []
    target = next((f for f in folders if str((f or {}).get('id') or '') == fid), None)
    if not target:
        return {'ok': False, 'error': f'目录 {folder_id} 不存在'}
    if any(str((f or {}).get('name') or '').strip() == nm and str((f or {}).get('id') or '') != fid for f in folders):
        return {'ok': False, 'error': '目录名称已存在'}
    now = now_iso()
    target['name'] = nm[:80]
    target['updatedAt'] = now
    docs['updatedAt'] = now
    project['strategy']['updatedAt'] = now
    project['updatedAt'] = now
    _save_jzg_data(data)
    return {'ok': True, 'project': project, 'folder': target}


def jzg_doc_folder_delete(project_id, folder_id):
    data = _load_jzg_data()
    project = _jzg_find_project(data, project_id)
    if not project:
        return {'ok': False, 'error': f'项目 {project_id} 不存在'}
    docs = _jzg_docs_data(project)
    fid = str(folder_id or '').strip()
    if not fid:
        return {'ok': False, 'error': 'folderId required'}
    if fid == JZG_DOC_DEFAULT_FOLDER_ID:
        return {'ok': False, 'error': '系统默认目录不支持删除'}
    folders = docs.get('folders') or []
    idx = next((i for i, f in enumerate(folders) if str((f or {}).get('id') or '') == fid), -1)
    if idx < 0:
        return {'ok': False, 'error': f'目录 {folder_id} 不存在'}
    removed = folders.pop(idx)
    for fd in folders:
        fd['order'] = folders.index(fd)
    for item in (docs.get('items') or []):
        if str((item or {}).get('folderId') or '') == fid:
            item['folderId'] = JZG_DOC_DEFAULT_FOLDER_ID
            item['updatedAt'] = now_iso()
    now = now_iso()
    docs['updatedAt'] = now
    project['strategy']['updatedAt'] = now
    project['updatedAt'] = now
    _save_jzg_data(data)
    return {'ok': True, 'project': project, 'folder': removed}


def jzg_doc_folder_reorder(project_id, source_folder_id, target_folder_id, place='before'):
    data = _load_jzg_data()
    project = _jzg_find_project(data, project_id)
    if not project:
        return {'ok': False, 'error': f'项目 {project_id} 不存在'}
    docs = _jzg_docs_data(project)
    s = str(source_folder_id or '').strip()
    t = str(target_folder_id or '').strip()
    if not s or not t:
        return {'ok': False, 'error': 'sourceFolderId and targetFolderId required'}
    folders = docs.get('folders') or []
    if len(folders) < 2:
        return {'ok': True, 'project': project, 'folders': folders}
    src_idx = next((i for i, f in enumerate(folders) if str((f or {}).get('id') or '') == s), -1)
    tgt_idx = next((i for i, f in enumerate(folders) if str((f or {}).get('id') or '') == t), -1)
    if src_idx < 0 or tgt_idx < 0:
        return {'ok': False, 'error': '目录不存在'}
    if src_idx == tgt_idx:
        return {'ok': True, 'project': project, 'folders': folders}
    moving = folders.pop(src_idx)
    if src_idx < tgt_idx:
        tgt_idx -= 1
    plc = str(place or 'before').strip().lower()
    insert_idx = tgt_idx + 1 if plc == 'after' else tgt_idx
    insert_idx = max(0, min(insert_idx, len(folders)))
    folders.insert(insert_idx, moving)
    now = now_iso()
    for idx, fd in enumerate(folders):
        if not isinstance(fd, dict):
            continue
        fd['order'] = idx
        fd['updatedAt'] = now
    docs['folders'] = folders
    docs['updatedAt'] = now
    project['strategy']['updatedAt'] = now
    project['updatedAt'] = now
    _save_jzg_data(data)
    return {'ok': True, 'project': project, 'folders': folders}


def jzg_doc_create(project_id, name, folder_id=None, content='', size=0, ext='', file_base64=''):
    data = _load_jzg_data()
    project = _jzg_find_project(data, project_id)
    if not project:
        return {'ok': False, 'error': f'项目 {project_id} 不存在'}
    docs = _jzg_docs_data(project)
    nm = str(name or '').strip()
    if not nm:
        return {'ok': False, 'error': '文档名称不能为空'}
    valid_folder_ids = {str((f or {}).get('id') or '') for f in (docs.get('folders') or [])}
    fid = str(folder_id or '').strip()
    if fid not in valid_folder_ids:
        fid = JZG_DOC_DEFAULT_FOLDER_ID
    try:
        s = int(size or 0)
    except Exception:
        s = 0
    now = now_iso()
    did = _new_pm_id('JDC')
    storage_path = ''
    fb64 = str(file_base64 or '').strip()
    if fb64:
        try:
            storage_path = _jzg_write_external_doc_file(project_id, did, nm, fb64)
        except Exception as e:
            return {'ok': False, 'error': f'外部文档落盘失败: {e}'}
    item = {
        'id': did,
        'name': nm[:240],
        'folderId': fid,
        'ext': str(ext or '').strip()[:20],
        'size': max(0, s),
        'uploader': 'user',
        'content': str(content or '').strip()[:30000],
        'storagePath': storage_path,
        'summary': '',
        'tags': [],
        'analysisStatus': 'pending',
        'analysisBy': '',
        'analysisAt': '',
        'createdAt': now,
        'updatedAt': now,
    }
    docs.setdefault('items', []).insert(0, item)
    docs['updatedAt'] = now
    project['strategy']['updatedAt'] = now
    project['updatedAt'] = now
    _save_jzg_data(data)
    return {'ok': True, 'project': project, 'item': item}


def jzg_doc_update(project_id, doc_id, name=None, folder_id=None, content=None, summary=None, tags=None, size=None, ext=None):
    data = _load_jzg_data()
    project = _jzg_find_project(data, project_id)
    if not project:
        return {'ok': False, 'error': f'项目 {project_id} 不存在'}
    docs = _jzg_docs_data(project)
    did = str(doc_id or '').strip()
    items = docs.get('items') or []
    item = next((x for x in items if str((x or {}).get('id') or '') == did), None)
    if not item:
        return {'ok': False, 'error': f'文档 {doc_id} 不存在'}
    if name is not None:
        nm = str(name or '').strip()
        if not nm:
            return {'ok': False, 'error': '文档名称不能为空'}
        item['name'] = nm[:240]
    if folder_id is not None:
        valid_folder_ids = {str((f or {}).get('id') or '') for f in (docs.get('folders') or [])}
        fid = str(folder_id or '').strip()
        item['folderId'] = fid if fid in valid_folder_ids else JZG_DOC_DEFAULT_FOLDER_ID
    if content is not None:
        item['content'] = str(content or '').strip()[:30000]
    if summary is not None:
        item['summary'] = str(summary or '').strip()[:8000]
    if tags is not None:
        clean_tags = []
        if isinstance(tags, list):
            for tg in tags:
                s = str(tg or '').strip()
                if not s:
                    continue
                clean_tags.append(s[:40])
                if len(clean_tags) >= 20:
                    break
        item['tags'] = clean_tags
    if size is not None:
        try:
            item['size'] = max(0, int(size or 0))
        except Exception:
            pass
    if ext is not None:
        item['ext'] = str(ext or '').strip()[:20]
    now = now_iso()
    item['updatedAt'] = now
    docs['updatedAt'] = now
    project['strategy']['updatedAt'] = now
    project['updatedAt'] = now
    _save_jzg_data(data)
    return {'ok': True, 'project': project, 'item': item}


def jzg_doc_delete(project_id, doc_id):
    data = _load_jzg_data()
    project = _jzg_find_project(data, project_id)
    if not project:
        return {'ok': False, 'error': f'项目 {project_id} 不存在'}
    docs = _jzg_docs_data(project)
    did = str(doc_id or '').strip()
    items = docs.get('items') or []
    idx = next((i for i, x in enumerate(items) if str((x or {}).get('id') or '') == did), -1)
    if idx < 0:
        return {'ok': False, 'error': f'文档 {doc_id} 不存在'}
    removed = items.pop(idx)
    storage_path = str((removed or {}).get('storagePath') or '').strip()
    if storage_path:
        try:
            p = pathlib.Path(storage_path).expanduser().resolve()
            if _path_within(p, JZG_EXTERNAL_DOCS_DIR) and p.exists() and p.is_file():
                p.unlink(missing_ok=True)
        except Exception:
            pass
    now = now_iso()
    docs['updatedAt'] = now
    project['strategy']['updatedAt'] = now
    project['updatedAt'] = now
    _save_jzg_data(data)
    return {'ok': True, 'project': project, 'item': removed}


def jzg_doc_analyze(project_id, doc_id):
    data = _load_jzg_data()
    project = _jzg_find_project(data, project_id)
    if not project:
        return {'ok': False, 'error': f'项目 {project_id} 不存在'}
    docs = _jzg_docs_data(project)
    did = str(doc_id or '').strip()
    items = docs.get('items') or []
    item = next((x for x in items if str((x or {}).get('id') or '') == did), None)
    if not item:
        return {'ok': False, 'error': f'文档 {doc_id} 不存在'}
    name = str(item.get('name') or '未命名文档')
    content = str(item.get('content') or '').strip()
    summary = ''
    tags = []
    if content:
        prompt = (
            "你是 PM 小组专家分析助手，请对文档内容做结构化摘要。\n"
            "要求：\n"
            "1) 仅输出 JSON。\n"
            "2) JSON 格式固定：{\"summary\":\"...\",\"tags\":[\"...\"]}\n"
            "3) summary 控制在 200 字内；tags 3-8 个。\n\n"
            f"文档名：{name}\n"
            f"文档内容：\n{content[:12000]}\n"
        )
        ai = _run_agent_sync('bingbu', prompt, timeout_sec=240)
        if ai.get('ok'):
            payload = _extract_json_payload(str(ai.get('raw') or ''))
            if isinstance(payload, dict):
                summary = str(payload.get('summary') or '').strip()
                tgs = payload.get('tags')
                if isinstance(tgs, list):
                    for tg in tgs:
                        s = str(tg or '').strip()
                        if not s:
                            continue
                        tags.append(s[:40])
                        if len(tags) >= 8:
                            break
    if not summary:
        base = content or str(item.get('summary') or '')
        summary = (base[:180] + ('...' if len(base) > 180 else '')) if base else f'已完成对《{name}》的结构化分析。'
    if not tags:
        guess = []
        for key in ('需求', '方案', '接口', '流程', '风险', '上线', '测试', '文档'):
            if key in content:
                guess.append(key)
        tags = guess[:8] if guess else ['待补充']
    now = now_iso()
    item['summary'] = summary[:8000]
    item['tags'] = tags
    item['analysisStatus'] = 'done'
    item['analysisBy'] = 'bingbu'
    item['analysisAt'] = now
    item['updatedAt'] = now
    docs['updatedAt'] = now
    project['strategy']['updatedAt'] = now
    project['updatedAt'] = now
    _save_jzg_data(data)
    return {'ok': True, 'project': project, 'item': item}


def jzg_add_reminder(project_id, title, schedule=''):
    data = _load_jzg_data()
    project = _jzg_find_project(data, project_id)
    if not project:
        return {'ok': False, 'error': f'项目 {project_id} 不存在'}
    _ensure_jzg_project(project)
    t = str(title or '').strip()
    if not t:
        return {'ok': False, 'error': '提醒标题不能为空'}
    now = now_iso()
    reminder = {
        'id': _new_pm_id('JRM'),
        'title': t[:240],
        'schedule': str(schedule or '').strip()[:200],
        'enabled': True,
        'createdAt': now,
        'updatedAt': now,
    }
    project['board']['reminders'].insert(0, reminder)
    project['board']['updatedAt'] = now
    project['updatedAt'] = now
    _save_jzg_data(data)
    return {'ok': True, 'project': project, 'reminder': reminder}


def jzg_toggle_reminder(project_id, reminder_id, enabled):
    data = _load_jzg_data()
    project = _jzg_find_project(data, project_id)
    if not project:
        return {'ok': False, 'error': f'项目 {project_id} 不存在'}
    _ensure_jzg_project(project)
    rid = str(reminder_id or '').strip()
    reminder = next((x for x in (project['board'].get('reminders') or []) if str(x.get('id') or '') == rid), None)
    if not reminder:
        return {'ok': False, 'error': f'提醒 {reminder_id} 不存在'}
    now = now_iso()
    reminder['enabled'] = bool(enabled)
    reminder['updatedAt'] = now
    project['board']['updatedAt'] = now
    project['updatedAt'] = now
    _save_jzg_data(data)
    return {'ok': True, 'project': project, 'reminder': reminder}


# 旨意标题最低要求
_MIN_TITLE_LEN = 6
_JUNK_TITLES = {
    '?', '？', '好', '好的', '是', '否', '不', '不是', '对', '了解', '收到',
    '嗯', '哦', '知道了', '开启了么', '可以', '不行', '行', 'ok', 'yes', 'no',
    '你去开启', '测试', '试试', '看看',
}
_TASK_ID_STATE_FILE = DATA / 'task_id_state.json'


def _next_jjc_task_id():
    """生成全局唯一、单调递增任务ID（不受清理 tasks_source 影响）。
    格式: JJC-YYYYMMDD-HHMMSSmmm
    """
    now_ms = int(time.time() * 1000)
    holder = {'ms': now_ms}

    def modifier(data):
        if not isinstance(data, dict):
            data = {}
        last_ms = int(data.get('last_ms') or 0)
        new_ms = max(now_ms, last_ms + 1)
        data['last_ms'] = new_ms
        holder['ms'] = new_ms
        return data

    atomic_json_update(_TASK_ID_STATE_FILE, modifier, {})
    ms = holder['ms']
    dt = datetime.datetime.fromtimestamp(ms / 1000.0)
    return f'JJC-{dt:%Y%m%d}-{dt:%H%M%S}{ms % 1000:03d}'


def handle_create_task(title, org='中书省', official='中书令', priority='normal', template_id='', params=None, target_dept=''):
    """从看板创建新任务（圣旨模板下旨）。"""
    if not title or not title.strip():
        return {'ok': False, 'error': '任务标题不能为空'}
    title = title.strip()
    # 剥离 Conversation info 元数据
    title = re.split(r'\n*Conversation info\s*\(', title, maxsplit=1)[0].strip()
    title = re.split(r'\n*```', title, maxsplit=1)[0].strip()
    # 清理常见前缀: "传旨:" "下旨:" 等
    title = re.sub(r'^(传旨|下旨)[：:\uff1a]\s*', '', title)
    if len(title) > 100:
        title = title[:100] + '…'
    # 标题质量校验：防止闲聊被误建为旨意
    if len(title) < _MIN_TITLE_LEN:
        return {'ok': False, 'error': f'标题过短（{len(title)}<{_MIN_TITLE_LEN}字），不像是旨意'}
    if title.lower() in _JUNK_TITLES:
        return {'ok': False, 'error': f'「{title}」不是有效旨意，请输入具体工作指令'}
    # 生成 task id: JJC-YYYYMMDD-HHMMSSmmm（持久单调递增）
    task_id = _next_jjc_task_id()
    # 正确流程起点：皇上 -> 太子分拣
    # target_dept 记录模板建议的最终执行部门（仅供尚书省派发参考）
    initial_org = '太子'
    new_task = {
        'id': task_id,
        'title': title,
        'official': official,
        'org': initial_org,
        'state': 'Taizi',
        'now': '等待太子接旨分拣',
        'eta': '-',
        'block': '无',
        'output': '',
        'ac': '',
        'priority': priority,
        'templateId': template_id,
        'templateParams': params or {},
        'flow_log': [{
            'at': now_iso(),
            'from': '皇上',
            'to': initial_org,
            'remark': f'下旨：{title}'
        }],
        'updatedAt': now_iso(),
    }
    if target_dept:
        new_task['targetDept'] = target_dept

    _ensure_scheduler(new_task)
    _scheduler_snapshot(new_task, 'create-task-initial')
    _scheduler_mark_progress(new_task, '任务创建')

    tasks.insert(0, new_task)
    save_tasks(tasks)
    log.info(f'创建任务: {task_id} | {title[:40]}')

    dispatch_for_state(task_id, new_task, 'Taizi', trigger='imperial-edict')

    return {'ok': True, 'taskId': task_id, 'message': f'旨意 {task_id} 已下达，正在派发给太子'}


def handle_review_action(task_id, action, comment=''):
    """门下省御批：准奏/封驳。"""
    tasks = load_tasks()
    task = next((t for t in tasks if t.get('id') == task_id), None)
    if not task:
        return {'ok': False, 'error': f'任务 {task_id} 不存在'}
    if task.get('state') not in ('Review', 'Menxia'):
        return {'ok': False, 'error': f'任务 {task_id} 当前状态为 {task.get("state")}，无法御批'}

    _ensure_scheduler(task)
    _scheduler_snapshot(task, f'review-before-{action}')

    if action == 'approve':
        if task['state'] == 'Menxia':
            task['state'] = 'Assigned'
            task['now'] = '门下省准奏，移交尚书省派发'
            remark = f'✅ 准奏：{comment or "门下省审议通过"}'
            to_dept = '尚书省'
        else:  # Review
            task['state'] = 'Done'
            task['now'] = '御批通过，任务完成'
            remark = f'✅ 御批准奏：{comment or "审查通过"}'
            to_dept = '皇上'
    elif action == 'reject':
        round_num = (task.get('review_round') or 0) + 1
        task['review_round'] = round_num
        task['state'] = 'Zhongshu'
        task['now'] = f'封驳退回中书省修订（第{round_num}轮）'
        remark = f'🚫 封驳：{comment or "需要修改"}'
        to_dept = '中书省'
    else:
        return {'ok': False, 'error': f'未知操作: {action}'}

    task.setdefault('flow_log', []).append({
        'at': now_iso(),
        'from': '门下省' if task.get('state') != 'Done' else '皇上',
        'to': to_dept,
        'remark': remark
    })
    _scheduler_mark_progress(task, f'审议动作 {action} -> {task.get("state")}')
    task['updatedAt'] = now_iso()
    save_tasks(tasks)

    # 🚀 审批后自动派发对应 Agent
    new_state = task['state']
    if new_state not in ('Done',):
        dispatch_for_state(task_id, task, new_state)

    label = '已准奏' if action == 'approve' else '已封驳'
    dispatched = ' (已自动派发 Agent)' if new_state != 'Done' else ''
    return {'ok': True, 'message': f'{task_id} {label}{dispatched}'}


# ══ Agent 在线状态检测 ══

_AGENT_DEPTS = [
    {'id':'taizi',   'label':'太子',  'emoji':'🤴', 'role':'太子',     'rank':'储君'},
    {'id':'zhongshu','label':'中书省','emoji':'📜', 'role':'中书令',   'rank':'正一品'},
    {'id':'menxia',  'label':'门下省','emoji':'🔍', 'role':'侍中',     'rank':'正一品'},
    {'id':'shangshu','label':'尚书省','emoji':'📮', 'role':'尚书令',   'rank':'正一品'},
    {'id':'hubu',    'label':'户部',  'emoji':'💰', 'role':'户部尚书', 'rank':'正二品'},
    {'id':'libu',    'label':'藏经阁',  'emoji':'📝', 'role':'扫地僧', 'rank':'正二品'},
    {'id':'bingbu',  'label':'PM小组',  'emoji':'⚔️', 'role':'项目经理', 'rank':'正二品'},
    {'id':'xingbu',  'label':'刑部',  'emoji':'⚖️', 'role':'刑部尚书', 'rank':'正二品'},
    {'id':'rnd',  'label':'研发部',  'emoji':'💻', 'role':'研发总监', 'rank':'正二品'},
    {'id':'libu_hr', 'label':'人事部',  'emoji':'👔', 'role':'人事经理', 'rank':'正二品'},
    {'id':'zaochao', 'label':'钦天监','emoji':'📰', 'role':'朝报官',   'rank':'正三品'},
]
_BASE_AGENT_IDS = {x['id'] for x in _AGENT_DEPTS}


def _check_gateway_alive():
    """检测 Gateway 是否在运行。

    Windows 上不要依赖 pgrep；优先通过本地端口探测判断。
    """
    if _check_gateway_probe():
        return True
    try:
        if os.name == 'nt':
            with socket.create_connection(('127.0.0.1', 18789), timeout=2):
                return True
            return False
        result = subprocess.run(['pgrep', '-f', 'openclaw-gateway'],
                                capture_output=True, text=True, timeout=5)
        return result.returncode == 0
    except Exception:
        return False


def _check_gateway_probe():
    """通过 HTTP probe 检测 Gateway 是否响应。"""
    for url in ('http://127.0.0.1:18789/', 'http://127.0.0.1:18789/healthz'):
        try:
            from urllib.request import urlopen
            resp = urlopen(url, timeout=3)
            if 200 <= resp.status < 500:
                return True
        except Exception:
            continue
    return False


def _get_agent_session_status(agent_id):
    """读取 Agent 的 sessions.json 获取活跃状态。
    返回: (last_active_ts_ms, session_count, is_busy)
    """
    sessions_file = OCLAW_HOME / 'agents' / agent_id / 'sessions' / 'sessions.json'
    if not sessions_file.exists():
        return 0, 0, False
    try:
        data = json.loads(sessions_file.read_text())
        if not isinstance(data, dict):
            return 0, 0, False
        session_count = len(data)
        last_ts = 0
        for v in data.values():
            ts = v.get('updatedAt', 0)
            if isinstance(ts, (int, float)) and ts > last_ts:
                last_ts = ts
        now_ms = int(datetime.datetime.now().timestamp() * 1000)
        age_ms = now_ms - last_ts if last_ts else 9999999999
        is_busy = age_ms <= 2 * 60 * 1000  # 2分钟内视为正在工作
        return last_ts, session_count, is_busy
    except Exception:
        return 0, 0, False


def _check_agent_process(agent_id):
    """检测是否有该 Agent 的 openclaw-agent 进程正在运行。"""
    try:
        result = subprocess.run(
            ['pgrep', '-f', f'openclaw.*--agent.*{agent_id}'],
            capture_output=True, text=True, timeout=5
        )
        return result.returncode == 0
    except Exception:
        return False


def _check_agent_workspace(agent_id):
    """检查 Agent 工作空间是否存在。"""
    aid = _normalize_agent_id(agent_id)
    ws = OCLAW_HOME / f'workspace-{aid}'
    return ws.is_dir()


def _list_agent_dirs():
    root = OCLAW_HOME / 'agents'
    if not root.exists():
        return []
    return sorted([p.name for p in root.iterdir() if p.is_dir()])


def _base_agent_id(agent_id):
    aid = _normalize_agent_id(agent_id)
    if '__' in aid:
        aid = aid.split('__', 1)[0]
    return aid


def _collect_related_agent_ids(agent_id):
    """返回某个基础 agent 相关的所有 runtime agent（含自身）。

    规则：
    - 若传入是基础 agent（在 _AGENT_DEPTS 中），返回 [base + base__*]
    - 若传入是具体 runtime agent，返回 [runtime]
    """
    aid = _normalize_agent_id(agent_id)
    if not aid:
        return []
    dirs = _list_agent_dirs()
    if aid in _BASE_AGENT_IDS:
        prefix = f'{aid}__'
        ids = [x for x in dirs if x == aid or x.startswith(prefix)]
        # 保证基础 agent 在首位
        ids.sort(key=lambda x: (0 if x == aid else 1, x))
        return ids
    return [aid]


def get_agents_status():
    """获取所有 Agent 的在线状态。
    返回各 Agent 的:
    - status: 'running' | 'idle' | 'offline' | 'unconfigured'
    - lastActive: 最后活跃时间
    - sessions: 会话数
    - hasWorkspace: 工作空间是否存在
    - processAlive: 是否有进程在运行
    """
    gateway_alive = _check_gateway_alive()
    gateway_probe = _check_gateway_probe() if gateway_alive else False

    agents = []
    for dept in _AGENT_DEPTS:
        aid = dept['id']
        related_ids = _collect_related_agent_ids(aid) or [aid]

        has_workspace = False
        last_ts = 0
        sess_count = 0
        is_busy = False
        process_alive = False
        active_runtime_agents = []

        for rid in related_ids:
            has_workspace = has_workspace or _check_agent_workspace(rid)
            r_last_ts, r_sess_count, r_is_busy = _get_agent_session_status(rid)
            if r_last_ts > last_ts:
                last_ts = r_last_ts
            sess_count += int(r_sess_count or 0)
            is_busy = is_busy or bool(r_is_busy)
            alive = _check_agent_process(rid)
            process_alive = process_alive or alive
            if alive or r_sess_count:
                active_runtime_agents.append(rid)

        # 状态判定
        if not has_workspace:
            status = 'unconfigured'
            status_label = '❌ 未配置'
        elif not gateway_alive:
            status = 'offline'
            status_label = '🔴 Gateway 离线'
        elif process_alive or is_busy:
            status = 'running'
            status_label = '🟢 运行中'
        elif last_ts > 0:
            now_ms = int(datetime.datetime.now().timestamp() * 1000)
            age_ms = now_ms - last_ts
            if age_ms <= 10 * 60 * 1000:  # 10分钟内
                status = 'idle'
                status_label = '🟡 待命'
            elif age_ms <= 3600 * 1000:  # 1小时内
                status = 'idle'
                status_label = '⚪ 空闲'
            else:
                status = 'idle'
                status_label = '⚪ 休眠'
        else:
            status = 'idle'
            status_label = '⚪ 无记录'

        # 格式化最后活跃时间
        last_active_str = None
        if last_ts > 0:
            try:
                last_active_str = datetime.datetime.fromtimestamp(
                    last_ts / 1000
                ).strftime('%m-%d %H:%M')
            except Exception:
                pass

        agents.append({
            'id': aid,
            'label': dept['label'],
            'emoji': dept['emoji'],
            'role': dept['role'],
            'status': status,
            'statusLabel': status_label,
            'lastActive': last_active_str,
            'lastActiveTs': last_ts,
            'sessions': sess_count,
            'hasWorkspace': has_workspace,
            'processAlive': process_alive,
            'runtimeAgents': related_ids,
            'activeRuntimeAgents': active_runtime_agents[:20],
        })

    return {
        'ok': True,
        'gateway': {
            'alive': gateway_alive,
            'probe': gateway_probe,
            'status': '🟢 运行中' if gateway_probe else ('🟡 进程在但无响应' if gateway_alive else '🔴 未启动'),
        },
        'agents': agents,
        'checkedAt': now_iso(),
    }


def wake_agent(agent_id, message=''):
    """唤醒指定 Agent，发送一条心跳/唤醒消息。"""
    requested_agent_id = str(agent_id or '').strip()
    if not _SAFE_NAME_RE.match(requested_agent_id):
        return {'ok': False, 'error': f'agent_id 非法: {requested_agent_id}'}
    runtime_id = _normalize_agent_id(requested_agent_id)
    if not _check_agent_workspace(runtime_id):
        return {'ok': False, 'error': f'{requested_agent_id} 工作空间不存在，请先配置'}
    if not _check_gateway_alive():
        return {'ok': False, 'error': 'Gateway 未启动，请先运行 openclaw gateway start'}

    msg = message or f'🔔 系统心跳检测 — 请回复 OK 确认在线。当前时间: {now_iso()}'

    def do_wake():
        try:
            cmd = ['openclaw', 'agent', '--agent', runtime_id, '-m', msg, '--timeout', '120']
            log.info(f'🔔 唤醒 {requested_agent_id}...')
            # 带重试（最多2次）
            for attempt in range(1, 3):
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=130)
                if result.returncode == 0:
                    log.info(f'✅ {requested_agent_id} 已唤醒')
                    return
                err_msg = result.stderr[:200] if result.stderr else result.stdout[:200]
                log.warning(f'⚠️ {requested_agent_id} 唤醒失败(第{attempt}次): {err_msg}')
                if attempt < 2:
                    import time
                    time.sleep(5)
            log.error(f'❌ {requested_agent_id} 唤醒最终失败')
        except subprocess.TimeoutExpired:
            log.error(f'❌ {requested_agent_id} 唤醒超时(130s)')
        except Exception as e:
            log.warning(f'⚠️ {requested_agent_id} 唤醒异常: {e}')
    threading.Thread(target=do_wake, daemon=True).start()

    return {'ok': True, 'message': f'{requested_agent_id} 唤醒指令已发出，约10-30秒后生效'}


def send_agent_message(agent_id, message, timeout_sec=180):
    """向指定 Agent 发送控制台消息（非飞书入口）。"""
    requested_agent_id = str(agent_id or '').strip()
    if not _SAFE_NAME_RE.match(requested_agent_id):
        return {'ok': False, 'error': f'agent_id 非法: {requested_agent_id}'}
    runtime_id = _normalize_agent_id(requested_agent_id)
    if not _check_agent_workspace(runtime_id):
        return {'ok': False, 'error': f'{requested_agent_id} 工作空间不存在，请先配置'}
    if not _check_gateway_alive():
        return {'ok': False, 'error': 'Gateway 未启动，请先运行 openclaw gateway start'}
    text = str(message or '').strip()
    if not text:
        return {'ok': False, 'error': 'message 不能为空'}

    timeout_sec = max(60, min(600, int(timeout_sec or 180)))

    def _runner():
        try:
            cmd = ['openclaw', 'agent', '--agent', runtime_id, '-m', text, '--timeout', str(timeout_sec)]
            log.info(f'💬 控制台消息 -> {requested_agent_id}: {text[:80]}')
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_sec + 20)
            if result.returncode == 0:
                log.info(f'✅ {requested_agent_id} 控制台消息执行完成')
            else:
                err_msg = (result.stderr or result.stdout or '').strip()[:300]
                log.warning(f'⚠️ {requested_agent_id} 控制台消息执行失败: {err_msg}')
        except subprocess.TimeoutExpired:
            log.error(f'❌ {requested_agent_id} 控制台消息超时({timeout_sec + 20}s)')
        except Exception as e:
            log.warning(f'⚠️ {requested_agent_id} 控制台消息异常: {e}')

    threading.Thread(target=_runner, daemon=True).start()
    return {'ok': True, 'message': f'消息已发送到 {requested_agent_id}，约10-30秒可见回执'}


def _format_session_meta(session_key, meta):
    m = meta if isinstance(meta, dict) else {}
    status = str(m.get('status', '') or '').strip().lower()
    if not status:
        status = 'unknown'
    ended_at = m.get('endedAt')
    alive = status in {'running', 'queued', 'processing'}
    if ended_at and status == 'running':
        # 某些实现会遗留 running + endedAt，优先按 endedAt 视为已结束
        alive = False
    return {
        'sessionKey': session_key,
        'sessionId': str(m.get('sessionId', '') or ''),
        'status': status,
        'alive': alive,
        'updatedAt': m.get('updatedAt'),
        'startedAt': m.get('startedAt'),
        'endedAt': ended_at,
        'model': m.get('model'),
        'modelProvider': m.get('modelProvider'),
        'lastChannel': m.get('lastChannel'),
        'subagentRole': m.get('subagentRole'),
        'spawnDepth': m.get('spawnDepth'),
        'label': m.get('label'),
        'sessionFile': m.get('sessionFile'),
        'contextTokens': m.get('contextTokens'),
    }


def _coerce_ts_ms(raw):
    try:
        if raw is None:
            return 0
        if isinstance(raw, (int, float)):
            v = int(raw)
            if v > 10_000_000_000:  # ms
                return v
            if v > 0:
                return v * 1000
        s = str(raw or '').strip()
        if not s:
            return 0
        if s.isdigit():
            v = int(s)
            if v > 10_000_000_000:
                return v
            if v > 0:
                return v * 1000
            return 0
        dt = datetime.datetime.fromisoformat(s.replace('Z', '+00:00'))
        return int(dt.timestamp() * 1000)
    except Exception:
        return 0


def _entry_ts_ms(item):
    """提取 session 条目时间（毫秒）。
    同时兼容 item.timestamp 和 message.timestamp，避免时间口径不一致。
    """
    if not isinstance(item, dict):
        return 0
    msg = item.get('message') if isinstance(item.get('message'), dict) else {}
    return max(
        _coerce_ts_ms(item.get('timestamp')),
        _coerce_ts_ms(msg.get('timestamp')),
    )


def _estimate_tokens_from_text(raw_text_or_chars):
    if isinstance(raw_text_or_chars, int):
        chars = max(0, raw_text_or_chars)
    else:
        s = str(raw_text_or_chars or '')
        chars = len(s)
    if chars <= 0:
        return 0
    # 粗略估算：中英混排按约 4 chars/token
    return max(1, int(round(chars / 4.0)))


def _compute_session_context_usage(session_file: pathlib.Path, meta):
    """计算会话上下文占用（优先 usage.input，其次文本估算）。"""
    m = meta if isinstance(meta, dict) else {}
    ctx_max = int(m.get('contextTokens') or 0) if str(m.get('contextTokens') or '').isdigit() else 0
    system_prompt = ''
    spr = m.get('systemPromptReport')
    if isinstance(spr, dict):
        system_prompt = str(spr.get('systemPrompt') or '')

    usage_input_latest = 0
    approx_chars = len(system_prompt)
    message_count = 0
    last_ts_ms = 0
    visible_last_ts_ms = 0

    try:
        if session_file.exists():
            with session_file.open('r', errors='ignore') as fp:
                for ln in fp:
                    try:
                        item = json.loads(ln)
                    except Exception:
                        continue
                    message_count += 1
                    last_ts_ms = max(last_ts_ms, _entry_ts_ms(item))
                    # 与会话面板口径统一：优先取“可见对话条目”的最后时间
                    try:
                        parsed = _parse_activity_entry(item, compact=True)
                    except Exception:
                        parsed = None
                    if parsed:
                        visible_last_ts_ms = max(visible_last_ts_ms, _coerce_ts_ms(parsed.get('at')))
                    msg = item.get('message') if isinstance(item.get('message'), dict) else {}
                    usage = msg.get('usage') if isinstance(msg.get('usage'), dict) else {}
                    # 绝大多数 provider 会把当前请求上下文 token 放在 input 上
                    input_tok = usage.get('input')
                    if not isinstance(input_tok, int):
                        try:
                            input_tok = int(input_tok or 0)
                        except Exception:
                            input_tok = 0
                    if input_tok > 0:
                        usage_input_latest = input_tok
                    approx_chars += len(_collect_message_text(msg))
    except Exception:
        pass

    if usage_input_latest > 0:
        used = usage_input_latest
        source = 'usage'
    else:
        used = _estimate_tokens_from_text(approx_chars)
        source = 'estimated'

    if ctx_max <= 0:
        # 无上限配置时给一个保守默认，避免前端除零
        ctx_max = max(used, 1)
    pct = min(999.0, (float(used) / float(max(1, ctx_max))) * 100.0)
    return {
        'contextMaxTokens': int(ctx_max),
        'contextUsedTokens': int(used),
        'contextUsedPct': round(pct, 2),
        'contextUsageSource': source,
        'messageCount': int(message_count),
        'lastTalkAtTs': int(visible_last_ts_ms or last_ts_ms),
    }


def _infer_trigger_from_recent_text(text):
    s = str(text or '').lower()
    if not s:
        return ''
    checks = [
        ('人事部-SOUL重新整理触发', ['soul重新整理', '重整soul', '重整目标agent的soul文档', 'agent-soul/reorganize']),
        ('研发部复审触发', ['复审', '评审', 'rnd-review', '待澄清问题']),
        ('版本控制-更新版本触发', ['更新版本', '版本更新日志', 'version-generate', '版本更改清单']),
        ('项目设计-需求说明生成', ['需求说明', 'prd', 'design-requirements']),
        ('项目设计-架构设计生成', ['架构设计', 'mermaid', 'design-architecture']),
        ('项目设计-功能设计生成', ['功能设计', 'fsd', 'design-function']),
        ('研发部催办触发', ['催办', '继续推进', 'execute']),
    ]
    for label, kws in checks:
        if any(k in s for k in kws):
            return label
    return ''


def _read_recent_user_text(session_file: pathlib.Path, limit=30):
    if not session_file.exists():
        return ''
    lines = []
    try:
        raw = session_file.read_text(errors='ignore').splitlines()
    except Exception:
        return ''
    for ln in reversed(raw[-max(20, int(limit)):]):
        try:
            item = json.loads(ln)
        except Exception:
            continue
        msg = item.get('message') if isinstance(item.get('message'), dict) else {}
        if str(msg.get('role') or '').strip().lower() != 'user':
            continue
        txt = _collect_message_text(msg).strip()
        if txt:
            lines.append(txt[:3000])
        if len(lines) >= 2:
            break
    lines.reverse()
    return '\n'.join(lines)


def _derive_session_trigger(session_key, runtime_agent_id, meta, recent_text=''):
    m = meta if isinstance(meta, dict) else {}
    label = str(m.get('label') or '').strip()
    if label:
        return label

    key = str(session_key or '')
    rid = str(runtime_agent_id or '')
    low = key.lower()
    low_rid = rid.lower()

    if ':cron:' in low or low.endswith(':cron'):
        return '定时任务触发'
    if ':subagent:' in low:
        return '子任务分派触发'

    # 研发部/PM 常见隔离会话来源
    if '__pm__design-' in low_rid:
        if 'requirements' in low_rid:
            return '项目设计-需求说明生成'
        if 'architecture' in low_rid:
            return '项目设计-架构设计生成'
        if 'function' in low_rid:
            return '项目设计-功能设计生成'
        return '项目设计触发'
    if '__pm__version-generate__' in low_rid:
        return '版本控制-更新版本触发'
    if '__pm__rnd-review-review__' in low_rid:
        return '问题详情-研发部复审触发'
    if '__pm__rnd-review-execute__' in low_rid:
        return '问题详情-研发部催办触发'
    if '__pm__' in low_rid:
        return 'PM小组功能触发'

    last_channel = str(m.get('lastChannel') or '').strip().lower()
    if last_channel:
        return f'渠道触发: {last_channel}'

    by_text = _infer_trigger_from_recent_text(recent_text)
    if by_text:
        return by_text

    if low.endswith(':main'):
        return '主会话（手动/默认）'
    return '主会话（未标注来源）'


def _parse_session_entries(session_file: pathlib.Path, limit=120):
    entries = []
    if not session_file.exists():
        return entries
    try:
        lines = session_file.read_text(errors='ignore').splitlines()
    except Exception:
        return entries
    for ln in lines:
        try:
            item = json.loads(ln)
        except Exception:
            continue
        # 会话详情面板：尽量保留原文，避免“被截断”的阅读体验。
        parsed = _parse_activity_entry(item, compact=False)
        if not parsed:
            continue
        # 规范输出字段，便于前端展示
        role = parsed.get('kind', 'unknown')
        text = ''
        if role == 'assistant':
            text = parsed.get('text') or parsed.get('thinking') or ''
            if not text and parsed.get('tools'):
                t0 = parsed['tools'][0]
                text = f"[tool] {t0.get('name', '')} {t0.get('input_preview', '')}"
        elif role == 'tool_result':
            text = f"{parsed.get('tool', 'tool')} => {parsed.get('output', '')}"
        else:
            text = parsed.get('text', '')
        entries.append({
            'at': parsed.get('at'),
            'role': role,
            # 保留较大上限，防止超长日志撑爆接口响应
            'text': str(text or '')[:6000],
        })
    return entries[-max(1, min(int(limit or 120), 400)):]


def get_agent_sessions(agent_id):
    """返回 agent 当前会话列表（含存活标记）。"""
    related_ids = _collect_related_agent_ids(agent_id)
    if not related_ids:
        related_ids = [agent_id]
    sessions = []
    for rid in related_ids:
        sessions_dir = OCLAW_HOME / 'agents' / rid / 'sessions'
        sessions_file = sessions_dir / 'sessions.json'
        if not sessions_file.exists():
            continue
        try:
            data = json.loads(sessions_file.read_text())
        except Exception as e:
            return {'ok': False, 'error': f'sessions.json 读取失败({rid}): {e}'}
        if not isinstance(data, dict):
            continue
        for k, v in data.items():
            row = _format_session_meta(k, v)
            row['agentId'] = rid
            sid = str(row.get('sessionId') or '').strip()
            candidate_file = pathlib.Path(str(row.get('sessionFile') or '')).expanduser() if row.get('sessionFile') else (sessions_dir / f'{sid}.jsonl')
            content_bytes = 0
            file_mtime_ms = 0
            try:
                if candidate_file.exists():
                    st = candidate_file.stat()
                    content_bytes = int(st.st_size or 0)
                    file_mtime_ms = int(st.st_mtime * 1000)
            except Exception:
                pass
            updated_ms = _coerce_ts_ms(row.get('updatedAt'))
            usage_stats = _compute_session_context_usage(candidate_file, v if isinstance(v, dict) else {})
            msg_last_talk_ms = int(usage_stats.get('lastTalkAtTs') or 0)
            # 口径统一：会话概览“上次对话”优先取真实消息时间，与会话内容面板一致。
            # 只有完全无消息时，才回退到元数据更新时间。
            if msg_last_talk_ms > 0:
                last_talk_ms = msg_last_talk_ms
            else:
                last_talk_ms = max(updated_ms, file_mtime_ms)
            recent_text = _read_recent_user_text(candidate_file, limit=40)
            row['contentBytes'] = content_bytes
            row['messageCount'] = int(usage_stats.get('messageCount') or 0)
            row['lastTalkAtTs'] = last_talk_ms
            row['contextMaxTokens'] = int(usage_stats.get('contextMaxTokens') or 0)
            row['contextUsedTokens'] = int(usage_stats.get('contextUsedTokens') or 0)
            row['contextUsedPct'] = float(usage_stats.get('contextUsedPct') or 0.0)
            row['contextUsageSource'] = usage_stats.get('contextUsageSource') or 'estimated'
            row['triggerReason'] = _derive_session_trigger(k, rid, v if isinstance(v, dict) else {}, recent_text=recent_text)
            sessions.append(row)
    sessions.sort(key=lambda x: int(x.get('lastTalkAtTs') or _coerce_ts_ms(x.get('updatedAt')) or 0), reverse=True)
    alive_count = sum(1 for s in sessions if s.get('alive'))
    return {
        'ok': True,
        'agentId': agent_id,
        'relatedAgentIds': related_ids,
        'sessions': sessions,
        'aliveCount': alive_count,
    }


def get_agent_session_log(agent_id, session_id, limit=120):
    safe_sid = str(session_id or '').strip()
    if not safe_sid:
        return {'ok': False, 'error': 'sessionId required'}
    if not re.fullmatch(r'[a-zA-Z0-9\-]{8,64}', safe_sid):
        return {'ok': False, 'error': 'invalid sessionId'}
    related_ids = _collect_related_agent_ids(agent_id)
    if not related_ids:
        related_ids = [agent_id]
    session_file = None
    actual_agent_id = agent_id
    for rid in related_ids:
        candidate = OCLAW_HOME / 'agents' / rid / 'sessions' / f'{safe_sid}.jsonl'
        if candidate.exists():
            session_file = candidate
            actual_agent_id = rid
            break
    if session_file is None:
        # 回退到传入 agent 目录，保持兼容
        session_file = OCLAW_HOME / 'agents' / agent_id / 'sessions' / f'{safe_sid}.jsonl'
    entries = _parse_session_entries(session_file, limit=limit)
    return {
        'ok': True,
        'agentId': actual_agent_id,
        'requestedAgentId': agent_id,
        'sessionId': safe_sid,
        'exists': session_file.exists(),
        'entries': entries,
    }


# ══ Agent 实时活动读取 ══

# 状态 → agent_id 映射
_STATE_AGENT_MAP = {
    'Taizi': 'taizi',
    'Zhongshu': 'zhongshu',
    'Menxia': 'menxia',
    'Assigned': 'shangshu',
    'Doing': None,         # 六部，需从 org 推断
    'Review': 'shangshu',
    'Next': None,          # 待执行，从 org 推断
    'Pending': 'zhongshu', # 待处理，默认中书省
}
_ORG_AGENT_MAP = {
    '礼部': 'libu', '藏经阁': 'libu', '户部': 'hubu', '兵部': 'bingbu',
    'PM小组': 'bingbu',
    '刑部': 'xingbu', '工部': 'rnd', '研发部': 'rnd', '吏部': 'libu_hr', '人事部': 'libu_hr',
    '中书省': 'zhongshu', '门下省': 'menxia', '尚书省': 'shangshu',
}

_TERMINAL_STATES = {'Done', 'Cancelled'}
_DISPATCH_DEDUP_SECONDS = 180


def _parse_iso(ts):
    if not ts or not isinstance(ts, str):
        return None
    try:
        return datetime.datetime.fromisoformat(ts.replace('Z', '+00:00'))
    except Exception:
        return None


def _ensure_scheduler(task):
    sched = task.setdefault('_scheduler', {})
    if not isinstance(sched, dict):
        sched = {}
        task['_scheduler'] = sched
    sched.setdefault('enabled', True)
    sched.setdefault('stallThresholdSec', 600)
    sched.setdefault('maxRetry', 2)
    sched.setdefault('retryCount', 0)
    sched.setdefault('escalationLevel', 0)
    sched.setdefault('autoRollback', True)
    if not sched.get('lastProgressAt'):
        sched['lastProgressAt'] = task.get('updatedAt') or now_iso()
    if 'stallSince' not in sched:
        sched['stallSince'] = None
    if 'lastDispatchStatus' not in sched:
        sched['lastDispatchStatus'] = 'idle'
    if 'snapshot' not in sched:
        sched['snapshot'] = {
            'state': task.get('state', ''),
            'org': task.get('org', ''),
            'now': task.get('now', ''),
            'savedAt': now_iso(),
            'note': 'init',
        }
    return sched


def _scheduler_add_flow(task, remark, to=''):
    task.setdefault('flow_log', []).append({
        'at': now_iso(),
        'from': '太子调度',
        'to': to or task.get('org', ''),
        'remark': f'🧭 {remark}'
    })


def _scheduler_snapshot(task, note=''):
    sched = _ensure_scheduler(task)
    sched['snapshot'] = {
        'state': task.get('state', ''),
        'org': task.get('org', ''),
        'now': task.get('now', ''),
        'savedAt': now_iso(),
        'note': note or 'snapshot',
    }


def _scheduler_mark_progress(task, note=''):
    sched = _ensure_scheduler(task)
    sched['lastProgressAt'] = now_iso()
    sched['stallSince'] = None
    sched['retryCount'] = 0
    sched['escalationLevel'] = 0
    sched['lastEscalatedAt'] = None
    if note:
        _scheduler_add_flow(task, f'进展确认：{note}')


def _update_task_scheduler(task_id, updater):
    tasks = load_tasks()
    task = next((t for t in tasks if t.get('id') == task_id), None)
    if not task:
        return False
    sched = _ensure_scheduler(task)
    updater(task, sched)
    task['updatedAt'] = now_iso()
    save_tasks(tasks)
    return True


def get_scheduler_state(task_id):
    tasks = load_tasks()
    task = next((t for t in tasks if t.get('id') == task_id), None)
    if not task:
        return {'ok': False, 'error': f'任务 {task_id} 不存在'}
    sched = _ensure_scheduler(task)
    last_progress = _parse_iso(sched.get('lastProgressAt') or task.get('updatedAt'))
    now_dt = datetime.datetime.now(datetime.timezone.utc)
    stalled_sec = 0
    if last_progress:
        stalled_sec = max(0, int((now_dt - last_progress).total_seconds()))
    return {
        'ok': True,
        'taskId': task_id,
        'state': task.get('state', ''),
        'org': task.get('org', ''),
        'scheduler': sched,
        'stalledSec': stalled_sec,
        'checkedAt': now_iso(),
    }


def handle_scheduler_retry(task_id, reason=''):
    tasks = load_tasks()
    task = next((t for t in tasks if t.get('id') == task_id), None)
    if not task:
        return {'ok': False, 'error': f'任务 {task_id} 不存在'}
    state = task.get('state', '')
    if state in _TERMINAL_STATES or state == 'Blocked':
        return {'ok': False, 'error': f'任务 {task_id} 当前状态 {state} 不支持重试'}

    sched = _ensure_scheduler(task)
    sched['retryCount'] = int(sched.get('retryCount') or 0) + 1
    sched['lastRetryAt'] = now_iso()
    sched['lastDispatchTrigger'] = 'taizi-retry'
    _scheduler_add_flow(task, f'触发重试第{sched["retryCount"]}次：{reason or "超时未推进"}')
    task['updatedAt'] = now_iso()
    save_tasks(tasks)

    dispatch_for_state(task_id, task, state, trigger='taizi-retry')
    return {'ok': True, 'message': f'{task_id} 已触发重试派发', 'retryCount': sched['retryCount']}


def handle_scheduler_escalate(task_id, reason=''):
    tasks = load_tasks()
    task = next((t for t in tasks if t.get('id') == task_id), None)
    if not task:
        return {'ok': False, 'error': f'任务 {task_id} 不存在'}
    state = task.get('state', '')
    if state in _TERMINAL_STATES:
        return {'ok': False, 'error': f'任务 {task_id} 已结束，无需升级'}

    sched = _ensure_scheduler(task)
    current_level = int(sched.get('escalationLevel') or 0)
    next_level = min(current_level + 1, 2)
    target = 'menxia' if next_level == 1 else 'shangshu'
    target_label = '门下省' if next_level == 1 else '尚书省'

    sched['escalationLevel'] = next_level
    sched['lastEscalatedAt'] = now_iso()
    _scheduler_add_flow(task, f'升级到{target_label}协调：{reason or "任务停滞"}', to=target_label)
    task['updatedAt'] = now_iso()
    save_tasks(tasks)

    msg = (
        f'🧭 太子调度升级通知\n'
        f'任务ID: {task_id}\n'
        f'当前状态: {state}\n'
        f'停滞处理: 请你介入协调推进\n'
        f'原因: {reason or "任务超过阈值未推进"}\n'
        f'⚠️ 看板已有任务，请勿重复创建。'
    )
    wake_agent(target, msg)

    return {'ok': True, 'message': f'{task_id} 已升级至{target_label}', 'escalationLevel': next_level}


def handle_scheduler_rollback(task_id, reason=''):
    tasks = load_tasks()
    task = next((t for t in tasks if t.get('id') == task_id), None)
    if not task:
        return {'ok': False, 'error': f'任务 {task_id} 不存在'}
    sched = _ensure_scheduler(task)
    snapshot = sched.get('snapshot') or {}
    snap_state = snapshot.get('state')
    if not snap_state:
        return {'ok': False, 'error': f'任务 {task_id} 无可用回滚快照'}

    old_state = task.get('state', '')
    task['state'] = snap_state
    task['org'] = snapshot.get('org', task.get('org', ''))
    task['now'] = f'↩️ 太子调度自动回滚：{reason or "恢复到上个稳定节点"}'
    task['block'] = '无'
    sched['retryCount'] = 0
    sched['escalationLevel'] = 0
    sched['stallSince'] = None
    sched['lastProgressAt'] = now_iso()
    _scheduler_add_flow(task, f'执行回滚：{old_state} → {snap_state}，原因：{reason or "停滞恢复"}')
    task['updatedAt'] = now_iso()
    save_tasks(tasks)

    if snap_state not in _TERMINAL_STATES:
        dispatch_for_state(task_id, task, snap_state, trigger='taizi-rollback')

    return {'ok': True, 'message': f'{task_id} 已回滚到 {snap_state}'}


def handle_scheduler_scan(threshold_sec=600):
    threshold_sec = max(60, int(threshold_sec or 600))
    tasks = load_tasks()
    now_dt = datetime.datetime.now(datetime.timezone.utc)
    pending_retries = []
    pending_escalates = []
    pending_rollbacks = []
    actions = []
    changed = False

    for task in tasks:
        task_id = task.get('id', '')
        state = task.get('state', '')
        if not task_id or state in _TERMINAL_STATES or task.get('archived'):
            continue
        if state == 'Blocked':
            continue

        sched = _ensure_scheduler(task)
        task_threshold = int(sched.get('stallThresholdSec') or threshold_sec)
        last_progress = _parse_iso(sched.get('lastProgressAt') or task.get('updatedAt'))
        if not last_progress:
            continue
        stalled_sec = max(0, int((now_dt - last_progress).total_seconds()))
        if stalled_sec < task_threshold:
            continue

        if not sched.get('stallSince'):
            sched['stallSince'] = now_iso()
            changed = True

        retry_count = int(sched.get('retryCount') or 0)
        max_retry = max(0, int(sched.get('maxRetry') or 1))
        level = int(sched.get('escalationLevel') or 0)

        if retry_count < max_retry:
            sched['retryCount'] = retry_count + 1
            sched['lastRetryAt'] = now_iso()
            sched['lastDispatchTrigger'] = 'taizi-scan-retry'
            _scheduler_add_flow(task, f'停滞{stalled_sec}秒，触发自动重试第{sched["retryCount"]}次')
            pending_retries.append((task_id, state))
            actions.append({'taskId': task_id, 'action': 'retry', 'stalledSec': stalled_sec})
            changed = True
            continue

        if level < 2:
            next_level = level + 1
            target = 'menxia' if next_level == 1 else 'shangshu'
            target_label = '门下省' if next_level == 1 else '尚书省'
            sched['escalationLevel'] = next_level
            sched['lastEscalatedAt'] = now_iso()
            _scheduler_add_flow(task, f'停滞{stalled_sec}秒，升级至{target_label}协调', to=target_label)
            pending_escalates.append((task_id, state, target, target_label, stalled_sec))
            actions.append({'taskId': task_id, 'action': 'escalate', 'to': target_label, 'stalledSec': stalled_sec})
            changed = True
            continue

        if sched.get('autoRollback', True):
            snapshot = sched.get('snapshot') or {}
            snap_state = snapshot.get('state')
            if snap_state and snap_state != state:
                old_state = state
                task['state'] = snap_state
                task['org'] = snapshot.get('org', task.get('org', ''))
                task['now'] = '↩️ 太子调度自动回滚到稳定节点'
                task['block'] = '无'
                sched['retryCount'] = 0
                sched['escalationLevel'] = 0
                sched['stallSince'] = None
                sched['lastProgressAt'] = now_iso()
                _scheduler_add_flow(task, f'连续停滞，自动回滚：{old_state} → {snap_state}')
                pending_rollbacks.append((task_id, snap_state))
                actions.append({'taskId': task_id, 'action': 'rollback', 'toState': snap_state})
                changed = True

    if changed:
        save_tasks(tasks)

    for task_id, state in pending_retries:
        retry_task = next((t for t in tasks if t.get('id') == task_id), None)
        if retry_task:
            dispatch_for_state(task_id, retry_task, state, trigger='taizi-scan-retry')

    for task_id, state, target, target_label, stalled_sec in pending_escalates:
        msg = (
            f'🧭 太子调度升级通知\n'
            f'任务ID: {task_id}\n'
            f'当前状态: {state}\n'
            f'已停滞: {stalled_sec} 秒\n'
            f'请立即介入协调推进\n'
            f'⚠️ 看板已有任务，请勿重复创建。'
        )
        wake_agent(target, msg)

    for task_id, state in pending_rollbacks:
        rollback_task = next((t for t in tasks if t.get('id') == task_id), None)
        if rollback_task and state not in _TERMINAL_STATES:
            dispatch_for_state(task_id, rollback_task, state, trigger='taizi-auto-rollback')

    return {
        'ok': True,
        'thresholdSec': threshold_sec,
        'actions': actions,
        'count': len(actions),
        'checkedAt': now_iso(),
    }


def _startup_recover_queued_dispatches():
    """服务启动后扫描 lastDispatchStatus=queued 的任务，重新派发。
    解决：kill -9 重启导致派发线程中断、任务永久卡住的问题。"""
    tasks = load_tasks()
    recovered = 0
    for task in tasks:
        task_id = task.get('id', '')
        state = task.get('state', '')
        if not task_id or state in _TERMINAL_STATES or task.get('archived'):
            continue
        sched = task.get('_scheduler') or {}
        if sched.get('lastDispatchStatus') == 'queued':
            log.info(f'🔄 启动恢复: {task_id} 状态={state} 上次派发未完成，重新派发')
            sched['lastDispatchTrigger'] = 'startup-recovery'
            dispatch_for_state(task_id, task, state, trigger='startup-recovery')
            recovered += 1
    if recovered:
        log.info(f'✅ 启动恢复完成: 重新派发 {recovered} 个任务')
    else:
        log.info(f'✅ 启动恢复: 无需恢复')


def handle_repair_flow_order():
    """修复历史任务中首条流转为“皇上->中书省”的错序问题。"""
    tasks = load_tasks()
    fixed = 0
    fixed_ids = []

    for task in tasks:
        task_id = task.get('id', '')
        if not task_id.startswith('JJC-'):
            continue
        flow_log = task.get('flow_log') or []
        if not flow_log:
            continue

        first = flow_log[0]
        if first.get('from') != '皇上' or first.get('to') != '中书省':
            continue

        first['to'] = '太子'
        remark = first.get('remark', '')
        if isinstance(remark, str) and remark.startswith('下旨：'):
            first['remark'] = remark

        if task.get('state') == 'Zhongshu' and task.get('org') == '中书省' and len(flow_log) == 1:
            task['state'] = 'Taizi'
            task['org'] = '太子'
            task['now'] = '等待太子接旨分拣'

        task['updatedAt'] = now_iso()
        fixed += 1
        fixed_ids.append(task_id)

    if fixed:
        save_tasks(tasks)

    return {
        'ok': True,
        'count': fixed,
        'taskIds': fixed_ids[:80],
        'more': max(0, fixed - 80),
        'checkedAt': now_iso(),
    }


def _collect_message_text(msg):
    """收集消息中的可检索文本，用于 task_id/关键词过滤。"""
    parts = []
    for c in msg.get('content', []) or []:
        ctype = c.get('type')
        if ctype == 'text' and c.get('text'):
            parts.append(str(c.get('text', '')))
        elif ctype == 'thinking' and c.get('thinking'):
            parts.append(str(c.get('thinking', '')))
        elif ctype == 'tool_use':
            parts.append(json.dumps(c.get('input', {}), ensure_ascii=False))
    details = msg.get('details') or {}
    for key in ('output', 'stdout', 'stderr', 'message'):
        val = details.get(key)
        if isinstance(val, str) and val:
            parts.append(val)
    return ''.join(parts)


def _parse_activity_entry(item, compact=True):
    """将 session jsonl 的 message 统一解析成看板活动条目。"""
    msg = item.get('message') or {}
    role = str(msg.get('role', '')).strip().lower()
    ts = item.get('timestamp', '')
    ts_ms = _entry_ts_ms(item)
    if ts_ms > 0:
        try:
            ts = datetime.datetime.fromtimestamp(ts_ms / 1000.0, datetime.timezone.utc).isoformat().replace('+00:00', 'Z')
        except Exception:
            pass

    if role == 'assistant':
        text = ''
        thinking = ''
        tool_calls = []
        for c in msg.get('content', []) or []:
            if c.get('type') == 'text' and c.get('text') and not text:
                text = str(c.get('text', '')).strip()
            elif c.get('type') == 'thinking' and c.get('thinking') and not thinking:
                thinking = str(c.get('thinking', '')).strip()
            elif c.get('type') == 'tool_use':
                tool_calls.append({
                    'name': c.get('name', ''),
                    'input_preview': json.dumps(c.get('input', {}), ensure_ascii=False)[:100]
                })
        if not (text or thinking or tool_calls):
            return None
        entry = {'at': ts, 'kind': 'assistant'}
        if text:
            entry['text'] = text[:300] if compact else text
        if thinking:
            entry['thinking'] = thinking[:200] if compact else thinking
        if tool_calls:
            entry['tools'] = tool_calls
        return entry

    if role in ('toolresult', 'tool_result'):
        details = msg.get('details') or {}
        code = details.get('exitCode')
        if code is None:
            code = details.get('code', details.get('status'))
        output = ''
        for c in msg.get('content', []) or []:
            if c.get('type') == 'text' and c.get('text'):
                output = str(c.get('text', '')).strip()
                break
        if not output:
            for key in ('output', 'stdout', 'stderr', 'message'):
                val = details.get(key)
                if isinstance(val, str) and val.strip():
                    output = val.strip()
                    break

        entry = {
            'at': ts,
            'kind': 'tool_result',
            'tool': msg.get('toolName', msg.get('name', '')),
            'exitCode': code,
            'output': output[:200] if compact else output,
        }
        duration_ms = details.get('durationMs')
        if isinstance(duration_ms, (int, float)):
            entry['durationMs'] = int(duration_ms)
        return entry

    if role == 'user':
        text = ''
        for c in msg.get('content', []) or []:
            if c.get('type') == 'text' and c.get('text'):
                text = str(c.get('text', '')).strip()
                break
        if not text:
            return None
        return {'at': ts, 'kind': 'user', 'text': text[:200] if compact else text}

    return None


def get_agent_activity(agent_id, limit=30, task_id=None):
    """从 Agent 的 session jsonl 读取最近活动。
    如果 task_id 不为空，只返回提及该 task_id 的相关条目。
    """
    related_ids = _collect_related_agent_ids(agent_id)
    if not related_ids:
        related_ids = [agent_id]

    jsonl_files = []
    for rid in related_ids:
        sessions_dir = OCLAW_HOME / 'agents' / rid / 'sessions'
        if not sessions_dir.exists():
            continue
        jsonl_files.extend(list(sessions_dir.glob('*.jsonl')))
    # 扫描所有 jsonl（按修改时间倒序），优先最新
    jsonl_files = sorted(jsonl_files, key=lambda f: f.stat().st_mtime, reverse=True)
    if not jsonl_files:
        return []

    entries = []
    # 如果需要按 task_id 过滤，可能需要扫描多个文件
    files_to_scan = jsonl_files[:3] if task_id else jsonl_files[:1]

    for session_file in files_to_scan:
        try:
            lines = session_file.read_text(errors='ignore').splitlines()
        except Exception:
            continue

        # 正向扫描以保持时间顺序；如果有 task_id，收集提及 task_id 的条目
        for ln in lines:
            try:
                item = json.loads(ln)
            except Exception:
                continue
            msg = item.get('message') or {}
            all_text = _collect_message_text(msg)

            # task_id 过滤：只保留提及 task_id 的条目
            if task_id and task_id not in all_text:
                continue
            entry = _parse_activity_entry(item)
            if entry:
                entries.append(entry)

            if len(entries) >= limit:
                break
        if len(entries) >= limit:
            break

    # 只保留最后 limit 条
    return entries[-limit:]


def _extract_keywords(title):
    """从任务标题中提取有意义的关键词（用于 session 内容匹配）。"""
    stop = {'的', '了', '在', '是', '有', '和', '与', '或', '一个', '一篇', '关于', '进行',
            '写', '做', '请', '把', '给', '用', '要', '需要', '面向', '风格', '包含',
            '出', '个', '不', '可以', '应该', '如何', '怎么', '什么', '这个', '那个'}
    # 提取英文词
    en_words = re.findall(r'[a-zA-Z][\w.-]{1,}', title)
    # 提取 2-4 字中文词组（更短的颗粒度）
    cn_words = re.findall(r'[\u4e00-\u9fff]{2,4}', title)
    all_words = en_words + cn_words
    kws = [w for w in all_words if w not in stop and len(w) >= 2]
    # 去重保序
    seen = set()
    unique = []
    for w in kws:
        if w.lower() not in seen:
            seen.add(w.lower())
            unique.append(w)
    return unique[:8]  # 最多 8 个关键词


def get_agent_activity_by_keywords(agent_id, keywords, limit=20):
    """从 agent session 中按关键词匹配获取活动条目。
    找到包含关键词的 session 文件，只读该文件的活动。
    """
    sessions_dir = OCLAW_HOME / 'agents' / agent_id / 'sessions'
    if not sessions_dir.exists():
        return []

    jsonl_files = sorted(sessions_dir.glob('*.jsonl'), key=lambda f: f.stat().st_mtime, reverse=True)
    if not jsonl_files:
        return []

    # 找到包含关键词的 session 文件
    target_file = None
    for sf in jsonl_files[:5]:
        try:
            content = sf.read_text(errors='ignore')
        except Exception:
            continue
        hits = sum(1 for kw in keywords if kw.lower() in content.lower())
        if hits >= min(2, len(keywords)):
            target_file = sf
            break

    if not target_file:
        return []

    # 解析 session 文件，按 user 消息分割为对话段
    # 找到包含关键词的对话段，只返回该段的活动
    try:
        lines = target_file.read_text(errors='ignore').splitlines()
    except Exception:
        return []

    # 第一遍：找到关键词匹配的 user 消息位置
    user_msg_indices = []  # (line_index, user_text)
    for i, ln in enumerate(lines):
        try:
            item = json.loads(ln)
        except Exception:
            continue
        msg = item.get('message') or {}
        if msg.get('role') == 'user':
            text = ''
            for c in msg.get('content', []):
                if c.get('type') == 'text' and c.get('text'):
                    text += c['text']
            user_msg_indices.append((i, text))

    # 找到与关键词匹配度最高的 user 消息
    best_idx = -1
    best_hits = 0
    for line_idx, utext in user_msg_indices:
        hits = sum(1 for kw in keywords if kw.lower() in utext.lower())
        if hits > best_hits:
            best_hits = hits
            best_idx = line_idx

    # 确定对话段的行范围：从匹配的 user 消息到下一个 user 消息之前
    if best_idx >= 0 and best_hits >= min(2, len(keywords)):
        # 找下一个 user 消息的位置
        next_user_idx = len(lines)
        for line_idx, _ in user_msg_indices:
            if line_idx > best_idx:
                next_user_idx = line_idx
                break
        start_line = best_idx
        end_line = next_user_idx
    else:
        # 没找到匹配的对话段，返回空
        return []

    # 第二遍：只解析对话段内的行
    entries = []
    for ln in lines[start_line:end_line]:
        try:
            item = json.loads(ln)
        except Exception:
            continue
        entry = _parse_activity_entry(item)
        if entry:
            entries.append(entry)

    return entries[-limit:]


def get_agent_latest_segment(agent_id, limit=20):
    """获取 Agent 最新一轮对话段（最后一条 user 消息起的所有内容）。
    用于活跃任务没有精确匹配时，展示 Agent 的实时工作状态。
    """
    sessions_dir = OCLAW_HOME / 'agents' / agent_id / 'sessions'
    if not sessions_dir.exists():
        return []

    jsonl_files = sorted(sessions_dir.glob('*.jsonl'),
                         key=lambda f: f.stat().st_mtime, reverse=True)
    if not jsonl_files:
        return []

    # 读取最新的 session 文件
    target_file = jsonl_files[0]
    try:
        lines = target_file.read_text(errors='ignore').splitlines()
    except Exception:
        return []

    # 找到最后一条 user 消息的行号
    last_user_idx = -1
    for i, ln in enumerate(lines):
        try:
            item = json.loads(ln)
        except Exception:
            continue
        msg = item.get('message') or {}
        if msg.get('role') == 'user':
            last_user_idx = i

    if last_user_idx < 0:
        return []

    # 从最后一条 user 消息开始，解析到文件末尾
    entries = []
    for ln in lines[last_user_idx:]:
        try:
            item = json.loads(ln)
        except Exception:
            continue
        entry = _parse_activity_entry(item)
        if entry:
            entries.append(entry)

    return entries[-limit:]


def _compute_phase_durations(flow_log):
    """从 flow_log 计算每个阶段的停留时长。"""
    if not flow_log or len(flow_log) < 1:
        return []
    phases = []
    for i, fl in enumerate(flow_log):
        start_at = fl.get('at', '')
        to_dept = fl.get('to', '')
        remark = fl.get('remark', '')
        # 下一阶段的起始时间就是本阶段的结束时间
        if i + 1 < len(flow_log):
            end_at = flow_log[i + 1].get('at', '')
            ongoing = False
        else:
            end_at = now_iso()
            ongoing = True
        # 计算时长
        dur_sec = 0
        try:
            from_dt = datetime.datetime.fromisoformat(start_at.replace('Z', '+00:00'))
            to_dt = datetime.datetime.fromisoformat(end_at.replace('Z', '+00:00'))
            dur_sec = max(0, int((to_dt - from_dt).total_seconds()))
        except Exception:
            pass
        # 人类可读时长
        if dur_sec < 60:
            dur_text = f'{dur_sec}秒'
        elif dur_sec < 3600:
            dur_text = f'{dur_sec // 60}分{dur_sec % 60}秒'
        elif dur_sec < 86400:
            h, rem = divmod(dur_sec, 3600)
            dur_text = f'{h}小时{rem // 60}分'
        else:
            d, rem = divmod(dur_sec, 86400)
            dur_text = f'{d}天{rem // 3600}小时'
        phases.append({
            'phase': to_dept,
            'from': start_at,
            'to': end_at,
            'durationSec': dur_sec,
            'durationText': dur_text,
            'ongoing': ongoing,
            'remark': remark,
        })
    return phases


def _compute_todos_summary(todos):
    """计算 todos 完成率汇总。"""
    if not todos:
        return None
    total = len(todos)
    completed = sum(1 for t in todos if t.get('status') == 'completed')
    in_progress = sum(1 for t in todos if t.get('status') == 'in-progress')
    not_started = total - completed - in_progress
    percent = round(completed / total * 100) if total else 0
    return {
        'total': total,
        'completed': completed,
        'inProgress': in_progress,
        'notStarted': not_started,
        'percent': percent,
    }


def _compute_todos_diff(prev_todos, curr_todos):
    """计算两个 todos 快照之间的差异。"""
    prev_map = {str(t.get('id', '')): t for t in (prev_todos or [])}
    curr_map = {str(t.get('id', '')): t for t in (curr_todos or [])}
    changed, added, removed = [], [], []
    for tid, ct in curr_map.items():
        if tid in prev_map:
            pt = prev_map[tid]
            if pt.get('status') != ct.get('status'):
                changed.append({
                    'id': tid, 'title': ct.get('title', ''),
                    'from': pt.get('status', ''), 'to': ct.get('status', ''),
                })
        else:
            added.append({'id': tid, 'title': ct.get('title', '')})
    for tid, pt in prev_map.items():
        if tid not in curr_map:
            removed.append({'id': tid, 'title': pt.get('title', '')})
    if not changed and not added and not removed:
        return None
    return {'changed': changed, 'added': added, 'removed': removed}


def get_task_activity(task_id):
    """获取任务的实时进展数据。
    数据来源：
    1. 任务自身的 now / todos / flow_log 字段（由 Agent 通过 progress 命令主动上报）
    2. Agent session JSONL 中的对话日志（thinking / tool_result / user，用于展示思考过程）

    增强字段:
    - taskMeta: 任务元信息 (title/state/org/output/block/priority/reviewRound/archived)
    - phaseDurations: 各阶段停留时长
    - todosSummary: todos 完成率汇总
    - resourceSummary: Agent 资源消耗汇总 (tokens/cost/elapsed)
    - activity 条目中 progress/todos 保留 state/org 快照
    - activity 中 todos 条目含 diff 字段
    """
    tasks = load_tasks()
    task = next((t for t in tasks if t.get('id') == task_id), None)
    if not task:
        return {'ok': False, 'error': f'任务 {task_id} 不存在'}

    state = task.get('state', '')
    org = task.get('org', '')
    now_text = task.get('now', '')
    todos = task.get('todos', [])
    updated_at = task.get('updatedAt', '')

    # ── 任务元信息 ──
    task_meta = {
        'title': task.get('title', ''),
        'state': state,
        'org': org,
        'output': task.get('output', ''),
        'block': task.get('block', ''),
        'priority': task.get('priority', 'normal'),
        'reviewRound': task.get('review_round', 0),
        'archived': task.get('archived', False),
    }

    # 当前负责 Agent（兼容旧逻辑）
    agent_id = _STATE_AGENT_MAP.get(state)
    if agent_id is None and state in ('Doing', 'Next'):
        agent_id = _ORG_AGENT_MAP.get(org)

    # ── 构建活动条目列表（flow_log + progress_log）──
    activity = []
    flow_log = task.get('flow_log', [])

    # 1. flow_log 转为活动条目
    for fl in flow_log:
        activity.append({
            'at': fl.get('at', ''),
            'kind': 'flow',
            'from': fl.get('from', ''),
            'to': fl.get('to', ''),
            'remark': fl.get('remark', ''),
        })

    progress_log = task.get('progress_log', [])
    related_agents = set()

    # 资源消耗累加
    total_tokens = 0
    total_cost = 0.0
    total_elapsed = 0
    has_resource_data = False

    # 用于 todos diff 计算
    prev_todos_snapshot = None

    if progress_log:
        # 2. 多 Agent 实时进展日志（每条 progress 都保留自己的 todo 快照）
        for pl in progress_log:
            p_at = pl.get('at', '')
            p_agent = pl.get('agent', '')
            p_text = pl.get('text', '')
            p_todos = pl.get('todos', [])
            p_state = pl.get('state', '')
            p_org = pl.get('org', '')
            if p_agent:
                related_agents.add(p_agent)
            # 累加资源消耗
            if pl.get('tokens'):
                total_tokens += pl['tokens']
                has_resource_data = True
            if pl.get('cost'):
                total_cost += pl['cost']
                has_resource_data = True
            if pl.get('elapsed'):
                total_elapsed += pl['elapsed']
                has_resource_data = True
            if p_text:
                entry = {
                    'at': p_at,
                    'kind': 'progress',
                    'text': p_text,
                    'agent': p_agent,
                    'agentLabel': pl.get('agentLabel', ''),
                    'state': p_state,
                    'org': p_org,
                }
                # 单条资源数据
                if pl.get('tokens'):
                    entry['tokens'] = pl['tokens']
                if pl.get('cost'):
                    entry['cost'] = pl['cost']
                if pl.get('elapsed'):
                    entry['elapsed'] = pl['elapsed']
                activity.append(entry)
            if p_todos:
                todos_entry = {
                    'at': p_at,
                    'kind': 'todos',
                    'items': p_todos,
                    'agent': p_agent,
                    'agentLabel': pl.get('agentLabel', ''),
                    'state': p_state,
                    'org': p_org,
                }
                # 计算 diff
                diff = _compute_todos_diff(prev_todos_snapshot, p_todos)
                if diff:
                    todos_entry['diff'] = diff
                activity.append(todos_entry)
                prev_todos_snapshot = p_todos

        # 仅当无法通过状态确定 Agent 时，才回退到最后一次上报的 Agent
        if not agent_id:
            last_pl = progress_log[-1]
            if last_pl.get('agent'):
                agent_id = last_pl.get('agent')
    else:
        # 兼容旧数据：仅使用 now/todos
        if now_text:
            activity.append({
                'at': updated_at,
                'kind': 'progress',
                'text': now_text,
                'agent': agent_id or '',
                'state': state,
                'org': org,
            })
        if todos:
            activity.append({
                'at': updated_at,
                'kind': 'todos',
                'items': todos,
                'agent': agent_id or '',
                'state': state,
                'org': org,
            })

    # 按时间排序，保证流转/进展穿插正确
    activity.sort(key=lambda x: x.get('at', ''))

    if agent_id:
        related_agents.add(agent_id)

    # ── 融合 Agent Session 活动（thinking / tool_result / user）──
    # 从 session JSONL 中提取 Agent 的思考过程和工具调用记录
    try:
        session_entries = []
        # 活跃任务：尝试按 task_id 精确匹配
        if state not in ('Done', 'Cancelled'):
            if agent_id:
                entries = get_agent_activity(agent_id, limit=30, task_id=task_id)
                session_entries.extend(entries)
            # 也从其他相关 Agent 获取
            for ra in related_agents:
                if ra != agent_id:
                    entries = get_agent_activity(ra, limit=20, task_id=task_id)
                    session_entries.extend(entries)
        else:
            # 已完成任务：基于关键词匹配
            title = task.get('title', '')
            keywords = _extract_keywords(title)
            if keywords:
                agents_to_scan = list(related_agents) if related_agents else ([agent_id] if agent_id else [])
                for ra in agents_to_scan[:5]:
                    entries = get_agent_activity_by_keywords(ra, keywords, limit=15)
                    session_entries.extend(entries)
        # 去重（通过 at+kind 去重避免重复）
        existing_keys = {(a.get('at', ''), a.get('kind', '')) for a in activity}
        for se in session_entries:
            key = (se.get('at', ''), se.get('kind', ''))
            if key not in existing_keys:
                activity.append(se)
                existing_keys.add(key)
        # 重新排序
        activity.sort(key=lambda x: x.get('at', ''))
    except Exception as e:
        log.warning(f'Session JSONL 融合失败 (task={task_id}): {e}')

    # ── 阶段耗时统计 ──
    phase_durations = _compute_phase_durations(flow_log)

    # ── Todos 汇总 ──
    todos_summary = _compute_todos_summary(todos)

    # ── 总耗时（首条 flow_log 到最后一条/当前） ──
    total_duration = None
    if flow_log:
        try:
            first_at = datetime.datetime.fromisoformat(flow_log[0].get('at', '').replace('Z', '+00:00'))
            if state in ('Done', 'Cancelled') and len(flow_log) >= 2:
                last_at = datetime.datetime.fromisoformat(flow_log[-1].get('at', '').replace('Z', '+00:00'))
            else:
                last_at = datetime.datetime.now(datetime.timezone.utc)
            dur = max(0, int((last_at - first_at).total_seconds()))
            if dur < 60:
                total_duration = f'{dur}秒'
            elif dur < 3600:
                total_duration = f'{dur // 60}分{dur % 60}秒'
            elif dur < 86400:
                h, rem = divmod(dur, 3600)
                total_duration = f'{h}小时{rem // 60}分'
            else:
                d, rem = divmod(dur, 86400)
                total_duration = f'{d}天{rem // 3600}小时'
        except Exception:
            pass

    result = {
        'ok': True,
        'taskId': task_id,
        'taskMeta': task_meta,
        'agentId': agent_id,
        'agentLabel': _STATE_LABELS.get(state, state),
        'lastActive': updated_at[:19].replace('T', ' ') if updated_at else None,
        'activity': activity,
        'activitySource': 'progress+session',
        'relatedAgents': sorted(list(related_agents)),
        'phaseDurations': phase_durations,
        'totalDuration': total_duration,
    }
    if todos_summary:
        result['todosSummary'] = todos_summary
    if has_resource_data:
        result['resourceSummary'] = {
            'totalTokens': total_tokens,
            'totalCost': round(total_cost, 4),
            'totalElapsedSec': total_elapsed,
        }
    return result


# 状态推进顺序（手动推进用）
_STATE_FLOW = {
    'Pending':  ('Taizi', '皇上', '太子', '待处理旨意转交太子分拣'),
    'Taizi':    ('Zhongshu', '太子', '中书省', '太子分拣完毕，转中书省起草'),
    'Zhongshu': ('Menxia', '中书省', '门下省', '中书省方案提交门下省审议'),
    'Menxia':   ('Assigned', '门下省', '尚书省', '门下省准奏，转尚书省派发'),
    'Assigned': ('Doing', '尚书省', '六部', '尚书省开始派发执行'),
    'Next':     ('Doing', '尚书省', '六部', '待执行任务开始执行'),
    'Doing':    ('Review', '六部', '尚书省', '各部完成，进入汇总'),
    'Review':   ('Done', '尚书省', '太子', '全流程完成，回奏太子转报皇上'),
}
_STATE_LABELS = {
    'Pending': '待处理', 'Taizi': '太子', 'Zhongshu': '中书省', 'Menxia': '门下省',
    'Assigned': '尚书省', 'Next': '待执行', 'Doing': '执行中', 'Review': '审查', 'Done': '完成',
}


def dispatch_for_state(task_id, task, new_state, trigger='state-transition'):
    """推进/审批后自动派发对应 Agent（后台异步，不阻塞响应）。"""
    agent_id = _STATE_AGENT_MAP.get(new_state)
    if agent_id is None and new_state in ('Doing', 'Next'):
        org = task.get('org', '')
        agent_id = _ORG_AGENT_MAP.get(org)
    if not agent_id:
        log.info(f'ℹ️ {task_id} 新状态 {new_state} 无对应 Agent，跳过自动派发')
        return

    queued = {'ok': False, 'reason': ''}
    def _mark_dispatch_queued(t, s):
        last_agent = s.get('lastDispatchAgent')
        last_state = s.get('lastDispatchState')
        last_status = s.get('lastDispatchStatus')
        last_at = _parse_iso(s.get('lastDispatchAt'))
        now_dt = datetime.datetime.now(datetime.timezone.utc)
        dedup = False
        if last_agent == agent_id and last_state == new_state and last_status in ('queued', 'success'):
            if last_at:
                elapsed = max(0, int((now_dt - last_at).total_seconds()))
                dedup = elapsed < _DISPATCH_DEDUP_SECONDS
            else:
                dedup = True
        if dedup:
            queued['reason'] = f'dedup(last={last_status}, trigger={s.get("lastDispatchTrigger", "")})'
            return

        s.update({
            'lastDispatchAt': now_iso(),
            'lastDispatchStatus': 'queued',
            'lastDispatchAgent': agent_id,
            'lastDispatchState': new_state,
            'lastDispatchTrigger': trigger,
        })
        _scheduler_add_flow(
            t,
            f'已入队派发：{new_state} → {agent_id}（{trigger}）',
            to=_STATE_LABELS.get(new_state, new_state),
        )
        queued['ok'] = True

    if not _update_task_scheduler(task_id, _mark_dispatch_queued):
        log.warning(f'⚠️ {task_id} 任务不存在，跳过自动派发')
        return
    if not queued['ok']:
        log.info(f'⏭️ {task_id} 跳过重复派发 {new_state}->{agent_id}: {queued["reason"]}')
        return

    title = task.get('title', '(无标题)')
    target_dept = task.get('targetDept', '')

    # 根据 agent_id 构造针对性消息
    _msgs = {
        'taizi': (
            f'📜 皇上旨意需要你处理\n'
            f'任务ID: {task_id}\n'
            f'旨意: {title}\n'
            f'⚠️ 看板已有此任务，请勿重复创建。直接用 kanban_update.py 更新状态。\n'
            f'请立即转交中书省起草执行方案。'
        ),
        'zhongshu': (
            f'📜 旨意已到中书省，请起草方案\n'
            f'任务ID: {task_id}\n'
            f'旨意: {title}\n'
            f'⚠️ 看板已有此任务记录，请勿重复创建。直接用 kanban_update.py state 更新状态。\n'
            f'请立即起草执行方案，走完完整三省流程（中书起草→门下审议→尚书派发→六部执行）。'
        ),
        'menxia': (
            f'📋 中书省方案提交审议\n'
            f'任务ID: {task_id}\n'
            f'旨意: {title}\n'
            f'⚠️ 看板已有此任务，请勿重复创建。\n'
            f'请审议中书省方案，给出准奏或封驳意见。'
        ),
        'shangshu': (
            f'📮 门下省已准奏，请派发执行\n'
            f'任务ID: {task_id}\n'
            f'旨意: {title}\n'
            f'{"建议派发部门: " + target_dept if target_dept else ""}\n'
            f'⚠️ 看板已有此任务，请勿重复创建。\n'
            f'请分析方案并派发给六部执行。'
        ),
    }
    msg = _msgs.get(agent_id, (
        f'📌 请处理任务\n'
        f'任务ID: {task_id}\n'
        f'旨意: {title}\n'
        f'⚠️ 看板已有此任务，请勿重复创建。直接用 kanban_update.py 更新状态。'
    ))

    def _do_dispatch():
        try:
            if not _check_gateway_alive():
                log.warning(f'⚠️ {task_id} 自动派发跳过: Gateway 未启动')
                _update_task_scheduler(task_id, lambda t, s: s.update({
                    'lastDispatchAt': now_iso(),
                    'lastDispatchStatus': 'gateway-offline',
                    'lastDispatchAgent': agent_id,
                    'lastDispatchTrigger': trigger,
                }))
                return
            # Fix #139/#182: dispatch channel 可配置；未配置时不传 --deliver 避免
            # "unknown channel: feishu" 错误（非飞书用户）
            _agent_cfg = read_json(DATA / 'agent_config.json', {})
            _channel = (_agent_cfg.get('dispatchChannel') or '').strip()
            cmd = ['openclaw', 'agent', '--agent', agent_id, '-m', msg, '--timeout', '300']
            if _channel:
                cmd.extend(['--deliver', '--channel', _channel])
            max_retries = 2
            err = ''
            for attempt in range(1, max_retries + 1):
                log.info(f'🔄 自动派发 {task_id} → {agent_id} (第{attempt}次)...')
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=310)
                if result.returncode == 0:
                    log.info(f'✅ {task_id} 自动派发成功 → {agent_id}')
                    _update_task_scheduler(task_id, lambda t, s: (
                        s.update({
                            'lastDispatchAt': now_iso(),
                            'lastDispatchStatus': 'success',
                            'lastDispatchAgent': agent_id,
                            'lastDispatchState': new_state,
                            'lastDispatchTrigger': trigger,
                            'lastDispatchError': '',
                        }),
                        _scheduler_add_flow(t, f'派发成功：{agent_id}（{trigger}）', to=t.get('org', ''))
                    ))
                    return
                err = result.stderr[:200] if result.stderr else result.stdout[:200]
                log.warning(f'⚠️ {task_id} 自动派发失败(第{attempt}次): {err}')
                if attempt < max_retries:
                    import time
                    time.sleep(5)
            log.error(f'❌ {task_id} 自动派发最终失败 → {agent_id}')
            _update_task_scheduler(task_id, lambda t, s: (
                s.update({
                    'lastDispatchAt': now_iso(),
                    'lastDispatchStatus': 'failed',
                    'lastDispatchAgent': agent_id,
                    'lastDispatchState': new_state,
                    'lastDispatchTrigger': trigger,
                    'lastDispatchError': err,
                }),
                _scheduler_add_flow(t, f'派发失败：{agent_id}（{trigger}）', to=t.get('org', ''))
            ))
        except subprocess.TimeoutExpired:
            log.error(f'❌ {task_id} 自动派发超时 → {agent_id}')
            _update_task_scheduler(task_id, lambda t, s: (
                s.update({
                    'lastDispatchAt': now_iso(),
                    'lastDispatchStatus': 'timeout',
                    'lastDispatchAgent': agent_id,
                    'lastDispatchState': new_state,
                    'lastDispatchTrigger': trigger,
                    'lastDispatchError': 'timeout',
                }),
                _scheduler_add_flow(t, f'派发超时：{agent_id}（{trigger}）', to=t.get('org', ''))
            ))
        except Exception as e:
            log.warning(f'⚠️ {task_id} 自动派发异常: {e}')
            _update_task_scheduler(task_id, lambda t, s: (
                s.update({
                    'lastDispatchAt': now_iso(),
                    'lastDispatchStatus': 'error',
                    'lastDispatchAgent': agent_id,
                    'lastDispatchState': new_state,
                    'lastDispatchTrigger': trigger,
                    'lastDispatchError': str(e)[:200],
                }),
                _scheduler_add_flow(t, f'派发异常：{agent_id}（{trigger}）', to=t.get('org', ''))
            ))

    threading.Thread(target=_do_dispatch, daemon=True).start()
    log.info(f'🚀 {task_id} 推进后自动派发 → {agent_id}')


def handle_advance_state(task_id, comment=''):
    """手动推进任务到下一阶段（解卡用），推进后自动派发对应 Agent。"""
    tasks = load_tasks()
    task = next((t for t in tasks if t.get('id') == task_id), None)
    if not task:
        return {'ok': False, 'error': f'任务 {task_id} 不存在'}
    cur = task.get('state', '')
    if cur not in _STATE_FLOW:
        return {'ok': False, 'error': f'任务 {task_id} 状态为 {cur}，无法推进'}
    _ensure_scheduler(task)
    _scheduler_snapshot(task, f'advance-before-{cur}')
    next_state, from_dept, to_dept, default_remark = _STATE_FLOW[cur]
    remark = comment or default_remark

    task['state'] = next_state
    task['now'] = f'⬇️ 手动推进：{remark}'
    task.setdefault('flow_log', []).append({
        'at': now_iso(),
        'from': from_dept,
        'to': to_dept,
        'remark': f'⬇️ 手动推进：{remark}'
    })
    _scheduler_mark_progress(task, f'手动推进 {cur} -> {next_state}')
    task['updatedAt'] = now_iso()
    save_tasks(tasks)

    # 🚀 推进后自动派发对应 Agent（Done 状态无需派发）
    if next_state != 'Done':
        dispatch_for_state(task_id, task, next_state)

    from_label = _STATE_LABELS.get(cur, cur)
    to_label = _STATE_LABELS.get(next_state, next_state)
    dispatched = ' (已自动派发 Agent)' if next_state != 'Done' else ''
    return {'ok': True, 'message': f'{task_id} {from_label} → {to_label}{dispatched}'}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        # 只记录 4xx/5xx 错误请求
        if args and len(args) >= 1:
            status = str(args[0]) if args else ''
            if status.startswith('4') or status.startswith('5'):
                log.warning(f'{self.client_address[0]} {fmt % args}')

    def handle_error(self):
        pass  # 静默处理连接错误，避免 BrokenPipe 崩溃

    def handle(self):
        try:
            super().handle()
        except (BrokenPipeError, ConnectionResetError):
            pass  # 客户端断开连接，忽略

    def do_OPTIONS(self):
        self.send_response(200)
        cors_headers(self)
        self.end_headers()

    def send_json(self, data, code=200):
        try:
            body = json.dumps(data, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            cors_headers(self)
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def send_file(self, path: pathlib.Path, mime='text/html; charset=utf-8'):
        if not path.exists():
            self.send_error(404)
            return
        try:
            body = path.read_bytes()
            self.send_response(200)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(body)))
            cors_headers(self)
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _serve_static(self, rel_path):
        """从 dist/ 目录提供静态文件。"""
        safe = rel_path.replace('\\', '/').lstrip('/')
        if '..' in safe:
            self.send_error(403)
            return True
        fp = DIST / safe
        if fp.is_file():
            mime = _MIME_TYPES.get(fp.suffix.lower(), 'application/octet-stream')
            self.send_file(fp, mime)
            return True
        return False

    def do_GET(self):
        parsed = urlparse(self.path)
        p = parsed.path.rstrip('/')
        q = parse_qs(parsed.query)
        if p in ('', '/dashboard', '/dashboard.html'):
            # 优先返回传统看板（包含省部调度扩展）；不存在时再回退到 React 构建页。
            if LEGACY_DASHBOARD_HTML.exists():
                self.send_file(LEGACY_DASHBOARD_HTML)
            else:
                self.send_file(DIST / 'index.html')
        elif p == '/healthz':
            task_data_dir = get_task_data_dir()
            checks = {'dataDir': task_data_dir.is_dir(), 'tasksReadable': (task_data_dir / 'tasks_source.json').exists()}
            checks['dataWritable'] = os.access(str(task_data_dir), os.W_OK)
            all_ok = all(checks.values())
            self.send_json({'status': 'ok' if all_ok else 'degraded', 'ts': now_iso(), 'checks': checks})
        elif p == '/api/live-status':
            task_data_dir = get_task_data_dir()
            self.send_json(read_json(task_data_dir / 'live_status.json'))
        elif p == '/api/agent-config':
            self.send_json(read_json(DATA / 'agent_config.json'))
        elif p == '/api/model-change-log':
            self.send_json(read_json(DATA / 'model_change_log.json', []))
        elif p == '/api/last-result':
            self.send_json(read_json(DATA / 'last_model_change_result.json', {}))
        elif p == '/api/officials-stats':
            self.send_json(read_json(DATA / 'officials_stats.json', {}))
        elif p == '/api/agent-work-scopes':
            data = _load_agent_work_scopes()
            self.send_json({'ok': True, 'scopes': data.get('scopes', {})})
        elif p == '/api/agent-work-bindings':
            data = _load_agent_work_bindings()
            self.send_json({'ok': True, 'bindings': data.get('bindings', {})})
        elif p.startswith('/api/agent-work-scopes/'):
            agent_id = p.replace('/api/agent-work-scopes/', '').strip()
            if not agent_id or not _SAFE_NAME_RE.match(agent_id):
                self.send_json({'ok': False, 'error': 'invalid agent_id'}, 400)
            else:
                data = _load_agent_work_scopes()
                self.send_json({
                    'ok': True,
                    'agentId': agent_id,
                    'scopes': data.get('scopes', {}).get(agent_id, []),
                })
        elif p == '/api/morning-brief':
            self.send_json(read_json(DATA / 'morning_brief.json', {}))
        elif p == '/api/morning-config':
            migrate_notification_config()
            self.send_json(read_json(DATA / 'morning_brief_config.json', {
                'categories': [
                    {'name': '政治', 'enabled': True},
                    {'name': '军事', 'enabled': True},
                    {'name': '经济', 'enabled': True},
                    {'name': 'AI大模型', 'enabled': True},
                ],
                'keywords': [], 'custom_feeds': [],
                'notification': {'enabled': True, 'channel': 'feishu', 'webhook': ''},
            }))
        elif p == '/api/learning-plan':
            self.send_json(list_learning_plans())
        elif p == '/api/pm/projects':
            self.send_json(pm_list_projects())
        elif p == '/api/automation/tasks':
            self.send_json(automation_list_tasks())
        elif p == '/api/jzg/projects':
            self.send_json(jzg_list_projects())
        elif p.startswith('/api/learning-plan/'):
            plan_id = p.replace('/api/learning-plan/', '').strip()
            if not plan_id:
                self.send_json({'ok': False, 'error': 'plan_id required'}, 400)
            else:
                self.send_json(get_learning_plan(plan_id))
        elif p == '/api/notification-channels':
            self.send_json({'ok': True, 'channels': get_channel_info()})
        elif p.startswith('/api/morning-brief/'):
            date = p.split('/')[-1]
            # 标准化日期格式为 YYYYMMDD（兼容 YYYY-MM-DD 输入）
            date_clean = date.replace('-', '')
            if not date_clean.isdigit() or len(date_clean) != 8:
                self.send_json({'ok': False, 'error': f'日期格式无效: {date}，请使用 YYYYMMDD'}, 400)
                return
            self.send_json(read_json(DATA / f'morning_brief_{date_clean}.json', {}))
        elif p == '/api/remote-skills-list':
            self.send_json(get_remote_skills_list())
        elif p.startswith('/api/skill-content/'):
            # /api/skill-content/{agentId}/{skillName}
            parts = p.replace('/api/skill-content/', '').split('/', 1)
            if len(parts) == 2:
                self.send_json(read_skill_content(parts[0], parts[1]))
            else:
                self.send_json({'ok': False, 'error': 'Usage: /api/skill-content/{agentId}/{skillName}'}, 400)
        elif p.startswith('/api/agent-soul/'):
            agent_id = p.replace('/api/agent-soul/', '')
            if not agent_id or not _SAFE_NAME_RE.match(agent_id):
                self.send_json({'ok': False, 'error': 'invalid agent_id'}, 400)
            else:
                self.send_json(read_agent_soul(agent_id))
        elif p.startswith('/api/task-activity/'):
            task_id = p.replace('/api/task-activity/', '')
            if not task_id:
                self.send_json({'ok': False, 'error': 'task_id required'}, 400)
            else:
                self.send_json(get_task_activity(task_id))
        elif p.startswith('/api/scheduler-state/'):
            task_id = p.replace('/api/scheduler-state/', '')
            if not task_id:
                self.send_json({'ok': False, 'error': 'task_id required'}, 400)
            else:
                self.send_json(get_scheduler_state(task_id))
        elif p == '/api/agents-status':
            self.send_json(get_agents_status())
        elif p.startswith('/api/task-output/'):
            task_id = p.replace('/api/task-output/', '')
            if not task_id or not _SAFE_NAME_RE.match(task_id):
                self.send_json({'ok': False, 'error': 'invalid task_id'}, 400)
            else:
                tasks = load_tasks()
                task = next((t for t in tasks if t.get('id') == task_id), None)
                if not task:
                    self.send_json({'ok': False, 'error': 'task not found'}, 404)
                else:
                    output_path = task.get('output', '')
                    if not output_path or output_path == '-':
                        self.send_json({'ok': True, 'taskId': task_id, 'content': '', 'exists': False})
                    else:
                        p_out = pathlib.Path(output_path)
                        if not p_out.exists():
                            self.send_json({'ok': True, 'taskId': task_id, 'content': '', 'exists': False})
                        else:
                            try:
                                content = p_out.read_text(encoding='utf-8', errors='replace')[:50000]
                                self.send_json({'ok': True, 'taskId': task_id, 'content': content, 'exists': True})
                            except Exception as e:
                                self.send_json({'ok': False, 'error': f'读取失败: {e}'}, 500)
        elif p.startswith('/api/agent-activity/'):
            agent_id = p.replace('/api/agent-activity/', '')
            if not agent_id or not _SAFE_NAME_RE.match(agent_id):
                self.send_json({'ok': False, 'error': 'invalid agent_id'}, 400)
            else:
                self.send_json({'ok': True, 'agentId': agent_id, 'activity': get_agent_activity(agent_id)})
        elif p.startswith('/api/agent-sessions/'):
            agent_id = p.replace('/api/agent-sessions/', '')
            if not agent_id or not _SAFE_NAME_RE.match(agent_id):
                self.send_json({'ok': False, 'error': 'invalid agent_id'}, 400)
            else:
                self.send_json(get_agent_sessions(agent_id))
        elif p == '/api/agent-session-log':
            agent_id = (q.get('agentId', [''])[0] or '').strip()
            session_id = (q.get('sessionId', [''])[0] or '').strip()
            limit = (q.get('limit', ['120'])[0] or '120').strip()
            if not agent_id or not _SAFE_NAME_RE.match(agent_id):
                self.send_json({'ok': False, 'error': 'invalid agent_id'}, 400)
            else:
                self.send_json(get_agent_session_log(agent_id, session_id, limit=limit))
        # ── 朝堂议政 ──
        elif p == '/api/court-discuss/list':
            self.send_json({'ok': True, 'sessions': cd_list()})
        elif p == '/api/court-discuss/officials':
            self.send_json({'ok': True, 'officials': CD_PROFILES})
        elif p.startswith('/api/court-discuss/session/'):
            sid = p.replace('/api/court-discuss/session/', '')
            data = cd_get(sid)
            self.send_json(data if data else {'ok': False, 'error': 'session not found'}, 200 if data else 404)
        elif p == '/api/court-discuss/fate':
            self.send_json({'ok': True, 'event': cd_fate()})
        elif self._serve_static(p):
            pass  # 已由 _serve_static 处理 (JS/CSS/图片等)
        else:
            # SPA fallback：非 /api/ 路径返回 dashboard.html（若不存在则回退 index.html）
            if not p.startswith('/api/'):
                if LEGACY_DASHBOARD_HTML.exists():
                    self.send_file(LEGACY_DASHBOARD_HTML)
                    return
                idx = DIST / 'index.html'
                if idx.exists():
                    self.send_file(idx)
                    return
            self.send_error(404)

    def do_POST(self):
        p = urlparse(self.path).path.rstrip('/')
        length = int(self.headers.get('Content-Length', 0))
        if length > MAX_REQUEST_BODY:
            self.send_json({'ok': False, 'error': f'Request body too large (max {MAX_REQUEST_BODY} bytes)'}, 413)
            return
        raw = self.rfile.read(length) if length else b''
        try:
            body = json.loads(raw) if raw else {}
        except Exception:
            self.send_json({'ok': False, 'error': 'invalid JSON'}, 400)
            return

        if p == '/api/morning-config':
            if not isinstance(body, dict):
                self.send_json({'ok': False, 'error': '请求体必须是 JSON 对象'}, 400)
                return
            allowed_keys = {'categories', 'keywords', 'custom_feeds', 'notification', 'feishu_webhook'}
            unknown = set(body.keys()) - allowed_keys
            if unknown:
                self.send_json({'ok': False, 'error': f'未知字段: {", ".join(unknown)}'}, 400)
                return
            if 'categories' in body and not isinstance(body['categories'], list):
                self.send_json({'ok': False, 'error': 'categories 必须是数组'}, 400)
                return
            if 'keywords' in body and not isinstance(body['keywords'], list):
                self.send_json({'ok': False, 'error': 'keywords 必须是数组'}, 400)
                return
            if 'notification' in body:
                noti = body['notification']
                if not isinstance(noti, dict):
                    self.send_json({'ok': False, 'error': 'notification 必须是对象'}, 400)
                    return
                channel_type = noti.get('channel', 'feishu')
                if channel_type not in NOTIFICATION_CHANNELS:
                    self.send_json({'ok': False, 'error': f'不支持的渠道: {channel_type}'}, 400)
                    return
                webhook = noti.get('webhook', '').strip()
                if webhook:
                    channel_cls = get_channel(channel_type)
                    if channel_cls and not channel_cls.validate_webhook(webhook):
                        self.send_json({'ok': False, 'error': f'{channel_cls.label} Webhook URL 无效'}, 400)
                        return
            webhook_legacy = body.get('feishu_webhook', '').strip()
            if webhook_legacy and 'notification' not in body:
                body['notification'] = {'enabled': True, 'channel': 'feishu', 'webhook': webhook_legacy}
            cfg_path = DATA / 'morning_brief_config.json'
            cfg_path.write_text(json.dumps(body, ensure_ascii=False, indent=2))
            self.send_json({'ok': True, 'message': '订阅配置已保存'})
            return

        if p == '/api/learning-plan/start':
            topic = body.get('topic', '').strip()
            if not topic:
                self.send_json({'ok': False, 'error': 'topic required'}, 400)
                return
            self.send_json(start_learning_plan(topic))
            return

        if p == '/api/agent-work-scopes/update':
            agent_id = str(body.get('agentId', '')).strip()
            scopes = body.get('scopes', [])
            if not agent_id or not _SAFE_NAME_RE.match(agent_id):
                self.send_json({'ok': False, 'error': 'invalid agentId'}, 400)
                return
            current = _load_agent_work_scopes().get('scopes', {})
            normalized = _normalize_work_scope_items(scopes)
            if not normalized:
                current.pop(agent_id, None)
            else:
                current[agent_id] = normalized
            _save_agent_work_scopes(current)
            self.send_json({'ok': True, 'agentId': agent_id, 'scopes': current.get(agent_id, [])})
            return

        if p == '/api/pm/project-create':
            name = body.get('name', '').strip()
            description = body.get('description', '').strip()
            if not name:
                self.send_json({'ok': False, 'error': 'name required'}, 400)
                return
            self.send_json(pm_create_project(name, description))
            return

        if p == '/api/jzg/project-create':
            name = body.get('name', '').strip()
            description = body.get('description', '').strip()
            if not name:
                self.send_json({'ok': False, 'error': 'name required'}, 400)
                return
            self.send_json(jzg_create_project(name, description))
            return

        if p == '/api/automation/parse-request':
            text = body.get('text', '')
            self.send_json(automation_parse_request(text))
            return

        if p == '/api/automation/task-create':
            self.send_json(automation_create_task(
                body.get('title', ''),
                body.get('requestText', ''),
                schedule_expr=body.get('scheduleExpr', ''),
                target_agent=body.get('targetAgent', 'shangshu'),
                target_session=body.get('targetSession', ''),
                prompt=body.get('prompt', ''),
            ))
            return

        if p == '/api/automation/task-update':
            task_id = body.get('taskId', '').strip()
            if not task_id:
                self.send_json({'ok': False, 'error': 'taskId required'}, 400)
                return
            self.send_json(automation_update_task(task_id, body))
            return

        if p == '/api/automation/task-delete':
            task_id = body.get('taskId', '').strip()
            if not task_id:
                self.send_json({'ok': False, 'error': 'taskId required'}, 400)
                return
            self.send_json(automation_delete_task(task_id))
            return

        if p == '/api/automation/task-run':
            task_id = body.get('taskId', '').strip()
            if not task_id:
                self.send_json({'ok': False, 'error': 'taskId required'}, 400)
                return
            self.send_json(automation_run_task(
                task_id,
                status_feedback=body.get('statusFeedback', ''),
                experience_feedback=body.get('experienceFeedback', ''),
            ))
            return

        if p == '/api/jzg/followup-create':
            project_id = body.get('projectId', '').strip()
            title = body.get('title', '').strip()
            if not project_id or not title:
                self.send_json({'ok': False, 'error': 'projectId and title required'}, 400)
                return
            self.send_json(jzg_add_followup(project_id, title))
            return

        if p == '/api/jzg/followup-toggle':
            project_id = body.get('projectId', '').strip()
            item_id = body.get('itemId', '').strip()
            if not project_id or not item_id:
                self.send_json({'ok': False, 'error': 'projectId and itemId required'}, 400)
                return
            self.send_json(jzg_toggle_followup(project_id, item_id, body.get('status', 'todo')))
            return

        if p == '/api/jzg/followup-update':
            project_id = body.get('projectId', '').strip()
            item_id = body.get('itemId', '').strip()
            if not project_id or not item_id:
                self.send_json({'ok': False, 'error': 'projectId and itemId required'}, 400)
                return
            self.send_json(jzg_update_followup(
                project_id,
                item_id,
                title=body.get('title', None),
                description=body.get('description', None),
                memo=body.get('memo', None),
                priority=body.get('priority', None),
                category=body.get('category', None),
                due_date=body.get('dueDate', None),
                status=body.get('status', None),
            ))
            return

        if p == '/api/jzg/followup-delete':
            project_id = body.get('projectId', '').strip()
            item_id = body.get('itemId', '').strip()
            if not project_id or not item_id:
                self.send_json({'ok': False, 'error': 'projectId and itemId required'}, 400)
                return
            self.send_json(jzg_delete_followup(project_id, item_id))
            return

        if p == '/api/jzg/followup-note':
            project_id = body.get('projectId', '').strip()
            text = body.get('text', '').strip()
            if not project_id or not text:
                self.send_json({'ok': False, 'error': 'projectId and text required'}, 400)
                return
            self.send_json(jzg_add_daily_note(project_id, text))
            return

        if p == '/api/jzg/daily-report-archive':
            project_id = body.get('projectId', '').strip()
            date = body.get('date', '').strip()
            report = body.get('report', '').strip()
            if not project_id:
                self.send_json({'ok': False, 'error': 'projectId required'}, 400)
                return
            self.send_json(jzg_archive_daily_report(project_id, date, report))
            return

        if p == '/api/jzg/daily-report-update':
            project_id = body.get('projectId', '').strip()
            record_id = body.get('recordId', '').strip()
            report = body.get('report', '').strip()
            if not project_id or not record_id:
                self.send_json({'ok': False, 'error': 'projectId and recordId required'}, 400)
                return
            self.send_json(jzg_update_daily_report(project_id, record_id, report, date=body.get('date', None)))
            return

        if p == '/api/jzg/report-template-update':
            project_id = body.get('projectId', '').strip()
            mode = body.get('mode', '').strip()
            if not project_id or not mode:
                self.send_json({'ok': False, 'error': 'projectId and mode required'}, 400)
                return
            self.send_json(jzg_update_report_template(project_id, mode, body.get('template', '')))
            return

        if p == '/api/jzg/report-template-generate':
            project_id = body.get('projectId', '').strip()
            mode = body.get('mode', '').strip()
            requirement = body.get('requirement', '').strip()
            if not project_id or not mode:
                self.send_json({'ok': False, 'error': 'projectId and mode required'}, 400)
                return
            self.send_json(jzg_generate_report_template(
                project_id,
                mode=mode,
                requirement=requirement,
                current_template=body.get('currentTemplate', ''),
            ))
            return

        if p == '/api/jzg/followup-report-generate':
            project_id = body.get('projectId', '').strip()
            mode = body.get('mode', 'daily').strip()
            if not project_id:
                self.send_json({'ok': False, 'error': 'projectId required'}, 400)
                return
            self.send_json(jzg_generate_followup_report(
                project_id,
                mode=mode,
                date=body.get('date', ''),
                start_date=body.get('startDate', ''),
                end_date=body.get('endDate', ''),
                template=body.get('template', ''),
            ))
            return

        if p == '/api/jzg/plan-update':
            project_id = body.get('projectId', '').strip()
            rows = body.get('rows', [])
            if not project_id:
                self.send_json({'ok': False, 'error': 'projectId required'}, 400)
                return
            self.send_json(jzg_update_plan(project_id, rows))
            return

        if p == '/api/jzg/strategy-topic-create':
            project_id = body.get('projectId', '').strip()
            title = body.get('title', '').strip()
            if not project_id or not title:
                self.send_json({'ok': False, 'error': 'projectId and title required'}, 400)
                return
            self.send_json(jzg_create_strategy_topic(project_id, title, body.get('context', '')))
            return

        if p == '/api/jzg/strategy-message':
            project_id = body.get('projectId', '').strip()
            topic_id = body.get('topicId', '').strip()
            message = body.get('message', '').strip()
            if not project_id or not topic_id or not message:
                self.send_json({'ok': False, 'error': 'projectId, topicId and message required'}, 400)
                return
            self.send_json(jzg_add_strategy_message(project_id, topic_id, message, body.get('role', 'user')))
            return

        if p == '/api/jzg/doc-folder-create':
            project_id = body.get('projectId', '').strip()
            name = body.get('name', '').strip()
            if not project_id or not name:
                self.send_json({'ok': False, 'error': 'projectId and name required'}, 400)
                return
            self.send_json(jzg_doc_folder_create(project_id, name))
            return

        if p == '/api/jzg/doc-folder-update':
            project_id = body.get('projectId', '').strip()
            folder_id = body.get('folderId', '').strip()
            name = body.get('name', '').strip()
            if not project_id or not folder_id or not name:
                self.send_json({'ok': False, 'error': 'projectId, folderId and name required'}, 400)
                return
            self.send_json(jzg_doc_folder_update(project_id, folder_id, name))
            return

        if p == '/api/jzg/doc-folder-delete':
            project_id = body.get('projectId', '').strip()
            folder_id = body.get('folderId', '').strip()
            if not project_id or not folder_id:
                self.send_json({'ok': False, 'error': 'projectId and folderId required'}, 400)
                return
            self.send_json(jzg_doc_folder_delete(project_id, folder_id))
            return

        if p == '/api/jzg/doc-folder-reorder':
            project_id = body.get('projectId', '').strip()
            source_folder_id = body.get('sourceFolderId', '').strip()
            target_folder_id = body.get('targetFolderId', '').strip()
            if not project_id or not source_folder_id or not target_folder_id:
                self.send_json({'ok': False, 'error': 'projectId, sourceFolderId and targetFolderId required'}, 400)
                return
            self.send_json(jzg_doc_folder_reorder(
                project_id,
                source_folder_id,
                target_folder_id,
                place=body.get('place', 'before'),
            ))
            return

        if p == '/api/jzg/doc-create':
            project_id = body.get('projectId', '').strip()
            name = body.get('name', '').strip()
            if not project_id or not name:
                self.send_json({'ok': False, 'error': 'projectId and name required'}, 400)
                return
            self.send_json(jzg_doc_create(
                project_id,
                name,
                folder_id=body.get('folderId', None),
                content=body.get('content', ''),
                size=body.get('size', 0),
                ext=body.get('ext', ''),
                file_base64=body.get('fileBase64', ''),
            ))
            return

        if p == '/api/jzg/doc-update':
            project_id = body.get('projectId', '').strip()
            doc_id = body.get('docId', '').strip()
            if not project_id or not doc_id:
                self.send_json({'ok': False, 'error': 'projectId and docId required'}, 400)
                return
            self.send_json(jzg_doc_update(
                project_id,
                doc_id,
                name=body.get('name', None),
                folder_id=body.get('folderId', None),
                content=body.get('content', None),
                summary=body.get('summary', None),
                tags=body.get('tags', None),
                size=body.get('size', None),
                ext=body.get('ext', None),
            ))
            return

        if p == '/api/jzg/doc-delete':
            project_id = body.get('projectId', '').strip()
            doc_id = body.get('docId', '').strip()
            if not project_id or not doc_id:
                self.send_json({'ok': False, 'error': 'projectId and docId required'}, 400)
                return
            self.send_json(jzg_doc_delete(project_id, doc_id))
            return

        if p == '/api/jzg/doc-analyze':
            project_id = body.get('projectId', '').strip()
            doc_id = body.get('docId', '').strip()
            if not project_id or not doc_id:
                self.send_json({'ok': False, 'error': 'projectId and docId required'}, 400)
                return
            self.send_json(jzg_doc_analyze(project_id, doc_id))
            return

        if p == '/api/jzg/reminder-create':
            project_id = body.get('projectId', '').strip()
            title = body.get('title', '').strip()
            if not project_id or not title:
                self.send_json({'ok': False, 'error': 'projectId and title required'}, 400)
                return
            self.send_json(jzg_add_reminder(project_id, title, body.get('schedule', '')))
            return

        if p == '/api/jzg/reminder-toggle':
            project_id = body.get('projectId', '').strip()
            reminder_id = body.get('reminderId', '').strip()
            if not project_id or not reminder_id:
                self.send_json({'ok': False, 'error': 'projectId and reminderId required'}, 400)
                return
            self.send_json(jzg_toggle_reminder(project_id, reminder_id, bool(body.get('enabled', True))))
            return

        if p == '/api/pm/project-update':
            project_id = body.get('projectId', '').strip()
            if not project_id:
                self.send_json({'ok': False, 'error': 'projectId required'}, 400)
                return
            self.send_json(pm_update_project(
                project_id,
                name=body.get('name', None),
                description=body.get('description', None),
            ))
            return

        if p == '/api/pm/project-delete':
            project_id = body.get('projectId', '').strip()
            if not project_id:
                self.send_json({'ok': False, 'error': 'projectId required'}, 400)
                return
            self.send_json(pm_delete_project(project_id))
            return

        if p == '/api/pm/item-create':
            project_id = body.get('projectId', '').strip()
            title = body.get('title', '').strip()
            item_type = body.get('type', 'bug').strip()
            priority = body.get('priority', 'P2').strip()
            description = body.get('description', '').strip()
            folder_id = body.get('folderId', '').strip()
            if not project_id or not title:
                self.send_json({'ok': False, 'error': 'projectId and title required'}, 400)
                return
            ret = pm_create_item(project_id, title, item_type, priority, description)
            if ret.get('ok') and folder_id:
                ret = pm_update_item(project_id, ret.get('item', {}).get('id', ''), folder_id=folder_id)
            self.send_json(ret)
            return

        if p == '/api/pm/item-update':
            project_id = body.get('projectId', '').strip()
            item_id = body.get('itemId', '').strip()
            if not project_id or not item_id:
                self.send_json({'ok': False, 'error': 'projectId and itemId required'}, 400)
                return
            self.send_json(pm_update_item(
                project_id,
                item_id,
                status=body.get('status', None),
                priority=body.get('priority', None),
                resolution=body.get('resolution', None),
                item_type=body.get('type', None),
                description=body.get('description', None),
                title=body.get('title', None),
                folder_id=body.get('folderId', None),
                questions=body.get('questions', None),
                clarify_replies=body.get('clarifyReplies', None),
                plan=body.get('plan', None),
                review_suggested_title=body.get('reviewSuggestedTitle', None),
                review_suggested_description=body.get('reviewSuggestedDescription', None),
                review_suggested_by=body.get('reviewSuggestedBy', None),
            ))
            return

        if p == '/api/pm/folder-create':
            project_id = body.get('projectId', '').strip()
            name = body.get('name', '').strip()
            if not project_id or not name:
                self.send_json({'ok': False, 'error': 'projectId and name required'}, 400)
                return
            self.send_json(pm_create_folder(project_id, name))
            return

        if p == '/api/pm/folder-update':
            project_id = body.get('projectId', '').strip()
            folder_id = body.get('folderId', '').strip()
            name = body.get('name', '').strip()
            if not project_id or not folder_id or not name:
                self.send_json({'ok': False, 'error': 'projectId, folderId and name required'}, 400)
                return
            self.send_json(pm_update_folder(project_id, folder_id, name))
            return

        if p == '/api/pm/folder-delete':
            project_id = body.get('projectId', '').strip()
            folder_id = body.get('folderId', '').strip()
            if not project_id or not folder_id:
                self.send_json({'ok': False, 'error': 'projectId and folderId required'}, 400)
                return
            self.send_json(pm_delete_folder(project_id, folder_id))
            return

        if p == '/api/pm/folder-reorder':
            project_id = body.get('projectId', '').strip()
            source_folder_id = body.get('sourceFolderId', '').strip()
            target_folder_id = body.get('targetFolderId', '').strip()
            if not project_id or not source_folder_id or not target_folder_id:
                self.send_json({'ok': False, 'error': 'projectId, sourceFolderId and targetFolderId required'}, 400)
                return
            self.send_json(pm_reorder_folder(
                project_id,
                source_folder_id,
                target_folder_id,
                place=body.get('place', 'before'),
            ))
            return

        if p == '/api/pm/design-update':
            project_id = body.get('projectId', '').strip()
            section = body.get('section', '').strip()
            if not project_id or not section:
                self.send_json({'ok': False, 'error': 'projectId and section required'}, 400)
                return
            self.send_json(pm_update_design(project_id, section, body.get('content', ''), updated_by=body.get('updatedBy', 'user')))
            return

        if p == '/api/pm/design-generate':
            project_id = body.get('projectId', '').strip()
            section = body.get('section', '').strip()
            if not project_id or not section:
                self.send_json({'ok': False, 'error': 'projectId and section required'}, 400)
                return
            self.send_json(pm_generate_design(project_id, section))
            return

        if p == '/api/pm/version-generate':
            project_id = body.get('projectId', '').strip()
            if not project_id:
                self.send_json({'ok': False, 'error': 'projectId required'}, 400)
                return
            self.send_json(pm_generate_version(project_id))
            return

        if p == '/api/pm/version-update':
            project_id = body.get('projectId', '').strip()
            version_id = body.get('versionId', '').strip()
            if not project_id or not version_id:
                self.send_json({'ok': False, 'error': 'projectId and versionId required'}, 400)
                return
            self.send_json(pm_update_version(
                project_id,
                version_id,
                version=body.get('githubVersion', body.get('version', None)),
                status=body.get('status', None),
            ))
            return

        if p == '/api/pm/design-suggestion-create':
            project_id = body.get('projectId', '').strip()
            section = body.get('section', '').strip()
            text = body.get('text', '').strip()
            if not project_id or not section or not text:
                self.send_json({'ok': False, 'error': 'projectId, section and text required'}, 400)
                return
            self.send_json(pm_create_design_suggestion(project_id, section, text))
            return

        if p == '/api/pm/design-suggestion-update':
            project_id = body.get('projectId', '').strip()
            section = body.get('section', '').strip()
            suggestion_id = body.get('suggestionId', '').strip()
            if not project_id or not section or not suggestion_id:
                self.send_json({'ok': False, 'error': 'projectId, section and suggestionId required'}, 400)
                return
            self.send_json(pm_update_design_suggestion(
                project_id,
                section,
                suggestion_id,
                text=body.get('text', None),
                status=body.get('status', None),
            ))
            return

        if p == '/api/pm/design-suggestion-delete':
            project_id = body.get('projectId', '').strip()
            section = body.get('section', '').strip()
            suggestion_id = body.get('suggestionId', '').strip()
            if not project_id or not section or not suggestion_id:
                self.send_json({'ok': False, 'error': 'projectId, section and suggestionId required'}, 400)
                return
            self.send_json(pm_delete_design_suggestion(project_id, section, suggestion_id))
            return

        if p == '/api/pm/item-delete':
            project_id = body.get('projectId', '').strip()
            item_id = body.get('itemId', '').strip()
            if not project_id or not item_id:
                self.send_json({'ok': False, 'error': 'projectId and itemId required'}, 400)
                return
            self.send_json(pm_delete_item(project_id, item_id))
            return

        if p == '/api/pm/item-reply':
            project_id = body.get('projectId', '').strip()
            item_id = body.get('itemId', '').strip()
            text = body.get('text', '').strip()
            if not project_id or not item_id or not text:
                self.send_json({'ok': False, 'error': 'projectId, itemId and text required'}, 400)
                return
            self.send_json(pm_add_reply(project_id, item_id, text, role=body.get('role', 'user')))
            return

        if p == '/api/pm/item-reply-delete':
            project_id = body.get('projectId', '').strip()
            item_id = body.get('itemId', '').strip()
            if not project_id or not item_id:
                self.send_json({'ok': False, 'error': 'projectId and itemId required'}, 400)
                return
            self.send_json(pm_delete_reply(project_id, item_id, body.get('replyIndex')))
            return

        if p == '/api/pm/rnd-review':
            project_id = body.get('projectId', '').strip()
            item_id = body.get('itemId', '').strip()
            mode = body.get('mode', 'review').strip()
            if not project_id or not item_id:
                self.send_json({'ok': False, 'error': 'projectId and itemId required'}, 400)
                return
            self.send_json(pm_rnd_review(project_id, item_id, mode=mode))
            return

        if p == '/api/learning-plan/answer':
            plan_id = body.get('planId', '').strip()
            answers = body.get('answers', [])
            if not plan_id:
                self.send_json({'ok': False, 'error': 'planId required'}, 400)
                return
            self.send_json(answer_learning_plan(plan_id, answers))
            return

        if p == '/api/learning-plan/topic-chat':
            plan_id = body.get('planId', '').strip()
            topic_id = body.get('topicId', '').strip()
            message = body.get('message', '').strip()
            if not plan_id or not topic_id:
                self.send_json({'ok': False, 'error': 'planId and topicId required'}, 400)
                return
            self.send_json(chat_learning_topic(plan_id, topic_id, message))
            return

        if p == '/api/learning-plan/topic-summarize':
            plan_id = body.get('planId', '').strip()
            topic_id = body.get('topicId', '').strip()
            if not plan_id or not topic_id:
                self.send_json({'ok': False, 'error': 'planId and topicId required'}, 400)
                return
            self.send_json(summarize_learning_topic(plan_id, topic_id))
            return

        if p == '/api/learning-plan/topic-delete':
            plan_id = body.get('planId', '').strip()
            topic_id = body.get('topicId', '').strip()
            if not plan_id or not topic_id:
                self.send_json({'ok': False, 'error': 'planId and topicId required'}, 400)
                return
            self.send_json(delete_learning_topic(plan_id, topic_id))
            return

        if p == '/api/learning-plan/delete':
            plan_id = body.get('planId', '').strip()
            if not plan_id:
                self.send_json({'ok': False, 'error': 'planId required'}, 400)
                return
            self.send_json(delete_learning_plan(plan_id))
            return

        if p == '/api/scheduler-scan':
            threshold_sec = body.get('thresholdSec', 180)
            try:
                result = handle_scheduler_scan(threshold_sec)
                self.send_json(result)
            except Exception as e:
                self.send_json({'ok': False, 'error': f'scheduler scan failed: {e}'}, 500)
            return

        if p == '/api/repair-flow-order':
            try:
                self.send_json(handle_repair_flow_order())
            except Exception as e:
                self.send_json({'ok': False, 'error': f'repair flow order failed: {e}'}, 500)
            return

        if p == '/api/scheduler-retry':
            task_id = body.get('taskId', '').strip()
            reason = body.get('reason', '').strip()
            if not task_id:
                self.send_json({'ok': False, 'error': 'taskId required'}, 400)
                return
            self.send_json(handle_scheduler_retry(task_id, reason))
            return

        if p == '/api/scheduler-escalate':
            task_id = body.get('taskId', '').strip()
            reason = body.get('reason', '').strip()
            if not task_id:
                self.send_json({'ok': False, 'error': 'taskId required'}, 400)
                return
            self.send_json(handle_scheduler_escalate(task_id, reason))
            return

        if p == '/api/scheduler-rollback':
            task_id = body.get('taskId', '').strip()
            reason = body.get('reason', '').strip()
            if not task_id:
                self.send_json({'ok': False, 'error': 'taskId required'}, 400)
                return
            self.send_json(handle_scheduler_rollback(task_id, reason))
            return

        if p == '/api/morning-brief/refresh':
            force = body.get('force', True)  # 从看板手动触发默认强制
            def do_refresh():
                try:
                    cmd = ['python3', str(SCRIPTS / 'fetch_morning_news.py')]
                    if force:
                        cmd.append('--force')
                    subprocess.run(cmd, timeout=120)
                    push_to_feishu()
                except Exception as e:
                    print(f'[refresh error] {e}', file=sys.stderr)
            threading.Thread(target=do_refresh, daemon=True).start()
            self.send_json({'ok': True, 'message': '采集已触发，约30-60秒后刷新'})
            return

        if p == '/api/add-skill':
            agent_id = body.get('agentId', '').strip()
            skill_name = body.get('skillName', body.get('name', '')).strip()
            desc = body.get('description', '').strip() or skill_name
            trigger = body.get('trigger', '').strip()
            if not agent_id or not skill_name:
                self.send_json({'ok': False, 'error': 'agentId and skillName required'}, 400)
                return
            result = add_skill_to_agent(agent_id, skill_name, desc, trigger)
            self.send_json(result)
            return

        if p == '/api/add-remote-skill':
            agent_id = body.get('agentId', '').strip()
            skill_name = body.get('skillName', '').strip()
            source_url = body.get('sourceUrl', '').strip()
            description = body.get('description', '').strip()
            if not agent_id or not skill_name or not source_url:
                self.send_json({'ok': False, 'error': 'agentId, skillName, and sourceUrl required'}, 400)
                return
            result = add_remote_skill(agent_id, skill_name, source_url, description)
            self.send_json(result)
            return

        if p == '/api/remote-skills-list':
            result = get_remote_skills_list()
            self.send_json(result)
            return

        if p == '/api/update-remote-skill':
            agent_id = body.get('agentId', '').strip()
            skill_name = body.get('skillName', '').strip()
            if not agent_id or not skill_name:
                self.send_json({'ok': False, 'error': 'agentId and skillName required'}, 400)
                return
            result = update_remote_skill(agent_id, skill_name)
            self.send_json(result)
            return

        if p == '/api/remove-remote-skill':
            agent_id = body.get('agentId', '').strip()
            skill_name = body.get('skillName', '').strip()
            if not agent_id or not skill_name:
                self.send_json({'ok': False, 'error': 'agentId and skillName required'}, 400)
                return
            result = remove_remote_skill(agent_id, skill_name)
            self.send_json(result)
            return

        if p == '/api/task-action':
            task_id = body.get('taskId', '').strip()
            action = body.get('action', '').strip()  # stop, cancel, resume
            reason = body.get('reason', '').strip() or f'皇上从看板{action}'
            if not task_id or action not in ('stop', 'cancel', 'resume'):
                self.send_json({'ok': False, 'error': 'taskId and action(stop/cancel/resume) required'}, 400)
                return
            result = handle_task_action(task_id, action, reason)
            self.send_json(result)
            return

        if p == '/api/archive-task':
            task_id = body.get('taskId', '').strip() if body.get('taskId') else ''
            archived = body.get('archived', True)
            archive_all = body.get('archiveAllDone', False)
            if not task_id and not archive_all:
                self.send_json({'ok': False, 'error': 'taskId or archiveAllDone required'}, 400)
                return
            result = handle_archive_task(task_id, archived, archive_all)
            self.send_json(result)
            return

        if p == '/api/task-todos':
            task_id = body.get('taskId', '').strip()
            todos = body.get('todos', [])  # [{id, title, status}]
            if not task_id:
                self.send_json({'ok': False, 'error': 'taskId required'}, 400)
                return
            # todos 输入校验
            if not isinstance(todos, list) or len(todos) > 200:
                self.send_json({'ok': False, 'error': 'todos must be a list (max 200 items)'}, 400)
                return
            valid_statuses = {'not-started', 'in-progress', 'completed'}
            for td in todos:
                if not isinstance(td, dict) or 'id' not in td or 'title' not in td:
                    self.send_json({'ok': False, 'error': 'each todo must have id and title'}, 400)
                    return
                if td.get('status', 'not-started') not in valid_statuses:
                    td['status'] = 'not-started'
            result = update_task_todos(task_id, todos)
            self.send_json(result)
            return

        if p == '/api/create-task':
            title = body.get('title', '').strip()
            org = body.get('org', '中书省').strip()
            official = body.get('official', '中书令').strip()
            priority = body.get('priority', 'normal').strip()
            template_id = body.get('templateId', '')
            params = body.get('params', {})
            if not title:
                self.send_json({'ok': False, 'error': 'title required'}, 400)
                return
            target_dept = body.get('targetDept', '').strip()
            result = handle_create_task(title, org, official, priority, template_id, params, target_dept)
            self.send_json(result)
            return

        if p == '/api/review-action':
            task_id = body.get('taskId', '').strip()
            action = body.get('action', '').strip()  # approve, reject
            comment = body.get('comment', '').strip()
            if not task_id or action not in ('approve', 'reject'):
                self.send_json({'ok': False, 'error': 'taskId and action(approve/reject) required'}, 400)
                return
            result = handle_review_action(task_id, action, comment)
            self.send_json(result)
            return

        if p == '/api/advance-state':
            task_id = body.get('taskId', '').strip()
            comment = body.get('comment', '').strip()
            if not task_id:
                self.send_json({'ok': False, 'error': 'taskId required'}, 400)
                return
            result = handle_advance_state(task_id, comment)
            self.send_json(result)
            return

        if p == '/api/agent-wake':
            agent_id = body.get('agentId', '').strip()
            message = body.get('message', '').strip()
            if not agent_id:
                self.send_json({'ok': False, 'error': 'agentId required'}, 400)
                return
            result = wake_agent(agent_id, message)
            self.send_json(result)
            return

        if p == '/api/agent-chat':
            agent_id = body.get('agentId', '').strip()
            message = body.get('message', '').strip()
            timeout = body.get('timeoutSec', 180)
            if not agent_id:
                self.send_json({'ok': False, 'error': 'agentId required'}, 400)
                return
            result = send_agent_message(agent_id, message, timeout)
            self.send_json(result)
            return

        if p == '/api/agent-soul/save':
            agent_id = body.get('agentId', '').strip()
            if not agent_id or not _SAFE_NAME_RE.match(agent_id):
                self.send_json({'ok': False, 'error': 'invalid agentId'}, 400)
                return
            result = write_agent_soul(agent_id, body.get('content', ''))
            self.send_json(result, 200 if result.get('ok') else 400)
            return

        if p == '/api/agent-soul/reorganize':
            agent_id = body.get('agentId', '').strip()
            if not agent_id or not _SAFE_NAME_RE.match(agent_id):
                self.send_json({'ok': False, 'error': 'invalid agentId'}, 400)
                return
            result = reorganize_agent_soul_by_hr(agent_id)
            self.send_json(result, 200 if result.get('ok') else 400)
            return

        if p == '/api/set-model':
            agent_id = body.get('agentId', '').strip()
            model = body.get('model', '').strip()
            if not agent_id or not model:
                self.send_json({'ok': False, 'error': 'agentId and model required'}, 400)
                return

            # Write to pending (atomic)
            pending_path = DATA / 'pending_model_changes.json'
            def update_pending(current):
                current = [x for x in current if x.get('agentId') != agent_id]
                current.append({'agentId': agent_id, 'model': model})
                return current
            atomic_json_update(pending_path, update_pending, [])

            # Async apply
            def apply_async():
                try:
                    subprocess.run(['python3', str(SCRIPTS / 'apply_model_changes.py')], timeout=30)
                    subprocess.run(['python3', str(SCRIPTS / 'sync_agent_config.py')], timeout=10)
                except Exception as e:
                    print(f'[apply error] {e}', file=sys.stderr)

            threading.Thread(target=apply_async, daemon=True).start()
            self.send_json({'ok': True, 'message': f'Queued: {agent_id} → {model}'})

        # Fix #139: 设置派发渠道（feishu/telegram/wecom/signal/tui）
        elif p == '/api/set-dispatch-channel':
            channel = body.get('channel', '').strip()
            allowed = {'feishu', 'telegram', 'wecom', 'signal', 'tui', 'discord', 'slack'}
            if not channel or channel not in allowed:
                self.send_json({'ok': False, 'error': f'channel must be one of: {", ".join(sorted(allowed))}'}, 400)
                return
            def _set_channel(cfg):
                cfg['dispatchChannel'] = channel
                return cfg
            atomic_json_update(DATA / 'agent_config.json', _set_channel, {})
            self.send_json({'ok': True, 'message': f'派发渠道已切换为 {channel}'})

        # ── 朝堂议政 POST ──
        elif p == '/api/court-discuss/start':
            topic = body.get('topic', '').strip()
            officials = body.get('officials', [])
            task_id = body.get('taskId', '').strip()
            if not topic:
                self.send_json({'ok': False, 'error': 'topic required'}, 400)
                return
            if not officials or not isinstance(officials, list):
                self.send_json({'ok': False, 'error': 'officials list required'}, 400)
                return
            # 校验官员 ID
            valid_ids = set(CD_PROFILES.keys())
            officials = [o for o in officials if o in valid_ids]
            if len(officials) < 2:
                self.send_json({'ok': False, 'error': '至少选择2位官员'}, 400)
                return
            self.send_json(cd_create(topic, officials, task_id))

        elif p == '/api/court-discuss/advance':
            sid = body.get('sessionId', '').strip()
            user_msg = body.get('userMessage', '').strip() or None
            decree = body.get('decree', '').strip() or None
            if not sid:
                self.send_json({'ok': False, 'error': 'sessionId required'}, 400)
                return
            self.send_json(cd_advance(sid, user_msg, decree))

        elif p == '/api/court-discuss/conclude':
            sid = body.get('sessionId', '').strip()
            if not sid:
                self.send_json({'ok': False, 'error': 'sessionId required'}, 400)
                return
            self.send_json(cd_conclude(sid))

        elif p == '/api/court-discuss/destroy':
            sid = body.get('sessionId', '').strip()
            if sid:
                cd_destroy(sid)
            self.send_json({'ok': True})

        else:
            self.send_error(404)


def main():
    parser = argparse.ArgumentParser(description='三省六部看板服务器')
    parser.add_argument('--port', type=int, default=7891)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--cors', default=None, help='Allowed CORS origin (default: reflect request Origin header)')
    args = parser.parse_args()

    global ALLOWED_ORIGIN, _DASHBOARD_PORT, _DEFAULT_ORIGINS
    ALLOWED_ORIGIN = args.cors
    _DASHBOARD_PORT = args.port
    _DEFAULT_ORIGINS = _DEFAULT_ORIGINS | {
        f'http://127.0.0.1:{args.port}', f'http://localhost:{args.port}',
    }

    # 多线程模式：避免单个长请求（如藏经阁深度问答）阻塞整个看板 API。
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    log.info(f'三省六部看板启动 → http://{args.host}:{args.port}')
    print(f'   按 Ctrl+C 停止')

    migrate_notification_config()

    # 启动恢复：重新派发上次被 kill 中断的 queued 任务
    threading.Timer(3.0, _startup_recover_queued_dispatches).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\n已停止')


if __name__ == '__main__':
    main()
