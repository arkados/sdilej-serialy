"""Check both account ledgers against the immutable migration baseline."""
import argparse
from collections import Counter
import gzip
import json
from pathlib import Path

from .dual import load


def targets(state):
    result = {}
    counters = Counter()
    for identity, row in state['episodes'].items():
        owner = row['target_account']
        for field in ('upload', 'prepared_target'):
            value = row.get(field) or {}
            if value:
                counters[owner, field] += 1
            video_id = value.get('target_video_id')
            if not video_id:
                if field == 'upload' and value:
                    raise ValueError('Completed upload has no target ID')
                continue
            video_id = str(video_id)
            binding = dict(identity=identity, account=owner)
            if video_id in result and result[video_id] != binding:
                raise ValueError('Target ID belongs to multiple episodes or accounts')
            result[video_id] = binding
    return result, counters


def audit(root, generation):
    directory, plan, queue = load(root, generation)
    state = json.loads((directory / 'state.json').read_text())
    baseline_path = root / 'config/migration-baseline.json.gz'
    with gzip.open(baseline_path, 'rt') as handle:
        baseline = json.load(handle)
    if (baseline['generation'] != generation or baseline['account_hashes'] != plan['account_hashes']
            or baseline['manifest_sha256'] != plan['manifest_sha256']):
        raise ValueError('Migration baseline account or frozen queue changed')
    current, counts = targets(state)
    for video_id, owner in baseline['targets'].items():
        if current.get(video_id) != owner:
            raise ValueError('Preserved target ID was lost or moved to another episode/account')
    return dict(generation=generation, queue=len(queue), preserved_target_ids=len(baseline['targets']),
                current_target_ids=len(current), accounts={a: dict(
                    completed=counts[a, 'upload'], pending=counts[a, 'prepared_target'],
                    expected_at_migration=baseline['account_statistics'][a]) for a in ('a', 'b')})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--generation', required=True)
    parser.add_argument('--root', type=Path, default=Path.cwd())
    args = parser.parse_args()
    print(json.dumps(audit(args.root, args.generation)), flush=True)


if __name__ == '__main__':
    main()
