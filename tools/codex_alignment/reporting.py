"""Human-readable reports built from sanitized evidence, never request values."""
from collections import defaultdict
from datetime import datetime
from pathlib import Path
import os
import re
import tempfile
import time
from zoneinfo import ZoneInfo


STATUS = {'pass': '检查通过', 'drift': '发现请求差异', 'unverified': '证据不足，尚未验证'}
CASES = {'basic': '普通问答', 'tool': '调用工具后继续回答', 'compact': '压缩上下文后继续回答'}
BRIDGES = {'compat-responses': 'Responses 接口', 'compat-chat': 'Chat Completions 接口',
           'compat-messages': 'Messages 接口'}
REASONS = {
    'native_tool_continuation_not_observed': '没有采集到工具调用后的继续请求',
    'native_compact_not_observed': '没有采集到上下文压缩流程',
    'native_expected_answer_not_observed': '没有得到预期回答，不能确认工具或问答成功',
    'native_capture_or_provider_incomplete': '官方请求的采集或响应不完整',
    'missing_http_evidence': '缺少实际发往上游的 HTTP 请求证据',
    'missing_tls_evidence': '缺少 TLS 连接特征证据',
    'requested_mode_not_observed': '实际请求模式与指定测试模式不一致',
    'missing_success_or_evidence': '响应未完成，或没有采集到转换后的请求',
    'codex_store_stream_contract': '转换后的存储或流式响应设置不符合预期',
    'model_mapping_changed': '转换后的模型与指定模型不一致',
    'missing_converted_input': '转换后的请求缺少有效输入',
    'native_login_required': '检测用的官方 Codex 尚未登录',
    'production_snapshot_stale': '生产状态记录过期，无法确认当前运行情况',
    'production_account_unavailable': '生产账号或应用当前不可用',
    'production_probe_binding_changed': '测试账号或固定出口的绑定发生变化',
    'production_oauth_identity_changed': '生产账号身份发生变化',
    'native_oauth_account_mismatch': '官方 Codex 登录账号与生产选定账号不一致',
    'production_unhealthy': '生产健康检查未通过',
    'run_deadline': '本轮达到最长运行时间',
    'evidence_storage_full': '检测证据存储空间达到上限',
    'TimeoutError': '等待响应超时',
    'HTTPError': '外部服务返回错误',
    'no_supported_standard_model': '当前账号可见目录里没有可用于线上检测的标准模式模型',
}
BOUNDARIES = {
    'native_http_uses_custom_ca': '官方 HTTP 请求经过本地检查代理后，再从固定出口发出。',
    'tls_is_separate_measurement': 'TLS（加密连接特征）与 HTTP 请求是分别测量的。',
    'native_same_transaction_direct_http_unverified': '尚未证明官方直连时，同一笔请求的全部网络报文完全一致。',
    'fresh_gateway_connections': '本轮使用新建连接；连接复用的行为不在此次证明范围内。',
    'opaque_turn_metadata_contents_unverified': '轮次元数据中嵌套内容的内部含义尚未完整验证。',
    'window_uuid_suffix_relationship_unverified': '窗口编号与 UUID 后缀之间的关系尚未验证。',
    'oauth_refresh_tunnels_are_encrypted_and_not_counted_as_model_http': '登录凭据刷新走独立加密通道，不计入模型 HTTP 请求次数，也不会解密凭据。',
}


def reason(code):
    return REASONS.get(code, '需要进一步查看原始记录（原因代码：' + str(code) + '）')


def label(row):
    name = row.get('name', '未命名场景')
    if name in BRIDGES:
        return BRIDGES[name]
    match = re.fullmatch(r'v([\d_]+)-(\w+)-(basic|tool|compact)', name)
    if match:
        version, mode, case = match.groups()
        return 'Codex ' + version.replace('_', '.') + ' / ' + mode.capitalize() + ' / ' + CASES[case]
    return name


def completed(replay):
    result = replay.get('result', {})
    status = result.get('http_status')
    return (result.get('complete') is True and isinstance(status, int) and 200 <= status < 300
            and replay.get('provider_verified') is not False)


def notification_signature(report):
    fields = {'body_diff', 'header_contract_diff', 'tls_diff', 'identity_failures', 'reason',
              'header_order_equal', 'compression_equal', 'provider_verified'}
    return {'findings': report.get('findings', []), 'scenarios': [
        {'name': row['name'], 'status': row['status'], 'violations': row.get('violations', []),
         'diffs': [{k: v for k, v in item.items() if k in fields} for item in row.get('replays', [])]}
        for row in report.get('scenarios', [])]}


def operation_document(code):
    description = ('上一轮检测在完成前中断，尚未形成完整结果。' if code == 'previous_run_interrupted'
                   else '检测程序未能正常完成，尚不能确认当前请求是否对齐。')
    return ('Sub2API 请求对齐检测异常说明\n\n'
            '时间：' + datetime.fromtimestamp(time.time(), ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d %H:%M:%S') +
            '（北京时间）。\n\n一、发生了什么\n\n' + description +
            '\n这是检测本身未完成，不足以断定生产业务已经故障；此前的成功报告也不能证明当前正常。'
            '\n\n二、建议如何处理\n\n请检查 codex-alignment.service 的运行状态、日志、网络和资源；'
            '修复检测条件后重新执行。检测器没有自动修改生产请求或升级服务。'
            '\n\n排查代码：' + code + '\n')


def document(report):
    rows = report.get('scenarios', [])
    native = [r for r in rows if not r.get('name', '').startswith('compat-')]
    bridges = [r for r in rows if r.get('name', '').startswith('compat-')]
    replays = [p for r in native for p in r.get('replays', [])]
    good = sum(completed(p) for p in replays)
    answers = sum(r.get('answer_verified') is True for r in native)
    incomplete = [r for r in rows if r.get('status') == 'unverified' or r.get('reason')
                  or r.get('capture_errors') or (r in native and
                  (r.get('answer_verified') is not True or r.get('cli_exit') != 0))
                  or any(not completed(p) or p.get('status') == 'unverified' for p in r.get('replays', []))]
    findings = report.get('findings', [])
    operations = [f for f in findings if f.startswith('incomplete:')]
    status = report.get('status', 'unverified')
    lines = ['Sub2API 请求对齐检测报告', '', '一、结论', '', STATUS.get(status, '检测状态未知') + '。']
    if native and replays and good == len(replays) and not incomplete and not operations:
        lines.append('本轮已执行的问答、工具等场景均完成，且预期回答已核对；这只能证明本次测试可用，不能保证所有业务请求都没有问题。')
    else:
        lines.append('本轮仍有未完成或缺少证据的检查，不能把已有成功结果当作完整通过。')
    if status == 'drift':
        lines.append('实际发出的请求与官方 Codex 存在差异。“请求成功”和“请求完全一致”是两项独立结论。')
    if incomplete or operations:
        lines.append('同时存在检测不完整的情况，具体原因见下文；差异结果不会覆盖这些问题。')

    def section(title, items):
        if items:
            lines.extend(['', title, ''] + ['- ' + item for item in items])

    def date(value):
        return datetime.fromtimestamp(value, ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d %H:%M:%S') if value else '未记录'

    repo = report.get('repository', {})
    ci = {'success': '通过', 'failure': '失败', 'cancelled': '已取消', 'timed_out': '超时'}.get(
        repo.get('conclusion'), '尚未获得通过结果')
    production = report.get('production', {})
    sources = [
        '时间：' + date(report.get('started_at')) + ' 至 ' + date(report.get('finished_at')) + '（北京时间）。',
        '耗时：' + str(report.get('elapsed_seconds', '未记录')) + ' 秒。',
        '触发方式：' + {'acceptance': '手动完整检测', 'daily_or_change': '每日定时或变更触发',
                       'version_changed': '官方版本变化'}.get(report.get('trigger'), '未记录') + '。',
        '对照版本：固定基准 ' + str(report.get('baseline', {}).get('version', '未记录')) +
        '；本轮发现的官方稳定版 ' + str(report.get('latest_stable', '未记录')).removeprefix('rust-v') + '。',
        '模型：' + ('、'.join(sorted({r['model'] for r in native if r.get('model')})) or '未执行到模型检查') + '。',
        '仓库 main 提交：' + str(repo.get('head') or '未取得') + '；GitHub 离线回归：' + ci + '。',
        '实际生产来源：' + str(production.get('source', '未取得')) +
        '；检测前健康检查：' + ('正常' if production.get('health_ok') is True else '未确认正常') + '。',
        '仓库测试与生产实测分别记录；不能仅凭本次差异判断是最近合并上游导致的。',
    ]
    if repo.get('run_id'):
        sources.append('GitHub 检测记录：https://github.com/BAIMOoo/sub2api/actions/runs/' + str(repo['run_id']))
    section('二、检测了什么', sources)

    checks = [
        '官方 CLI 共发出 ' + str(report.get('native_upstream_attempts', '未记录')) + ' 次上游 HTTP 请求（包括模型目录等辅助请求，不等于对话次数）。',
        '官方场景预期回答：' + str(answers) + '/' + str(len(native)) + ' 核对通过。工具与压缩场景还要求返回实际读取的随机文件内容。',
        '生产请求重放：' + str(good) + '/' + str(len(replays)) + ' 收到完整成功响应。',
        '兼容接口：' + str(sum(r.get('status') == 'pass' for r in bridges)) + '/' + str(len(bridges)) +
        ' 通过；这里只验证响应完整及转换后的关键字段，不证明任意对话内容都正确。',
    ]
    for row in rows:
        detail = STATUS.get(row.get('status'), '状态未知')
        if row in native:
            detail = ('预期回答已核对' if row.get('answer_verified') else '预期回答未确认') + '；生产重放 ' + str(
                sum(completed(p) for p in row.get('replays', []))) + '/' + str(len(row.get('replays', []))) + ' 完成；' + detail
        checks.append(label(row) + '：' + detail + '。')
    for key, description in [('header_order_equal', '可比请求头顺序'), ('compression_equal', '请求压缩方式')]:
        checked = [p[key] for p in replays if key in p]
        checks.append(description + '：' + ('已比较 ' + str(len(checked)) + ' 次，' +
                      ('未发现差异' if all(checked) else '发现差异') if checked else '尚未取得证据') + '。')
    tls = [p['tls_diff'] for p in replays if 'tls_diff' in p]
    checks.append('单独测量的 TLS 特征：' + ('已比较 ' + str(len(tls)) + ' 次，' +
                  ('未发现差异' if not any(tls) else '有差异或缺失证据') if tls else '尚未取得证据') + '。')
    section('三、执行结果', checks)

    details = defaultdict(set)
    for row in rows:
        for replay in row.get('replays', []):
            for key in ('body_diff', 'header_contract_diff', 'tls_diff', 'identity_failures'):
                for path in replay.get(key, []):
                    details[(key, path)].add(label(row))
            for key in ('header_order_equal', 'compression_equal'):
                if replay.get(key) is False:
                    details[(key, '不一致')].add(label(row))
    explanations = []
    paths = [path for _, path in details]
    if any(p.startswith('$.user-agent') for p in paths):
        explanations.append('客户端身份说明（User-Agent）不同：生产转发时使用的客户端标识与官方 CLI 不一致。这说明上游看到的请求外观不同；本项本身不说明业务失败。')
    if any(p.startswith('$.version:') for p in paths):
        explanations.append('版本说明（version）不同：至少一个对照版本与生产请求声明的版本不一致。固定旧版本基准不会自动更新，需要结合各版本结果判断。')
    if any('.call_id.' in p or '.call_id:' in p for p in paths):
        explanations.append('工具调用编号（call_id）不同：用于把工具执行结果关联到原调用的编号发生变化。当前完成情况见上一节；仍需确认改写后关联关系在更多工具链路中始终正确。')
    if any(re.search(r'\.input\[\d+\]\.id:missing$', p) for p in paths):
        explanations.append('输入条目的 id 在一侧缺失：一个请求保留了条目标识，另一个没有；需要核对是否影响条目的保留或关联。')
    if any(kind == 'tls_diff' for kind, _ in details):
        explanations.append('加密连接（TLS）检查有差异或缺少证据：这项比较的是连接建立时的特征，与模型能否正常回答分别判断。')
    if any(kind == 'identity_failures' for kind, _ in details):
        explanations.append('会话身份关联检查未通过：请求中的会话、线程或窗口编号，没有满足已检查的对应关系。')
    if any(kind == 'header_order_equal' for kind, _ in details):
        explanations.append('请求头顺序不同：即使字段内容相同，实际发送时的排列也与官方 CLI 不一致。')
    if any(kind == 'compression_equal' for kind, _ in details):
        explanations.append('请求压缩方式不同：请求正文发送前的压缩设置与官方 CLI 不一致。')
    if details:
        explanations.append('下方逐项列出了全部已记录的差异位置及受影响场景；相同差异合并展示，没有按条数截断。路径表示字段位置，不包含凭据或请求原文。')
    elif status == 'drift':
        explanations.append('报告标记了差异，但未记录可解释的字段明细，需要检查原始证据；不能据此认定一致。')
    else:
        explanations.append('本轮没有记录到可比字段差异；缺少证据的项目仍不能视为通过。')
    section('四、差异是什么意思', explanations)

    missing = []
    for row in rows:
        for code in ([row['reason']] if row.get('reason') else []) + row.get('violations', []):
            missing.append(label(row) + '：' + reason(code) + '。')
        if row.get('capture_errors'):
            missing.append(label(row) + '：采集出现 ' + str(len(row['capture_errors'])) + ' 个错误，未输出可能含私密内容的错误原文。')
        for i, replay in enumerate(row.get('replays', []), 1):
            if not completed(replay) or replay.get('status') == 'unverified':
                missing.append(label(row) + ' 第 ' + str(i) + ' 次重放：' + reason(replay.get('reason', 'missing_success_or_evidence')) + '。')
    for code in operations:
        missing.append('本轮未完整结束：' + reason(code.partition(':')[2]) + '。')
    if repo.get('conclusion') != 'success':
        missing.append('该 main 提交的离线回归尚未确认通过，生产实测成功不能代替仓库回归。')
    for mode, code in report.get('coverage_gaps', {}).items():
        missing.append(('标准模式' if mode == 'standard' else mode) + '：' + reason(code) + '；离线覆盖不能代替线上实测。')
    if 'coverage:standard_model_now_available_review_selection' in findings:
        missing.append('账号现在出现可用标准模式模型，需要重新选择模型并补做线上检测。')
    explained = {row.get('name', '') + ':' + row.get('reason', row.get('status', '')) for row in rows}
    explained.update('coverage:' + mode + ':' + code for mode, code in report.get('coverage_gaps', {}).items())
    explained.add('coverage:standard_model_now_available_review_selection')
    for finding in findings:
        if finding not in explained and not finding.startswith('incomplete:') and finding != 'repository_regression_not_verified':
            missing.append('其他检测发现：' + reason(finding) + '。')
    missing.append('本服务仅覆盖 HTTP，WebSocket 尚未检测。')
    missing.extend(BOUNDARIES.get(b, '其他验证边界：' + b) for b in report.get('boundaries', []))
    missing.append('固定基准只批准已有请求正文样本，不代表已经批准全部线上行为。')
    section('五、未验证范围与限制', missing)

    actions = []
    if incomplete or operations or repo.get('conclusion') != 'success':
        actions.append('先处理上述未完成检查或失败的仓库回归，再判断剩余差异；旧的成功报告不能证明当前正常。')
    if status == 'drift':
        actions.append('建议核对上面的差异是否属于有意设计；若不属于已批准例外，再修复并复测。现有证据不足以判断最近一次合并是原因。')
    if report.get('coverage_gaps'):
        actions.append('补齐可用模型后再验证缺失的模式；当前报告不包含这些模式的线上通过结论。')
    if not actions:
        actions.append('继续每日检测；这次通过仅适用于已经执行并有证据的检查。')
    actions.append('检测程序只记录和通知，不会自动改请求、合并代码、升级生产或更新批准基准。')
    section('六、建议如何处理', actions)

    categories = {'body_diff': '正文', 'header_contract_diff': '请求头', 'tls_diff': 'TLS',
                  'identity_failures': '身份关联', 'header_order_equal': '请求头顺序', 'compression_equal': '压缩方式'}
    grouped = defaultdict(list)
    for (kind, path), affected in sorted(details.items()):
        grouped[(kind, tuple(sorted(affected)))].append(path)
    section('附录：差异位置（供排查使用）', [categories[kind] + '：' + '；'.join(paths) + '\n  涉及：' + '；'.join(affected)
                                                for (kind, affected), paths in grouped.items()])
    section('报告定位', ['运行编号：' + str(report.get('run', '未记录')),
                        '本报告不包含 token、账号凭据、代理密码或对话原文。'])
    return '\n'.join(lines) + '\n'


def save_document(root, report):
    run = report['run']
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', run):
        raise ValueError('invalid_report_run')
    text = document(report)
    for path in (Path(root) / 'runs' / run / 'report.md', Path(root) / 'latest.md'):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, temporary = tempfile.mkstemp(prefix='.report-', dir=path.parent)
        try:
            with os.fdopen(fd, 'w') as stream:
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    return text
