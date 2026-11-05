"""Archive expired RQ v1 started-job pointers before starting v16 workers.

Run from repository source through the ERP backend's Python stdin. This keeps
job records and their original registry scores intact in an archive sorted set;
it neither deletes nor retries queued work. Active-worker entries are refused.
Without --apply this only reports candidates. Repeated runs are idempotent.
Composite execution IDs and pointers without a surviving job hash are skipped.
"""

import argparse
import json
import os
import time


ARCHIVE = """
local score = redis.call('ZSCORE', KEYS[1], ARGV[1])
if not score or tonumber(score) > tonumber(ARGV[2]) then return 0 end
if redis.call('TYPE', KEYS[3]).ok ~= 'hash' then return 0 end
local worker = redis.call('HGET', KEYS[3], 'worker_name')
if worker and redis.call('EXISTS', 'rq:worker:' .. worker) == 1 then return -1 end
redis.call('ZADD', KEYS[2], score, ARGV[1])
return redis.call('ZREM', KEYS[1], ARGV[1])
"""


def legacy_job_key(member, site):
    """Accept only an unambiguous Frappe v1 site::job pointer.

    RQ v2 appends :execution-id. Legacy custom IDs containing colons are
    deliberately left untouched because their format is ambiguous.
    """
    prefix = (site + "::").encode()
    if not member.startswith(prefix):
        return None
    job_id = member[len(prefix):]
    if not job_id or b":" in job_id:
        return None
    return b"rq:job:" + member


def archive_candidates(redis, site, *, apply=False, cutoff=None):
    cutoff = int(time.time()) if cutoff is None else cutoff
    counts = {"candidates": 0, "archived": 0}
    for registry in redis.scan_iter("rq:wip:*"):
        for member in redis.zrangebyscore(registry, "-inf", cutoff):
            job_key = legacy_job_key(member, site)
            if job_key is None or redis.type(job_key) != b"hash":
                continue
            counts["candidates"] += 1
            if apply:
                result = redis.eval(ARCHIVE, 3, registry,
                                    b"frappe:v16-upgrade:legacy-started:" + registry,
                                    job_key, member, cutoff)
                if result == -1:
                    raise RuntimeError("Legacy entry still belongs to a registered worker; stop it first")
                counts["archived"] += result
    return counts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    os.chdir("/home/frappe/frappe-bench/sites")
    import frappe
    from frappe.utils.background_jobs import get_redis_conn

    frappe.init(site=args.site)
    try:
        redis = get_redis_conn()
        print(json.dumps(archive_candidates(redis, args.site, apply=args.apply)))
    finally:
        frappe.destroy()


if __name__ == "__main__":
    main()
