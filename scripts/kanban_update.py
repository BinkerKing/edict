#!/usr/bin/env python3
"""
看板任务更新工具 - 供各省部 Agent 调用

本工具操作 data/tasks_source.json（JSON 看板模式）。

用法:
  # 先生成唯一任务ID（推荐）
  python3 scripts/generate_task_id.py
  # 新建任务（收旨时）
  python3 kanban_update.py create JJC-20260223-102530123 "任务标题" Zhongshu 中书省 中书令

  # 更新状态
  python3 kanban_update.py state JJC-20260223-012 Menxia "规划方案已提交门下省"

  # 添加流转记录
  python3 kanban_update.py flow JJC-20260223-012 "中书省" "门下省" "规划方案提交审核"

  # 完成任务
  python3 kanban_update.py done JJC-20260223-012 "/path/to/output" "任务完成摘要"

  # 添加/更新子任务 todo
  python3 kanban_update.py todo JJC-20260223-012 1 "实现API接口" in-progress
  python3 kanban_update.py todo JJC-20260223-012 1 "" completed

  # 🔥 实时进展汇报（Agent 主动调用，频率不限）
  python3 kanban_update.py progress JJC-20260223-012 "正在分析需求，拟定3个子方案" "1.调研技术选型|2.撰写设计文档|3.实现原型"
"""
import datetime
import json, pathlib, sys, subprocess, logging, os, re

_BASE = pathlib.Path(os.environ['EDICT_HOME']) if 'EDICT_HOME' in os.environ else pathlib.Path(__file__).resolve().parent.parent
TASKS_FILE = _BASE / 'data' / 'tasks_source.json'
REFRESH_SCRIPT = _BASE / 'scripts' / 'refresh_live_data.py'

log = logging.getLogger('kanban')
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(name)s] %(message)s', datefmt='%H:%M:%S')

# 文件锁 —— 防止多 Agent 同时读写 tasks_source.json
from file_lock import atomic_json_read, atomic_json_update  # noqa: E402
from utils import now_iso  # noqa: E402

STATE_ORG_MAP = {
    'Taizi': '太子', 'Zhongshu': '中书省', 'Menxia': '门下省',
    'Assigned': '尚书省', 'Next': '尚书省',
    'Doing': '执行中', 'Review': '尚书省', 'Done': '完成', 'Blocked': '阻塞',
}

_STATE_AGENT_MAP = {
    'Taizi': 'taizi',
    'Zhongshu': 'zhongshu',
    'Menxia': 'menxia',
    'Assigned': 'shangshu',
    'Review': 'shangshu',
    'Pending': 'zhongshu',
}

_ORG_AGENT_MAP = {
    '礼部': 'libu', '藏经阁': 'libu', '户部': 'hubu', '兵部': 'bingbu',
    'PM小组': 'bingbu',
    '刑部': 'xingbu', '工部': 'rnd', '研发部': 'rnd', '吏部': 'libu_hr', '人事部': 'libu_hr',
    '中书省': 'zhongshu', '门下省': 'menxia', '尚书省': 'shangshu',
}

_AGENT_LABELS = {
    'main': '太子', 'taizi': '太子',
    'zhongshu': '中书省', 'menxia': '门下省', 'shangshu': '尚书省',
    'libu': '藏经阁', 'hubu': '户部', 'bingbu': 'PM小组', 'xingbu': '刑部',
    'rnd': '研发部', 'libu_hr': '人事部', 'zaochao': '钦天监',
}

_TERMINAL_STATES = {'Done', 'Cancelled'}


def _agent_session_contains_task(agent_id: str, task_id: str) -> bool:
    root = pathlib.Path.home() / '.openclaw' / 'agents' / agent_id / 'sessions'
    if not root.exists():
        return False
    for p in root.glob('*.jsonl'):
        try:
            if task_id in p.read_text(encoding='utf-8', errors='ignore'):
                return True
        except Exception:
            continue
    return False


def _nudge_agent(agent_id: str, task_id: str, title: str, state: str, now_text: str) -> None:
    """仅做纠偏督促：唤醒对应 agent 接单继续执行，不代替其完成任务。"""
    msg = (
        f"📋 调度纠偏通知\n"
        f"任务ID: {task_id}\n"
        f"标题: {title}\n"
        f"当前状态: {state}\n"
        f"当前动态: {now_text}\n"
        f"请立即接单并按职责推进，禁止口头承诺。"
    )
    try:
        result = subprocess.run(
            ["openclaw", "agent", "--agent", agent_id, "-m", msg, "--timeout", "240"],
            cwd=str(_BASE),
            capture_output=True,
            text=True,
            timeout=260,
        )
        if result.returncode == 0:
            log.info(f'📣 已催办 {agent_id} 处理 {task_id} (state={state})')
        else:
            err = (result.stderr or '').strip()[:200]
            log.warning(f'⚠️ 催办失败 {agent_id} {task_id}: rc={result.returncode} err={err}')
    except subprocess.TimeoutExpired:
        log.warning(f'⚠️ 催办超时 {agent_id} {task_id}')
    except Exception as e:
        log.warning(f'⚠️ 催办异常 {agent_id} {task_id}: {e}')


def _is_recent_menxia_reject_back(task: dict, within_seconds: int = 300) -> bool:
    """判断是否刚发生“门下封驳退回中书”，用于拦截滞后进展覆盖。"""
    flow = task.get('flow_log') or []
    if not flow:
        return False
    last = flow[-1]
    if (last.get('from') != '门下省') or (last.get('to') != '中书省'):
        return False
    remark = str(last.get('remark') or '')
    if '封驳' not in remark:
        return False
    ts = str(last.get('at') or '')
    try:
        dt = datetime.datetime.fromisoformat(ts.replace('Z', '+00:00'))
    except Exception:
        return False
    now_dt = datetime.datetime.now(datetime.timezone.utc)
    elapsed = max(0, int((now_dt - dt).total_seconds()))
    return elapsed <= within_seconds


def _looks_like_waiting_menxia(text: str) -> bool:
    s = str(text or '')
    hints = (
        '提交门下', '门下审议', '门下省审议',
        '等待审批', '等待审议', '等待门下',
    )
    return any(h in s for h in hints)


def _looks_like_path_token(token: str) -> bool:
    s = str(token or '').strip()
    if not s:
        return False
    if '/' in s or s.startswith('./') or s.startswith('../'):
        return True
    lower = s.lower()
    exts = ('.md', '.txt', '.pdf', '.docx', '.csv', '.json', '.png', '.jpg', '.jpeg', '.svg', '.html')
    return lower.endswith(exts)


def _normalize_output_paths(raw: str) -> list[str]:
    """将输出路径规范为绝对路径列表；非路径文本会被忽略。"""
    if not raw:
        return []
    parts = re.split(r'[;\n|]+', str(raw))
    out = []
    for p in parts:
        p = p.strip()
        if not p or not _looks_like_path_token(p):
            continue
        pp = pathlib.Path(p)
        if not pp.is_absolute():
            pp = (_BASE / pp).resolve()
        else:
            pp = pp.resolve()
        out.append(str(pp))
    # 去重但保持顺序
    dedup = []
    seen = set()
    for x in out:
        if x in seen:
            continue
        seen.add(x)
        dedup.append(x)
    return dedup


def _has_recent_zhongshu_submit_flow(task: dict, within_seconds: int = 300) -> bool:
    """判断是否存在最近一次“中书省 -> 门下省”的提交流转，用于约束 Zhongshu->Menxia 合法推进。"""
    flow = task.get('flow_log') or []
    if not flow:
        return False
    now_dt = datetime.datetime.now(datetime.timezone.utc)
    for item in reversed(flow[-8:]):
        if item.get('from') != '中书省' or item.get('to') != '门下省':
            continue
        remark = str(item.get('remark') or '')
        if not any(k in remark for k in ('提交', '审议', '准奏', '方案')):
            continue
        ts = str(item.get('at') or '')
        try:
            dt = datetime.datetime.fromisoformat(ts.replace('Z', '+00:00'))
        except Exception:
            continue
        elapsed = max(0, int((now_dt - dt).total_seconds()))
        return elapsed <= within_seconds
    return False

MAX_PROGRESS_LOG = 100  # 单任务最大进展日志条数

def load():
    return atomic_json_read(TASKS_FILE, [])

def _trigger_refresh():
    """异步触发 live_status 刷新，不阻塞调用方。"""
    try:
        subprocess.Popen(['python3', str(REFRESH_SCRIPT)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass

def find_task(tasks, task_id):
    return next((t for t in tasks if t.get('id') == task_id), None)


# 旨意标题最低要求
_MIN_TITLE_LEN = 6
_JUNK_TITLES = {
    '?', '？', '好', '好的', '是', '否', '不', '不是', '对', '了解', '收到',
    '嗯', '哦', '知道了', '开启了么', '可以', '不行', '行', 'ok', 'yes', 'no',
    '你去开启', '测试', '试试', '看看',
}

def _sanitize_text(raw, max_len=80):
    """清洗文本：剥离文件路径、URL、Conversation 元数据、传旨前缀、截断过长内容。"""
    t = (raw or '').strip()
    # 1) 剥离 Conversation info / Conversation 后面的所有内容
    t = re.split(r'\n*Conversation\b', t, maxsplit=1)[0].strip()
    # 2) 剥离 ```json 代码块
    t = re.split(r'\n*```', t, maxsplit=1)[0].strip()
    # 3) 剥离 Unix/Mac 文件路径 (/Users/xxx, /home/xxx, /opt/xxx, ./xxx)
    t = re.sub(r'[/\\.~][A-Za-z0-9_\-./]+(?:\.(?:py|js|ts|json|md|sh|yaml|yml|txt|csv|html|css|log))?', '', t)
    # 4) 剥离 URL
    t = re.sub(r'https?://\S+', '', t)
    # 5) 清理常见前缀: "传旨:" "下旨:" "下旨（xxx）:" 等
    t = re.sub(r'^(传旨|下旨)([（(][^)）]*[)）])?[：:\uff1a]\s*', '', t)
    # 6) 剥离系统元数据关键词
    t = re.sub(r'(message_id|session_id|chat_id|open_id|user_id|tenant_key)\s*[:=]\s*\S+', '', t)
    # 7) 合并多余空白
    t = re.sub(r'\s+', ' ', t).strip()
    # 8) 截断过长内容
    if len(t) > max_len:
        t = t[:max_len] + '…'
    return t


def _sanitize_title(raw):
    """清洗标题（最长 80 字符）。"""
    return _sanitize_text(raw, 80)


def _sanitize_remark(raw):
    """清洗流转备注（最长 120 字符）。"""
    return _sanitize_text(raw, 120)


def _infer_agent_id_from_runtime(task=None):
    """尽量推断当前执行该命令的 Agent。"""
    for k in ('OPENCLAW_AGENT_ID', 'OPENCLAW_AGENT', 'AGENT_ID'):
        v = (os.environ.get(k) or '').strip()
        if v:
            return v

    cwd = str(pathlib.Path.cwd())
    m = re.search(r'workspace-([a-zA-Z0-9_\-]+)', cwd)
    if m:
        return m.group(1)

    fpath = str(pathlib.Path(__file__).resolve())
    m2 = re.search(r'workspace-([a-zA-Z0-9_\-]+)', fpath)
    if m2:
        return m2.group(1)

    if task:
        state = task.get('state', '')
        org = task.get('org', '')
        aid = _STATE_AGENT_MAP.get(state)
        if aid is None and state in ('Doing', 'Next'):
            aid = _ORG_AGENT_MAP.get(org)
        if aid:
            return aid
    return ''


def _is_valid_task_title(title):
    """校验标题是否足够作为一个旨意任务。"""
    t = (title or '').strip()
    if len(t) < _MIN_TITLE_LEN:
        return False, f'标题过短（{len(t)}<{_MIN_TITLE_LEN}字），疑似非旨意'
    if t.lower() in _JUNK_TITLES:
        return False, f'标题 "{t}" 不是有效旨意'
    # 纯标点或问号
    if re.fullmatch(r'[\s?？!！.。,，…·\-—~]+', t):
        return False, '标题只有标点符号'
    # 看起来像文件路径
    if re.match(r'^[/\\~.]', t) or re.search(r'/[a-zA-Z0-9_-]+/[a-zA-Z0-9_-]+', t):
        return False, f'标题看起来像文件路径，请用中文概括任务'
    # 只剩标点和空白（清洗后可能变空）
    if re.fullmatch(r'[\s\W]*', t):
        return False, '标题清洗后为空'
    return True, ''


def cmd_create(task_id, title, state, org, official, remark=None):
    """新建任务（收旨时立即调用）"""
    # 清洗标题（剥离元数据）
    title = _sanitize_title(title)
    # 旨意标题校验
    valid, reason = _is_valid_task_title(title)
    if not valid:
        log.warning(f'⚠️ 拒绝创建 {task_id}：{reason}')
        print(f'[看板] 拒绝创建：{reason}', flush=True)
        return
    actual_org = STATE_ORG_MAP.get(state, org)
    clean_remark = _sanitize_remark(remark) if remark else f"下旨：{title}"
    created = [False]
    def modifier(tasks):
        existing = next((t for t in tasks if t.get('id') == task_id), None)
        if existing:
            # 任务ID必须全局唯一：禁止覆盖已有任务，避免跨会话串单
            log.warning(f'⚠️ 任务 {task_id} 已存在 (state={existing.get("state","?")})，拒绝覆盖；请使用新任务ID')
            print(f'[看板] 创建失败：任务ID已存在 {task_id}', flush=True)
            return tasks
        tasks.insert(0, {
            "id": task_id, "title": title, "official": official,
            "org": actual_org, "state": state,
            "now": clean_remark[:60] if remark else f"已下旨，等待{actual_org}接旨",
            "eta": "-", "block": "无", "output": "", "ac": "",
            "flow_log": [{"at": now_iso(), "from": "皇上", "to": actual_org, "remark": clean_remark}],
            "updatedAt": now_iso()
        })
        created[0] = True
        return tasks
    atomic_json_update(TASKS_FILE, modifier, [])
    if created[0]:
        _trigger_refresh()
        log.info(f'✅ 创建 {task_id} | {title[:30]} | state={state}')


# ── 状态流转合法性校验 ──
# 只允许文档定义的状态路径:
# Pending→Taizi→Zhongshu→Menxia→Assigned→Doing→Review→Done
# 额外: Blocked 可双向切换, Cancelled 从任意非终态可达, Next→Doing
_VALID_TRANSITIONS = {
    'Pending':   {'Taizi', 'Cancelled'},
    'Taizi':     {'Zhongshu', 'Cancelled'},
    'Zhongshu':  {'Menxia', 'Cancelled'},
    'Menxia':    {'Assigned', 'Zhongshu', 'Cancelled'},   # 封驳可回中书
    'Assigned':  {'Doing', 'Next', 'Blocked', 'Cancelled'},
    'Next':      {'Doing', 'Blocked', 'Cancelled'},
    'Doing':     {'Review', 'Blocked', 'Cancelled'},
    'Review':    {'Done', 'Menxia', 'Doing', 'Cancelled'},  # 可打回重审/重做
    'Blocked':   {'Doing', 'Next', 'Assigned', 'Review', 'Cancelled'},  # 解除后回原位
    'Done':      set(),       # 终态
    'Cancelled': set(),       # 终态
}


def cmd_state(task_id, new_state, now_text=None):
    """更新任务状态（原子操作，含流转合法性校验）"""
    old_state = [None]
    rejected = [False]
    assigned_meta = {'need_nudge': False, 'title': '', 'now': ''}
    reject_back_meta = {'need_nudge': False, 'title': '', 'now': ''}
    def modifier(tasks):
        t = find_task(tasks, task_id)
        if not t:
            log.error(f'任务 {task_id} 不存在')
            return tasks
        old_state[0] = t['state']
        allowed = _VALID_TRANSITIONS.get(old_state[0])
        if allowed is not None and new_state not in allowed:
            log.warning(f'⚠️ 非法状态转换 {task_id}: {old_state[0]} → {new_state}（允许: {allowed}）')
            rejected[0] = True
            return tasks
        # 约束：中书省进入门下审议前，必须先存在“中书省->门下省”的提交流转。
        # 防止旧上下文/口头回复直接把状态写成 Menxia，导致执行部门错乱。
        if old_state[0] == 'Zhongshu' and new_state == 'Menxia':
            if not _has_recent_zhongshu_submit_flow(t):
                log.warning(f'⚠️ 拒绝状态推进 {task_id}: Zhongshu→Menxia 缺少最近提交审议流转')
                rejected[0] = True
                return tasks
        t['state'] = new_state
        if new_state in STATE_ORG_MAP:
            t['org'] = STATE_ORG_MAP[new_state]
        if now_text:
            t['now'] = now_text
        # 门下封驳退回中书：主动重置为“修订态”，避免旧进展文案覆盖产生打架感
        if old_state[0] == 'Menxia' and new_state == 'Zhongshu':
            if not now_text:
                t['now'] = '门下省封驳，待中书省修订方案'
            t['todos'] = [
                {'id': '1', 'title': '根据门下省意见修订方案', 'status': 'in-progress'},
                {'id': '2', 'title': '补充实施细节与交付标准', 'status': 'not-started'},
                {'id': '3', 'title': '修订后再次提交门下省审议', 'status': 'not-started'},
            ]
            # 门下封驳后立即唤醒中书继续修订，避免等待通用催办超时窗口。
            reject_back_meta['need_nudge'] = True
            reject_back_meta['title'] = t.get('title', '')
            reject_back_meta['now'] = t.get('now', '')
        # 门下准奏进入尚书执行：强制切换为尚书执行态，避免沿用门下/中书旧 todo 与文案
        if old_state[0] == 'Menxia' and new_state == 'Assigned':
            if not now_text:
                t['now'] = '门下省准奏，转尚书省执行'
            t['org'] = '尚书省'
            t['todos'] = [
                {'id': '1', 'title': '分析派发方案', 'status': 'in-progress'},
                {'id': '2', 'title': '派发六部执行', 'status': 'not-started'},
                {'id': '3', 'title': '汇总执行结果', 'status': 'not-started'},
                {'id': '4', 'title': '提交中书省审核', 'status': 'not-started'},
            ]
        if new_state == 'Assigned':
            assigned_meta['need_nudge'] = True
            assigned_meta['title'] = t.get('title', '')
            assigned_meta['now'] = t.get('now', '')
        t['updatedAt'] = now_iso()
        return tasks
    atomic_json_update(TASKS_FILE, modifier, [])
    _trigger_refresh()
    if rejected[0]:
        log.info(f'❌ {task_id} 状态转换被拒: {old_state[0]} → {new_state}')
    else:
        log.info(f'✅ {task_id} 状态更新: {old_state[0]} → {new_state}')
        if assigned_meta['need_nudge']:
            _nudge_agent('shangshu', task_id, assigned_meta['title'], new_state, assigned_meta['now'])
        if reject_back_meta['need_nudge']:
            _nudge_agent('zhongshu', task_id, reject_back_meta['title'], new_state, reject_back_meta['now'])


def cmd_flow(task_id, from_dept, to_dept, remark):
    """添加流转记录（原子操作）"""
    clean_remark = _sanitize_remark(remark)
    agent_id = _infer_agent_id_from_runtime()
    agent_label = _AGENT_LABELS.get(agent_id, agent_id)
    rejected = [False]
    reject_reason = ['']
    def modifier(tasks):
        t = find_task(tasks, task_id)
        if not t:
            log.error(f'任务 {task_id} 不存在')
            return tasks
        if t.get('state') in _TERMINAL_STATES:
            rejected[0] = True
            reject_reason[0] = f'终态任务禁止追加流转（state={t.get("state")})'
            return tasks
        t.setdefault('flow_log', []).append({
            "at": now_iso(), "from": from_dept, "to": to_dept, "remark": clean_remark,
            "agent": agent_id, "agentLabel": agent_label,
        })
        # 状态优先：org 以当前状态对应部门为准，避免 flow(to=中书省) 覆盖 Assigned/Doing 等执行归属。
        cur_state = t.get('state', '')
        t['org'] = STATE_ORG_MAP.get(cur_state, to_dept)
        t['updatedAt'] = now_iso()
        return tasks
    atomic_json_update(TASKS_FILE, modifier, [])
    _trigger_refresh()
    if rejected[0]:
        log.warning(f'⚠️ {task_id} flow 被拒绝: {reject_reason[0]}')
        print(f'[看板] flow 被拒绝：{reject_reason[0]}', flush=True)
    else:
        log.info(f'✅ {task_id} 流转记录: {from_dept} → {to_dept}')


def cmd_done(task_id, output_path='', summary=''):
    """标记任务完成（原子操作）"""
    rejected = [False]
    reject_reason = ['']
    nudge_on_reject = {'do': False, 'title': '', 'state': '', 'now': ''}

    def modifier(tasks):
        t = find_task(tasks, task_id)
        if not t:
            log.error(f'任务 {task_id} 不存在')
            return tasks

        actor = _infer_agent_id_from_runtime(t)
        cur_state = t.get('state', '')

        # 仅尚书省可收尾 done（允许空 actor 兼容历史手工调用）
        if actor and actor != 'shangshu':
            rejected[0] = True
            reject_reason[0] = f'仅尚书省可执行 done，当前执行者={actor}'
            nudge_on_reject['do'] = True
            nudge_on_reject['title'] = t.get('title', '')
            nudge_on_reject['state'] = t.get('state', '')
            nudge_on_reject['now'] = t.get('now', '')
            return tasks

        # 必须由正常流转进入 Review，再允许 done
        if cur_state != 'Review':
            rejected[0] = True
            reject_reason[0] = f'当前状态 {cur_state} 不允许 done，必须先进入 Review'
            nudge_on_reject['do'] = True
            nudge_on_reject['title'] = t.get('title', '')
            nudge_on_reject['state'] = t.get('state', '')
            nudge_on_reject['now'] = t.get('now', '')
            return tasks

        # 若有 todos，必须全部 completed 才允许 done
        todos = t.get('todos') or []
        if todos:
            unfinished = [td for td in todos if td.get('status') != 'completed']
            if unfinished:
                rejected[0] = True
                reject_reason[0] = f'仍有未完成子任务 {len(unfinished)} 项，禁止 done'
                nudge_on_reject['do'] = True
                nudge_on_reject['title'] = t.get('title', '')
                nudge_on_reject['state'] = t.get('state', '')
                nudge_on_reject['now'] = t.get('now', '')
                return tasks

        # 必须有尚书省真实接单会话
        if not _agent_session_contains_task('shangshu', task_id):
            rejected[0] = True
            reject_reason[0] = '尚书省未检测到接单会话，禁止直接 done'
            if cur_state == 'Assigned':
                t['now'] = '尚书省待接单，调度继续督促中'
                t['block'] = '尚书省未接单'
                t['updatedAt'] = now_iso()
            nudge_on_reject['do'] = True
            nudge_on_reject['title'] = t.get('title', '')
            nudge_on_reject['state'] = t.get('state', '')
            nudge_on_reject['now'] = t.get('now', '')
            return tasks

        t['state'] = 'Done'
        t['org'] = '完成'
        t['block'] = '无'
        output_paths = _normalize_output_paths(output_path)
        t['output'] = ';'.join(output_paths) if output_paths else output_path
        t['outputPaths'] = output_paths
        t['now'] = summary or '任务已完成'
        t.setdefault('flow_log', []).append({
            "at": now_iso(), "from": t.get('org', '执行部门'),
            "to": "皇上", "remark": f"✅ 完成：{summary or '任务已完成'}"
        })
        # 同步设置 outputMeta，包含多文件明细，避免依赖 refresh_live_data.py 异步补充
        if output_paths:
            files = []
            exists_all = True
            for ap in output_paths:
                p = pathlib.Path(ap)
                if p.exists():
                    ts = datetime.datetime.fromtimestamp(p.stat().st_mtime).strftime('%Y-%m-%d %H:%M:%S')
                    files.append({"path": ap, "exists": True, "lastModified": ts})
                else:
                    exists_all = False
                    files.append({"path": ap, "exists": False, "lastModified": None})
            t['outputMeta'] = {"existsAll": exists_all, "files": files}
        t['updatedAt'] = now_iso()
        return tasks
    atomic_json_update(TASKS_FILE, modifier, [])
    _trigger_refresh()
    if rejected[0]:
        log.warning(f'⚠️ {task_id} done 被拒绝: {reject_reason[0]}')
        print(f'[看板] done 被拒绝：{reject_reason[0]}', flush=True)
        if nudge_on_reject['do']:
            _nudge_agent('shangshu', task_id, nudge_on_reject['title'], nudge_on_reject['state'], nudge_on_reject['now'])
        return
    log.info(f'✅ {task_id} 已完成')


def cmd_block(task_id, reason):
    """标记阻塞（原子操作）"""
    def modifier(tasks):
        t = find_task(tasks, task_id)
        if not t:
            log.error(f'任务 {task_id} 不存在')
            return tasks
        t['state'] = 'Blocked'
        t['block'] = reason
        t['updatedAt'] = now_iso()
        return tasks
    atomic_json_update(TASKS_FILE, modifier, [])
    _trigger_refresh()
    log.warning(f'⚠️ {task_id} 已阻塞: {reason}')


def cmd_progress(task_id, now_text, todos_pipe='', tokens=0, cost=0.0, elapsed=0):
    """🔥 实时进展汇报 — Agent 主动调用，不改变状态，只更新 now + todos

    now_text: 当前正在做什么的一句话描述（必填）
    todos_pipe: 可选，用 | 分隔的 todo 列表，格式：
        "已完成的事项✅|正在做的事项🔄|计划做的事项"
        - 以 ✅ 结尾 → completed
        - 以 🔄 结尾 → in-progress
        - 其他 → not-started
    tokens: 可选，本次消耗的 token 数
    cost: 可选，本次成本（美元）
    elapsed: 可选，本次耗时（秒）
    """
    clean = _sanitize_remark(now_text)
    # 解析 todos_pipe
    parsed_todos = None
    if todos_pipe:
        new_todos = []
        for i, item in enumerate(todos_pipe.split('|'), 1):
            item = item.strip()
            if not item:
                continue
            if item.endswith('✅'):
                status = 'completed'
                title = item[:-1].strip()
            elif item.endswith('🔄'):
                status = 'in-progress'
                title = item[:-1].strip()
            else:
                status = 'not-started'
                title = item
            new_todos.append({'id': str(i), 'title': title, 'status': status})
        if new_todos:
            parsed_todos = new_todos

    # 解析资源消耗参数
    try:
        tokens = int(tokens) if tokens else 0
    except (ValueError, TypeError):
        tokens = 0
    try:
        cost = float(cost) if cost else 0.0
    except (ValueError, TypeError):
        cost = 0.0
    try:
        elapsed = int(elapsed) if elapsed else 0
    except (ValueError, TypeError):
        elapsed = 0

    done_cnt = [0]
    total_cnt = [0]
    rejected = [False]
    reject_reason = ['']
    def modifier(tasks):
        t = find_task(tasks, task_id)
        if not t:
            log.error(f'任务 {task_id} 不存在')
            return tasks
        actor = _infer_agent_id_from_runtime(t)
        if t.get('state') in _TERMINAL_STATES:
            rejected[0] = True
            reject_reason[0] = f'终态任务禁止更新进展（state={t.get("state")})'
            return tasks
        # Assigned 阶段仅尚书省可写进展，避免门下/中书滞后回包覆盖执行态。
        if t.get('state') == 'Assigned' and actor and actor != 'shangshu':
            rejected[0] = True
            reject_reason[0] = f'Assigned 阶段仅尚书省可更新进展（当前={actor}）'
            return tasks
        # 状态-承办一致性兜底：Assigned 必须属于尚书省。
        if t.get('state') == 'Assigned':
            t['org'] = '尚书省'
        # 门下刚封驳退回后的短窗口内，拦截滞后“等待门下审批”进展，避免覆盖修订态
        if t.get('state') == 'Zhongshu' and _is_recent_menxia_reject_back(t):
            if _looks_like_waiting_menxia(clean):
                rejected[0] = True
                reject_reason[0] = '检测到封驳回退后的滞后进展文本，已拒绝覆盖当前修订态'
                return tasks
        t['now'] = clean
        if parsed_todos is not None:
            t['todos'] = parsed_todos
            # 工作流自动对齐：尚书省在 Doing 阶段且 todos 全部完成时，进入 Review
            if actor == 'shangshu' and t.get('state') == 'Doing':
                if parsed_todos and all(td.get('status') == 'completed' for td in parsed_todos):
                    t['state'] = 'Review'
                    t['org'] = '尚书省'
                    t.setdefault('flow_log', []).append({
                        "at": now_iso(),
                        "from": "六部",
                        "to": "尚书省",
                        "remark": "执行汇总完成，进入审查"
                    })
        # 多 Agent 并行进展日志
        at = now_iso()
        agent_id = _infer_agent_id_from_runtime(t)
        agent_label = _AGENT_LABELS.get(agent_id, agent_id)
        log_todos = parsed_todos if parsed_todos is not None else t.get('todos', [])
        log_entry = {
            'at': at, 'agent': agent_id, 'agentLabel': agent_label,
            'text': clean, 'todos': log_todos,
            'state': t.get('state', ''), 'org': t.get('org', ''),
        }
        # 资源消耗（可选字段，有值才写入）
        if tokens > 0:
            log_entry['tokens'] = tokens
        if cost > 0:
            log_entry['cost'] = cost
        if elapsed > 0:
            log_entry['elapsed'] = elapsed
        t.setdefault('progress_log', []).append(log_entry)
        # 限制 progress_log 大小，防止无限增长
        if len(t['progress_log']) > MAX_PROGRESS_LOG:
            t['progress_log'] = t['progress_log'][-MAX_PROGRESS_LOG:]
        t['updatedAt'] = at
        done_cnt[0] = sum(1 for td in t.get('todos', []) if td.get('status') == 'completed')
        total_cnt[0] = len(t.get('todos', []))
        return tasks
    atomic_json_update(TASKS_FILE, modifier, [])
    _trigger_refresh()
    if rejected[0]:
        log.warning(f'⚠️ {task_id} progress 被拒绝: {reject_reason[0]}')
        print(f'[看板] progress 被拒绝：{reject_reason[0]}', flush=True)
        return
    res_info = ''
    if tokens or cost or elapsed:
        res_info = f' [res: {tokens}tok/${cost:.4f}/{elapsed}s]'
    log.info(f'📡 {task_id} 进展: {clean[:40]}... [{done_cnt[0]}/{total_cnt[0]}]{res_info}')

def cmd_todo(task_id, todo_id, title, status='not-started', detail=''):
    """添加或更新子任务 todo（原子操作）

    status: not-started / in-progress / completed
    detail: 可选，该子任务的详细产出/说明（Markdown 格式）
    """
    # 校验 status 值
    if status not in ('not-started', 'in-progress', 'completed'):
        status = 'not-started'
    result_info = [0, 0]
    rejected = [False]
    reject_reason = ['']
    def modifier(tasks):
        t = find_task(tasks, task_id)
        if not t:
            log.error(f'任务 {task_id} 不存在')
            return tasks
        if t.get('state') in _TERMINAL_STATES:
            rejected[0] = True
            reject_reason[0] = f'终态任务禁止更新 todo（state={t.get("state")})'
            return tasks
        if 'todos' not in t:
            t['todos'] = []
        existing = next((td for td in t['todos'] if str(td.get('id')) == str(todo_id)), None)
        if existing:
            existing['status'] = status
            if title:
                existing['title'] = title
            if detail:
                existing['detail'] = detail
        else:
            item = {'id': todo_id, 'title': title, 'status': status}
            if detail:
                item['detail'] = detail
            t['todos'].append(item)
        t['updatedAt'] = now_iso()
        result_info[0] = sum(1 for td in t['todos'] if td.get('status') == 'completed')
        result_info[1] = len(t['todos'])
        return tasks
    atomic_json_update(TASKS_FILE, modifier, [])
    _trigger_refresh()
    if rejected[0]:
        log.warning(f'⚠️ {task_id} todo 被拒绝: {reject_reason[0]}')
        print(f'[看板] todo 被拒绝：{reject_reason[0]}', flush=True)
    else:
        log.info(f'✅ {task_id} todo [{result_info[0]}/{result_info[1]}]: {todo_id} → {status}')

_CMD_MIN_ARGS = {
    'create': 6, 'state': 3, 'flow': 5, 'done': 2, 'block': 3, 'todo': 4, 'progress': 3,
}

if __name__ == '__main__':
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(0)
    cmd = args[0]
    if cmd in _CMD_MIN_ARGS and len(args) < _CMD_MIN_ARGS[cmd]:
        print(f'错误："{cmd}" 命令至少需要 {_CMD_MIN_ARGS[cmd]} 个参数，实际 {len(args)} 个')
        print(__doc__)
        sys.exit(1)
    if cmd == 'create':
        cmd_create(args[1], args[2], args[3], args[4], args[5], args[6] if len(args)>6 else None)
    elif cmd == 'state':
        cmd_state(args[1], args[2], args[3] if len(args)>3 else None)
    elif cmd == 'flow':
        cmd_flow(args[1], args[2], args[3], args[4])
    elif cmd == 'done':
        cmd_done(args[1], args[2] if len(args)>2 else '', args[3] if len(args)>3 else '')
    elif cmd == 'block':
        cmd_block(args[1], args[2])
    elif cmd == 'todo':
        # 解析可选 --detail 参数
        todo_pos = []
        todo_detail = ''
        ti = 1
        while ti < len(args):
            if args[ti] == '--detail' and ti + 1 < len(args):
                todo_detail = args[ti + 1]; ti += 2
            else:
                todo_pos.append(args[ti]); ti += 1
        cmd_todo(
            todo_pos[0] if len(todo_pos) > 0 else '',
            todo_pos[1] if len(todo_pos) > 1 else '',
            todo_pos[2] if len(todo_pos) > 2 else '',
            todo_pos[3] if len(todo_pos) > 3 else 'not-started',
            detail=todo_detail,
        )
    elif cmd == 'progress':
        # 解析可选 --tokens/--cost/--elapsed 参数
        pos_args = []
        kw = {}
        i = 1
        while i < len(args):
            if args[i] == '--tokens' and i + 1 < len(args):
                kw['tokens'] = args[i + 1]; i += 2
            elif args[i] == '--cost' and i + 1 < len(args):
                kw['cost'] = args[i + 1]; i += 2
            elif args[i] == '--elapsed' and i + 1 < len(args):
                kw['elapsed'] = args[i + 1]; i += 2
            else:
                pos_args.append(args[i]); i += 1
        cmd_progress(
            pos_args[0] if len(pos_args) > 0 else '',
            pos_args[1] if len(pos_args) > 1 else '',
            pos_args[2] if len(pos_args) > 2 else '',
            tokens=kw.get('tokens', 0),
            cost=kw.get('cost', 0.0),
            elapsed=kw.get('elapsed', 0),
        )
    else:
        print(__doc__)
        sys.exit(1)
