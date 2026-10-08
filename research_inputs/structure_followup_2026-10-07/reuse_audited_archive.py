"""Share exact local archive copies of already audited, immutable research files."""

import fcntl
import json
import os
from pathlib import Path

from research.data import file_sha256
from research.reporting import write_json
from research.storage import SpaceBudget

HERE=Path(__file__).parent.resolve()
ROOT=HERE.parents[1]


def main():
    plan=json.loads((HERE/'plan.json').read_text())
    budget=SpaceBudget(plan['budget'])
    state=ROOT/'research_inputs/coverage_expansion_2026-10-05/cloud_state'
    archive_root=state/'objects'
    original_root=ROOT/'research_outputs/coverage_expansion_2026-10-05'
    records=[]
    before_usage=budget.check(HERE)['used_bytes']
    with (state/'archive.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        for proof in sorted(original_root.glob('**/independent_coverage_audit.json')):
            run=proof.parent
            manifest=json.loads((run/'manifest.json').read_text())
            if manifest['window']['end'] >= '2026-09-24':
                continue
            if json.loads(proof.read_text())['status']!='passed':
                continue
            for name in ('manifest.json','config_snapshot.json','data_quality.json','result.json.gz'):
                original=run/name
                if not original.is_file():
                    continue
                stat=original.stat()
                if stat.st_size<512*1024:
                    continue
                checksum=file_sha256(original)
                archive=archive_root/checksum[:2]/checksum
                if not archive.is_file():
                    continue
                prior=archive.stat()
                if (stat.st_dev,stat.st_ino)==(prior.st_dev,prior.st_ino) or prior.st_nlink!=1:
                    continue
                if stat.st_uid!=prior.st_uid or stat.st_gid!=prior.st_gid or file_sha256(archive)!=checksum:
                    raise RuntimeError('归档内容或所有者不符')
                partial=archive.with_name(archive.name+'.research-share.partial')
                os.link(original,partial)
                os.replace(partial,archive)
                after=original.stat()
                if (after.st_mode,after.st_uid,after.st_gid,after.st_mtime_ns)!=(stat.st_mode,stat.st_uid,stat.st_gid,stat.st_mtime_ns) or file_sha256(archive)!=checksum:
                    raise RuntimeError('原研究文件的内容或属性改变')
                records.append({'original':str(original),'archive':str(archive),'sha256':checksum,
                                'released_bytes':prior.st_size,'original_permissions_and_mtime_preserved':True})
        fcntl.flock(lock,fcntl.LOCK_UN)
    receipt=HERE/'audited_archive_reuse.json'
    if receipt.exists():
        previous=json.loads(receipt.read_text())['records']
        records=previous+records
    write_json(receipt,{'status':'passed','records':records,'released_bytes':sum(r['released_bytes'] for r in records),
                       'remote_operations':False,'locked_test_read':False},budget)
    print(json.dumps({'status':'passed','files_shared':len(records),'released_bytes':sum(r['released_bytes'] for r in records),
                      'space':budget.check(HERE)},ensure_ascii=False))


if __name__=='__main__':main()
