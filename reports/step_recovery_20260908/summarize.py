"""Summarize the selected baseline and a completed recovery replay."""
import hashlib
import json
from pathlib import Path
import sys

root = Path(__file__).resolve().parent
summary = Path(sys.argv[1]) if len(sys.argv) > 1 else root / 'replay/0908_03.json'
summary = summary.resolve()
baseline = {r['executable_path']: r for r in json.loads((root / 'baseline.json').read_text())}
replay = json.loads(summary.read_text())
results = {r['executable_path']: r for r in replay['results']}
retry_sources = []
for filename in sys.argv[2:]:
    retry = json.loads(Path(filename).read_text())
    retry_sources.append(filename)
    for result in retry['results']:
        results[result['executable_path']] = result
if any(r.get('evaluation_status') != 'valid' or r.get('process_status') != 'completed' for r in results.values()):
    raise RuntimeError('Incomplete simulator results cannot be counted as navigation improvements')
rows = []
for current in results.values():
    old = baseline[current['executable_path']]
    def failures(result):
        return sum(a['action_type'] == 'GoToObject' and a['status'] == 'failed' for a in result['actions'])
    nav = current.get('navigation_metrics', {})
    rows.append({
        'task_id': old['task_id'], 'baseline_failures': failures(old), 'replay_failures': failures(current),
        'baseline_seconds': old['run_time_seconds'], 'replay_seconds': current['run_time_seconds'],
        'baseline_timeout': old['timed_out'], 'replay_timeout': current['timed_out'],
        'visibility_recoveries': nav.get('visibility_recoveries', 0),
        'candidate_expansions': nav.get('candidate_expansions', 0),
        'navigation_teleports': sum(nav.get('action_counts', {}).get(a, 0) for a in ('Teleport', 'TeleportFull')),
        'baseline_peer_aborts': sum('was aborted by navigation agent' in a.get('error', '') for a in old['actions']),
        'replay_peer_aborts': sum('was aborted by navigation agent' in a.get('error', '') for a in current['actions']),
        'execution_status': current['execution_status'],
    })
rows.sort(key=lambda r: int(r['task_id'].rsplit('_', 1)[1]))
result = {'baseline_source': 'coderun_results/0908_02.json', 'replay_source': str(summary),
          'run_id': replay['run_id'], 'retry_sources': retry_sources, 'settings': {'movement_mode': 'step', 'execution_policy': 'legacy',
                                                'reachable_refresh_mode': 'full', 'timeout_seconds': 120},
          'tasks': rows}
result['source_sha256'] = {name: hashlib.sha256((root.parents[1] / 'scripts/executor_system' / name).read_bytes()).hexdigest() for name in ('movement.py', 'movement_coordinator.py', 'runtime.py', 'step_movement.py', 'object_interactor.py', 'parallel_runner.py')}
(root / 'comparison.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
old_failures = sum(r['baseline_failures'] for r in rows)
new_failures = sum(r['replay_failures'] for r in rows)
lines = ['# Step GoToObject 恢复验证', '',
         f'6 个固定样本的 GoToObject 失败：{old_failures} → {new_failures}。本报告是样本重放结果，不代表原批次全部 339 个任务的改善幅度。', '',
         '|任务|导航失败（原→现）|耗时秒（原→现）|视角恢复成功|现超时|导航瞬移|',
         '|---|---:|---:|---:|---|---:|']
for r in rows:
    lines.append(f"|{r['task_id']}|{r['baseline_failures']} → {r['replay_failures']}|{r['baseline_seconds']:.1f} → {r['replay_seconds']:.1f}|{r['visibility_recoveries']}|{r['replay_timeout']}|{r['navigation_teleports']}|")
lines += ['', '## 实现和边界', '',
          '- 到达后目标不可见时最多尝试六个俯仰角；失败恢复原视角，超时和取消直接传播。',
          '- step 候选默认从 10 点一次扩展到总上限 30 点，保留对象绑定及排除位置。',
          '- 逐机器人返回候选、到达和 event 目标刷新失败，保留其他机器人的成功结果。',
          '- NavigationMetrics 新增 visibility_look_attempts、visibility_recoveries、candidate_expansions；robot_failures 保留 navigation_decision_trace。',
          '- 纯 step；保留既有失败边、移动重规划和任务时限；未修改 LLM/PDDL 生成或评估语义。', '',
          '## 验证', '',
          '- 158 个导航相关测试通过，编译检查及 git diff --check 通过。独立审查发现的问题均补充回归并修正。',
          '- 扩展测试曾运行 239 项，其中 238 项通过，1 项现有评估断言失败：test_temperature_check_records_only_satisfied_state_without_mutating_goals 预期 GCR 0、实际 0.5。该测试只执行 Wait，换回 HEAD evaluate 方法仍复现；未在本次修改评估口径。',
          '- 最终重放设置：step / legacy / full / 120 秒 / 单 worker；原 executable_plan.py 未改写。',
          '- 最终六样本批次中 FloorPlan1_task_17 出现一次 BrokenPipeError，属于模拟器通信中断；其未完成动作不计作改善。仅该样本单独重跑，完整重跑结果用于该行比较，原始中断记录保留。',
          '- 最初沙箱内重放在创建 ~/.ai2thor 锁文件时失败，未执行动作，不计入比较；其产物保留在 replay/0908_01.json。',
          '- 恢复搜索增加了困难案例的耗时。不可见或物理不可达目标仍可能失败；这次改善不能解读为保证导航成功。', '',
          '## 产物', '',
          f'- 最终结果：[{summary.name}]({summary.relative_to(root)})',
          '- 单样本重跑结果：[retry/0908_01.json](retry/0908_01.json)',
          '- [逐任务比较](comparison.json)', '- [测试日志](unit-tests.log)',
          '- [重放脚本](replay.py)（从仓库根目录使用 pyenv exec python 运行；模拟器需要可写 AI2-THOR 缓存和 GPU）',
          '- [汇总脚本](summarize.py)', '']
(root / 'README.md').write_text('\n'.join(lines))
print(json.dumps(rows, ensure_ascii=False, indent=2))
