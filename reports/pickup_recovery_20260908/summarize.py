"""Compare action outcomes for the fixed before/after simulator replays."""
import json
import re
from pathlib import Path

OUT = Path(__file__).resolve().parent


def counts(result, downstream_keys):
    actions = result['actions']
    pickups = [a for a in actions if a['action_type'] == 'PickupObject']
    downstream = [a for a in actions if a['action_key'] in downstream_keys]
    return {
        'pickup_succeeded': sum(a['status'] == 'succeeded' for a in pickups),
        'pickup_total': len(pickups),
        'pickup_visibility_failed': sum('specified visibility' in (a.get('error') or '')
                                        and a['status'] == 'failed' for a in pickups),
        'downstream_succeeded': sum(a['status'] == 'succeeded' for a in downstream),
        'downstream_total': len(downstream_keys),
        'all_actions_succeeded': sum(a['status'] == 'succeeded' for a in actions),
        'all_actions_total': len(actions),
        'seconds': result.get('run_time_seconds'),
        'timed_out': result.get('timed_out'),
        'execution_status': result.get('execution_status'),
        'process_status': result.get('process_status'),
        'pickup_reposition_successes': sum(
            line.startswith('PickupObject interaction reposition final action')
            and line.endswith(': succeeded')
            for line in result.get('stdout', '').splitlines()),
        'navigation_teleports': sum(v for k, v in result.get('navigation_metrics', {}).get('action_counts', {}).items()
                                    if k.startswith('Teleport')),
    }


def main():
    rows = []
    for sample in json.loads((OUT / 'samples.json').read_text()):
        task_id = sample['task_id']
        before = json.loads((OUT / 'baseline' / (task_id + '.json')).read_text())
        after = json.loads((OUT / 'updated' / (task_id + '.json')).read_text())
        affected, keys = set(), set()
        for action in before['actions']:
            if action['robot_id'] in affected and action['action_type'] != 'PickupObject':
                keys.add(action['action_key'])
            if action['action_type'] == 'PickupObject' and action['status'] == 'failed':
                affected.add(action['robot_id'])
        old_actions = {a['action_key']: a for a in before['actions']}
        transitions = [
            {'action_key': a['action_key'], 'before': old_actions.get(a['action_key'], {}).get('status'),
             'after': a['status'], 'before_error': old_actions.get(a['action_key'], {}).get('error'),
             'after_error': a.get('error')}
            for a in after['actions'] if a['action_type'] == 'PickupObject'
        ]
        rows.append({'task_id': task_id, 'category': sample['category'],
                     'baseline': counts(before, keys), 'updated': counts(after, keys),
                     'pickup_transitions': transitions,
                     'action_changes': [
                         {'action_key': a['action_key'], 'action_type': a['action_type'],
                          'before': old_actions.get(a['action_key'], {}).get('status'),
                          'after': a['status'], 'error': a.get('error')}
                         for a in after['actions']
                         if a['status'] != old_actions.get(a['action_key'], {}).get('status')
                     ]})
    (OUT / 'comparison.json').write_text(json.dumps(rows, indent=2) + '\n')
    for row in rows:
        a, b = row['baseline'], row['updated']
        print(row['task_id'], 'pickup', a['pickup_succeeded'], '->', b['pickup_succeeded'],
              '/', a['pickup_total'], 'seconds', round(a['seconds'], 1), '->', round(b['seconds'], 1),
              'timeout', a['timed_out'], '->', b['timed_out'],
              'recovered', b['pickup_reposition_successes'])

    totals = {
        variant: {key: sum(row[variant][key] for row in rows)
                  for key in ('pickup_succeeded', 'pickup_total', 'downstream_succeeded',
                              'downstream_total', 'all_actions_succeeded', 'all_actions_total',
                              'seconds', 'timed_out', 'navigation_teleports', 'pickup_reposition_successes')}
        for variant in ('baseline', 'updated')
    }
    old, new = totals['baseline'], totals['updated']
    test_log = (OUT / 'unit-tests.log').read_text()
    test_count = re.search(r'Ran (\d+) tests', test_log).group(1)
    lines = [
        '# step 模式 PickupObject 换位恢复验证', '',
        f"固定 {len(rows)} 个样本中，实际抓取成功数 **{old['pickup_succeeded']}/{old['pickup_total']} → "
        f"{new['pickup_succeeded']}/{new['pickup_total']}**；这是样本结果，不代表原批次全部任务的改善幅度。", '',
        f"任务执行耗时合计 **{old['seconds']:.1f} → {new['seconds']:.1f} 秒**，"
        f"超时 **{old['timed_out']} → {new['timed_out']}**，"
        f"全部动作成功数 **{old['all_actions_succeeded']}/{old['all_actions_total']} → "
        f"{new['all_actions_succeeded']}/{new['all_actions_total']}**。", '',
        '## 逐任务结果', '',
        '|任务|抓取成功/总数（前→后）|耗时秒（前→后）|换位后抓取成功次数|超时（前→后）|',
        '|---|---:|---:|---:|---|',
    ]
    for row in rows:
        a, b = row['baseline'], row['updated']
        lines.append(f"|{row['task_id']}|{a['pickup_succeeded']}/{a['pickup_total']} → "
                     f"{b['pickup_succeeded']}/{b['pickup_total']}|{a['seconds']:.1f} → {b['seconds']:.1f}|"
                     f"{b['pickup_reposition_successes']}|{a['timed_out']} → {b['timed_out']}|")
    lines += [
        '', '## 后续执行与限制', '',
        f"以基线失败抓取之后、同一机器人的非抓取动作建立固定集合，并按 action_key 对齐："
        f"后续动作成功数 **{old['downstream_succeeded']}/{old['downstream_total']} → "
        f"{new['downstream_succeeded']}/{new['downstream_total']}**。未执行动作不计成功。", '',
        '恢复不保证整项任务完成。困难目标会增加候选搜索耗时；原候选搜索上限和 120 秒任务时限仍生效。'
        '关闭容器、缺少可见站位或缺少安全路径时仍可能失败；本次不自动开容器、不强制抓取，也不改变调度与评估语义。', '',
        '这是固定样本各一次前后重放，不能作为稳定的全批次成功率或性能估计。历史结果中的 55 次可见性失败仅用于选样，'
        '实际比较采用本次重新运行的基线，不与旧批次结果直接计算收益。', '',
        '## 实现与验证', '',
        '- 先执行既有视角重试；step 抓取仍不可见或碰撞时，最多换位一次并执行一次最终抓取。'
        '使用原 objectId，排除当前站位，关闭自动手部准备。失败成为终止恢复错误，避免可见性和碰撞分支相互重入。',
        '- 换位请求构建、步进导航和最终抓取共享导航执行作用域；保留能力检查、目标绑定、计数、任务时限和取消语义。',
        '- 保留 SliceObject 入口和 teleport 重试次数。普通异常恢复镜头；超时、取消和终止换位错误不再操作镜头。',
        f"- {test_count} 项相关测试通过；编译检查和 git diff --check 通过。独立审查发现的 teleport 重试次数、"
        '原始错误上下文及普通异常镜头恢复问题均有失败回归和修正后的验证。',
        f"- 最终重放导航瞬移计数：{new['navigation_teleports']}；单元测试同时验证抓取恢复不调用瞬移、不新增 forceAction。", '',
        '## 重放来源与产物', '',
        '- 来源：parallel_runs/pddlrun_llmseparate_20260908_171313；对应 coderun_results/0908_02.json 的 339 个执行计划。'
        '选取导航失败后抓取失败的前三个任务、其余四次可见性失败所属任务，以及前三个导航和抓取均成功的对照任务；按原结果顺序选择并去重。',
        '- 设置：原 executable_plan.py / step / legacy / full / 120 秒 / 单 worker。'
        'samples.json 保存计划路径与 SHA-256；code-hashes.json 保存前后代码快照校验值。'
        '两份快照只有 object_interactor.py 不同，工作区既有其他改动保留。',
        '- baseline/ 与 updated/ 保存最终对比结果、模拟器日志和指标；initial_revision/ 与 pre_camera_fix/ 保留审查期间的中间重放，不计入最终比较。',
        '- [逐任务比较及抓取状态变化](comparison.json)、[固定样本清单](samples.json)、[测试日志](unit-tests.log)。', '',
        '从仓库根目录执行；重放会跳过已存在的任务结果。如需新一轮实验，请先另存现有结果目录。', '',
        '```bash',
        '/home/dwb/.pyenv/bin/pyenv exec python reports/pickup_recovery_20260908/replay.py baseline',
        '/home/dwb/.pyenv/bin/pyenv exec python reports/pickup_recovery_20260908/replay.py updated',
        '/home/dwb/.pyenv/bin/pyenv exec python reports/pickup_recovery_20260908/summarize.py',
        '```', '',
        '模拟器需要本机 GPU 和可写 AI2-THOR 缓存。', '',
    ]
    pan_case = next(row for row in rows if row['task_id'] == 'FloorPlan2_task_7')
    pan_result = json.loads((OUT / 'updated' / 'FloorPlan2_task_7.json').read_text())
    pan_id = 'Pan|-01.29|+00.90|-01.35'
    if pan_case['updated']['pickup_succeeded'] > pan_case['baseline']['pickup_succeeded']:
        held = pan_result['final_snapshot']['held_objects']
        assert pan_id in held['robot2']
        lines += [
            '## 已确认的恢复实例', '',
            f'FloorPlan2_task_7：robot2 换位后成功抓取原目标 `{pan_id}`。'
            '动作 `0:robot2:5` 从失败变为成功，最终快照中 robot2 确实持有该平底锅。'
            '同一任务的 robot1 动作 `0:robot1:5`（GoToObject）从成功变为无安全路径失败；'
            '因此该样本的抓取成功数增加，但全部动作成功数未增加。'
            '本次单次前后重放不足以证明导航变化的唯一原因。', '',
        ]
    (OUT / 'README.md').write_text('\n'.join(lines))


if __name__ == '__main__':
    main()
