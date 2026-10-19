"""Continue a persisted initial fill without waiting for the scheduled retry."""
import argparse
import json
import os
from pathlib import Path


def should_continue(ledger, run, run_id, *, start=False):
    ledger = ledger or {}
    initial = ledger.get('initial_fill', {})
    cohort = initial.get('cohort_target_ids')
    legacy_exhausted = set(initial.get('exhausted_target_ids', []))
    eligible = [key for key, entry in (ledger or {}).get('targets', {}).items()
                if (cohort is None or key in cohort) and entry.get('retry_pending')
                and (key in legacy_exhausted or not entry.get('initial_fill_done')
                     or (entry.get('initial_fill_done_reason') != 'success'
                         and not entry.get('all_five_success_at')))]
    if not eligible:
        return False
    if start:
        return True
    return bool(run and run.get('run_id') == run_id and run.get('mode') == 'fill'
                and run.get('immediate_continuation'))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', default='data-state')
    args = parser.parse_args()
    root = Path(args.state_dir) / 'collection'
    read = lambda name: json.loads((root / name).read_text(encoding='utf-8')) if (root / name).is_file() else None
    result = should_continue(read('ledger.json'), read('last-run.json'), os.environ.get('GITHUB_RUN_ID'),
                             start=os.environ.get('START_INITIAL_FILL', '').lower() == 'true')
    if os.environ.get('GITHUB_OUTPUT'):
        with open(os.environ['GITHUB_OUTPUT'], 'a', encoding='utf-8', newline='\n') as output:
            output.write(f'continue_initial_fill={str(result).lower()}\n')
    print(json.dumps({'continue_initial_fill': result}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
