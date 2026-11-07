# -*- coding: utf-8 -*-
"""Refresh MetaTube metadata and pin the new badge poster as the cover.

    python plex_badge_refresh.py scan      [targets]        # what would be hit
    python plex_badge_refresh.py refresh   [targets]        # re-scrape
    python plex_badge_refresh.py covers    [targets]        # switch covers
    python plex_badge_refresh.py verify    [targets]        # check result
    python plex_badge_refresh.py all       [targets]        # refresh+covers+verify

Targets: --list FILE | --item KEY (repeatable) | --recent DAYS
(defaults to the list_file in config.json)
"""
import argparse
import hashlib
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import plex_common as pc  # noqa: E402


def out(text=''):
    sys.stdout.write(text + '\n')
    sys.stdout.flush()


def setup_console():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding='utf-8')
        except Exception:
            pass


# ------------------------------------------------------------------ targets
def build_targets(cfg, plex, args, refresh_index=False):
    if args.item:
        hits, missing = pc.item_targets(plex, args.item), []
    elif args.recent:
        hits, missing = pc.recent_targets(plex, args.recent, args.limit), []
    else:
        list_path = args.list or cfg.get('list_file')
        if not list_path:
            raise pc.ConfigError(
                'no target: pass --list FILE / --item KEY / --recent DAYS, '
                'or set list_file in config.json')
        entries = pc.read_list(list_path)
        wanted = entries if args.all_files else [e for e in entries if e['tag']]
        hits, missing, ambiguous = pc.match_entries(
            wanted, plex.index(refresh_index))
        if ambiguous:
            out('note: %d file(s) had several candidates, used path/first hit'
                % len(ambiguous))
    if args.limit:
        hits = hits[:args.limit]
    return hits, missing


def show_targets(hits, submissions=None):
    out('%-8s %-4s %-30s %-9s %s' % ('key', 'tag', 'file', 'cover now', 'title'))
    out('-' * 84)
    for hit in hits:
        cover = '-'
        if submissions:
            info = submissions.get(pc.ident_of(hit.get('guid')))
            if info:
                cover = pc.badge_label(info[1])
        out('%-8s %-4s %-30s %-9s %s' % (
            hit['key'], hit.get('tag') or '-', (hit['stem'] or '')[:30],
            cover, (hit.get('title') or '')[:28]))


# ------------------------------------------------------------------ refresh
def track(hits, cfg, log_path, timeout_s, quiet=False):
    """Follow the plug-in log until every target has produced a poster line.

    PMS runs plug-in updates strictly one at a time and does not bump
    updatedAt when the agent rewrites metadata, so the log is the only
    completion signal.
    """
    offset = pc.log_size(log_path)
    pending = {h['key']: h for h in hits}
    done = set()
    started = time.time()
    note_at = started
    deadline = started + timeout_s
    while pending and time.time() < deadline:
        size = pc.log_size(log_path)
        if size < offset:  # rotated mid-run
            offset = 0
        text = pc.log_since(log_path, offset)
        if text:
            offset = size
            for line in text.lower().splitlines():
                if ('poster submitted:' not in line
                        and 'failed to download poster' not in line):
                    continue
                for key, hit in list(pending.items()):
                    ident = pc.ident_of(hit.get('guid'))
                    if ident and re.search(r'/%s[?&]' % re.escape(ident), line):
                        pending.pop(key, None)
                        done.add(key)
                        if not quiet:
                            out('    done %-40s (%d left)'
                                % ((hit.get('stem') or '')[:40], len(pending)))
                        break
        if pending:
            time.sleep(4)
            if not quiet and time.time() - note_at >= 30:
                note_at = time.time()
                out('    ... %d/%d done, %d left, %d min elapsed'
                    % (len(done), len(hits), len(pending),
                       int((time.time() - started) / 60)))
    return done, set(pending)


def queue_refreshes(plex, hits):
    for hit in hits:
        try:
            plex.refresh(hit['key'])
        except Exception as exc:
            out('  refresh request failed for %s: %s' % (hit['key'], exc))
        time.sleep(0.1)


# ------------------------------------------------------------------- covers
def apply_covers(plex, hits, submissions, dry_run=False):
    stats = {}
    for pos, hit in enumerate(hits, 1):
        ident = pc.ident_of(hit.get('guid'))
        info = submissions.get(ident)
        if not info:
            state, badge = 'no-log-line', '-'
        else:
            url, badge = info
            try:
                state = plex.pin(hit['key'], url, dry_run=dry_run)
            except Exception as exc:
                state, badge = 'error: %s' % exc, badge
        stats[state] = stats.get(state, 0) + 1
        out('[%2d/%d] %-38s %-14s %s' % (pos, len(hits),
                                         (hit.get('stem') or '')[:38],
                                         state, pc.badge_label(badge)))
    return stats


# ------------------------------------------------------------------- verify
def verify(plex, hits, submissions):
    ok = bad = skip = 0
    out('%-30s %-4s %-9s %s' % ('file', 'tag', 'cover', 'result'))
    out('-' * 70)
    for hit in hits:
        ident = pc.ident_of(hit.get('guid'))
        info = submissions.get(ident)
        if not info:
            out('%-30s %-4s %-9s no agent log' % ((hit['stem'] or '')[:30],
                                                 hit.get('tag') or '-', '-'))
            skip += 1
            continue
        url, badge = info
        digest = hashlib.sha1(url.encode()).hexdigest()
        selected, total = plex.selected_digest(hit['key'])
        expect = pc.expect_for(hit['tag']) if hit.get('tag') else None
        if selected != digest:
            state, bad = 'NOT-SELECTED', bad + 1
        elif expect and badge not in expect:
            state, bad = 'BADGE-OFF', bad + 1
        else:
            state, ok = 'OK', ok + 1
        out('%-30s %-4s %-9s %-12s (%d posters)' % (
            (hit['stem'] or '')[:30], hit.get('tag') or '-',
            pc.badge_label(badge), state, total))
    out('')
    out('OK %d   PROBLEM %d   NO-LOG %d' % (ok, bad, skip))
    return bad


# --------------------------------------------------------------------- main
def cmd_scan(cfg, plex, args):
    hits, missing = build_targets(cfg, plex, args, refresh_index=args.reindex)
    out('targets %d   missing %d' % (len(hits), len(missing)))
    if args.tag_filter:
        hits = [h for h in hits if h.get('tag') == args.tag_filter]
        out('after tag filter: %d' % len(hits))
    out('')
    show_targets(hits, pc.latest_submissions(cfg['plugin_log']))
    if missing:
        out('')
        out('-- not in Plex --')
        for entry in missing:
            out('  ' + entry['path'])
    return 0


def cmd_refresh(cfg, plex, args):
    hits, missing = build_targets(cfg, plex, args, refresh_index=args.reindex)
    out('targets %d   missing %d' % (len(hits), len(missing)))
    if not hits:
        return 0
    if not args.apply:
        show_targets(hits, pc.latest_submissions(cfg['plugin_log']))
        out('')
        out('DRY RUN - add --apply to queue %d refresh request(s)' % len(hits))
        return 0

    started = time.time()
    out('queueing %d refresh request(s) ...' % len(hits))
    out('PMS runs plug-in updates one at a time; a queued request can wait')
    out('several minutes before the agent even starts.')
    queue_refreshes(plex, hits)
    out('waiting for the agent (plug-in log) ...')
    done, pending = track(hits, cfg, cfg['plugin_log'], args.timeout * 60)

    if pending:
        out('')
        out('%d item(s) produced no agent log, retrying via match ...'
            % len(pending))
        retry = [h for h in hits if h['key'] in pending]
        for hit in retry:
            try:
                plex.rematch(hit['key'], hit.get('guid'), hit.get('title'))
            except Exception:
                pass
        again, still = track(retry, cfg, cfg['plugin_log'], args.timeout * 60)
        done |= again
        pending = still

    out('')
    out('refreshed %d / %d in %.1f min'
        % (len(done), len(hits), (time.time() - started) / 60.0))
    if pending:
        for hit in hits:
            if hit['key'] in pending:
                out('  no update: %-40s %s' % ((hit.get('stem') or '')[:40],
                                               (hit.get('title') or '')[:30]))
        out('')
        out('Two causes, in order of likelihood:')
        out('  1. the request is still queued -> raise --timeout (e.g. --timeout 90)')
        out('  2. the image host failed -> look for "Failed to download poster"')
        out('     in %s' % cfg['plugin_log'])
    return 0


def cmd_covers(cfg, plex, args):
    hits, missing = build_targets(cfg, plex, args, refresh_index=args.reindex)
    submissions = pc.latest_submissions(cfg['plugin_log'])
    if not submissions:
        out('warning: no "Poster submitted" line in the plug-in log.')
        out('         the installed MetaTube.bundle is probably the upstream')
        out('         build; the patched one logs every submission.')
    out('targets %d   log entries %d' % (len(hits), len(submissions)))
    stats = apply_covers(plex, hits, submissions, dry_run=not args.apply)
    out('')
    out('summary: ' + ', '.join('%s=%d' % (k, stats[k]) for k in sorted(stats)))
    return 0


def cmd_verify(cfg, plex, args):
    hits, missing = build_targets(cfg, plex, args, refresh_index=args.reindex)
    bad = verify(plex, hits, pc.latest_submissions(cfg['plugin_log']))
    return 1 if bad else 0


def cmd_all(cfg, plex, args):
    hits, missing = build_targets(cfg, plex, args, refresh_index=args.reindex)
    out('targets %d   missing %d' % (len(hits), len(missing)))
    if not hits:
        return 0
    if args.skip_refresh:
        out('skipping the metadata refresh (--skip-refresh)')
    else:
        if not (args.apply or args.yes):
            show_targets(hits, pc.latest_submissions(cfg['plugin_log']))
            out('')
            out('DRY RUN - add --apply to refresh, switch covers and verify')
            return 0
        cmd_refresh(cfg, plex, args)

    submissions = pc.latest_submissions(cfg['plugin_log'])
    out('')
    out('-- switching covers --')
    apply_covers(plex, hits, submissions)
    out('')
    out('-- verifying --')
    bad = verify(plex, hits, submissions)
    return 1 if bad else 0


# ------------------------------------------------------------------- rematch
# When several providers carry the same code, prefer the one the badge work
# already relies on: MGS and FANZA answer with a real poster most of the time,
# aggregator sites (JavBus / JAV321) are slower and sometimes return 500.
PROVIDER_ORDER = ['mgs', 'fanza', 'sod', 'faleno', 'dahlia', 'mywife', 'kin8',
                  '1pondo', '10musume', 'heyzo', 'caribbeancom',
                  'caribbeancompr', 'pacopacomama', 'muramura', 'tokyo-hot',
                  'gcolle', 'pcolle', 'duga', 'getchu', 'ave', 'h0930',
                  'h4610', 'c0930', 'heydouga', '10musume']


def _prefer(result):
    prov = (result.get('provider') or '').lower()
    if prov in PROVIDER_ORDER:
        return (PROVIDER_ORDER.index(prov), prov)
    return (len(PROVIDER_ORDER), prov)


def pick_with_cast(cfg, candidates, prefer_actors=True, tries=3,
                   info_fn=None):
    """Pick a candidate that still has actors when possible.

    Rematching rewrites the cast from the new provider (the plug-in does
    ``metadata.roles.clear()`` first), and several providers simply have no
    actors for amateur titles. Picking by provider order alone therefore
    wipes a cast the item already had; asking for the detail of the first few
    candidates keeps it whenever some other source still knows the names.
    """
    if not candidates:
        return None, None
    first = candidates[0]
    if not prefer_actors:
        return first, None
    info_fn = info_fn or pc.movie_info
    first_actors = None
    for pos, cand in enumerate(candidates[:tries]):
        info = info_fn(cfg, cand.get('provider'), cand.get('id'))
        actors = (info or {}).get('actors') or []
        if pos == 0:
            first_actors = actors
        if actors:
            return cand, actors
    return first, first_actors or []


def cmd_rematch(cfg, plex, args):
    """Re-match items whose guid names a provider the server no longer has.

    Such an item looks fine in Plex but every refresh dies immediately with
    "404 provider not found"; Plex swallows the exception and keeps the old
    metadata, so the item reads as "refresh finished instantly, nothing
    changed".
    """
    valid = pc.server_providers(cfg)
    out('server providers: %d' % len(valid))

    index = plex.index(reload=args.reindex)
    broken = {}
    for rows in index.values():
        for row in rows:
            guid = row.get('guid') or ''
            if cfg['agent'] not in guid:
                continue
            prov = pc.guid_provider(guid)
            if not prov or prov in valid:
                continue
            broken.setdefault(row['key'], row)

    targets = list(broken.values())
    counts = {}
    for row in targets:
        prov = pc.guid_provider(row['guid'])
        counts[prov] = counts.get(prov, 0) + 1
    out('items pointing at a missing provider: %d' % len(targets))
    for prov in sorted(counts, key=lambda p: -counts[p]):
        out('   %-16s %d' % (prov, counts[prov]))
    if args.item:
        wanted = set(str(k) for k in args.item)
        targets = [r for r in targets if r['key'] in wanted]
    if args.limit:
        targets = targets[:args.limit]
    if not args.apply:
        out('')
        out('DRY RUN - add --apply to re-match (%d item(s)).' % len(targets))

    fixed = failed = 0
    no_cast = []
    for pos, row in enumerate(targets, 1):
        old_prov = pc.guid_provider(row['guid'])
        name = (row.get('file') or '').replace('\\', '/').rsplit('/', 1)[-1]
        pick = None
        cast = None
        code = ''
        for cand in pc.code_candidates(row['guid'], row['file']):
            exact = [r for r in pc.search_movies(cfg, cand)
                     if pc.norm_code(r.get('id')) == pc.norm_code(cand)]
            if exact:
                pick, cast = pick_with_cast(
                    cfg, sorted(exact, key=_prefer),
                    prefer_actors=not args.no_prefer_actors,
                    tries=args.cast_tries)
                code = cand
                break
        if not pick:
            failed += 1
            out('[%3d/%3d] %-36s %-9s -> no result' % (pos, len(targets),
                                                        name[:36], old_prov))
            continue
        prov = pick.get('provider') or ''
        ident = pick.get('id') or ''
        if args.apply:
            guid = '%s://%s:%s?lang=zh' % (cfg['agent'], prov, ident)
            plex.rematch(row['key'], guid, row.get('title') or code)
        fixed += 1
        if cast is None:
            note = ''
        elif cast:
            note = '+%d 演员' % len(cast)
        else:
            note = '无演员数据'
            no_cast.append('%s %s' % (name, prov))
        out('[%3d/%3d] %-36s %-9s -> %-9s %-22s %s' % (
            pos, len(targets), name[:36], old_prov, prov, ident[:22], note))

    out('')
    out('%s: %d re-matched, %d without a result'
        % ('APPLIED' if args.apply else 'DRY RUN', fixed, failed))
    if no_cast:
        out('%d 条的新 provider 本身没有演员数据（Plex 原有演员会被清空）：'
            % len(no_cast))
        for line in no_cast:
            out('   %s' % line)
    if args.apply and fixed:
        out('next: run "covers --apply" once the agent has written the posters')
    return 1 if failed else 0


# ----------------------------------------------------------------- providers
def _state_path(cfg, override):
    if override:
        return override
    cfg_file = cfg.get('_config_file') or os.path.join(pc.SKILL_DIR,
                                                       'config.json')
    return os.path.join(os.path.dirname(os.path.abspath(cfg_file)),
                        'provider_state.json')


def _load_state(path):
    try:
        with open(path, 'r', encoding='utf-8') as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _save_state(path, state):
    with open(path, 'w', encoding='utf-8') as handle:
        json.dump(state, handle, ensure_ascii=False, indent=2,
                  sort_keys=True)
        handle.write('\n')


def broken_provider_counts(plex, cfg, index):
    """guid provider -> number of items, for providers the server dropped."""
    valid = pc.server_providers(cfg)
    counts = {}
    for rows in (index or {}).values():
        for row in rows:
            guid = row.get('guid') or ''
            if cfg['agent'] not in guid:
                continue
            prov = pc.guid_provider(guid)
            if not prov or prov in valid:
                continue
            counts[prov] = counts.get(prov, 0) + 1
    return counts


def cmd_providers(cfg, plex, args):
    """Watch the upstream provider registry and what our server serves."""
    token = args.github_token or os.environ.get('GITHUB_TOKEN')
    repo, ref = args.upstream_repo, args.ref
    try:
        head = pc.upstream_head(repo, ref, token)
        commits = pc.upstream_provider_commits(repo, ref, 8, token)
        dirs_default, dirs_exp = pc.upstream_dirs(repo, ref, token)
    except Exception as exc:
        out('cannot read upstream %s@%s: %s' % (repo, ref, exc))
        out('(GitHub API allows 60 anonymous calls/hour; '
            'set GITHUB_TOKEN to raise it)')
        return 2

    names_default = pc.provider_names(dirs_default, repo, ref, token)
    names_exp = pc.provider_names(dirs_exp, repo, ref, token)

    state_path = _state_path(cfg, args.state)
    old = _load_state(state_path)
    old_default = (old or {}).get('default') or {}
    old_exp = (old or {}).get('experimental') or {}
    change = pc.diff_providers(old_default, names_default)
    exp_change = pc.diff_providers(old_exp, names_exp)
    changed = any(change.values()) or any(exp_change.values())

    actor = set()
    try:
        server = sorted(pc.server_providers(cfg))
        actor = pc.server_actor_providers(cfg)
    except Exception as exc:
        out('cannot read server providers: %s' % exc)
        server = []
    view = pc.classify_providers(list(names_default.values()),
                                 list(names_exp.values()),
                                 set(server) | actor)
    plex_counts = {}
    if args.plex and plex is not None:
        plex_counts = broken_provider_counts(plex, cfg, plex.index())

    if args.json:
        payload = {
            'upstream': {'repo': repo, 'ref': ref, 'head': head,
                         'default': names_default,
                         'experimental': names_exp,
                         'commits': commits},
            'change': change, 'experimental_change': exp_change,
            'changed': changed,
            'server': {'providers': server, 'view': view,
                       'broken': plex_counts},
            'state_file': state_path,
        }
        out(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        out('MetaTube 上游 provider 监控')
        out('上游 %s@%s  %s (%s)' % (repo, ref, head['sha'][:7],
                                    (head['date'] or '')[:10]))
        out('  %s' % head['message'])
        out('上游 provider：默认 %d / 实验 %d'
            % (len(names_default), len(names_exp)))
        out('')
        out('上游最近的 provider 提交')
        for row in commits[:6]:
            out('  %s  %-10s %s' % (row['sha'], row['date'], row['message'][:70]))
        out('')
        if old is None:
            out('首次运行：已把当前清单写成基线（%s）' % state_path)
        else:
            out('与上次快照对比（%s）'
                % (old.get('updated') or '未知时间'))
            if not changed:
                out('  provider 集合无变化')
            for directory in change['added']:
                out('  + %-16s 默认池新增（目录 %s）→ 重建镜像即可支持'
                    % (names_default[directory], directory))
            for directory in change['removed']:
                out('  - %-16s 已被上游移除 → 引用它的旧条目会 404，需 rematch'
                    % old_default.get(directory, directory))
            for directory in change['renamed']:
                out('  ~ %-16s %s → %s（API 名称变了，旧 guid 会 404）'
                    % (directory, old_default[directory],
                       names_default[directory]))
            for directory in exp_change['added']:
                out('  + %-16s 实验池新增（%s）→ 需 -tags experimental 重建'
                    % (names_exp[directory], directory))
            for directory in exp_change['removed']:
                out('  - %-16s 实验池移除' % old_exp.get(directory, directory))
            if changed and head['sha'][:7] != (old.get('sha') or '')[:7]:
                out('  上游 HEAD %s → %s'
                    % ((old.get('sha') or '')[:7], head['sha'][:7]))
        out('')
        out('本机服务 provider：%d 个' % len(server))
        if view['missing']:
            out('  缺失上游默认 provider：%s' % '、'.join(view['missing']))
        else:
            out('  缺失上游默认 provider：无')
        if view['experimental_off']:
            out('  实验池未启用：%s（构建时 -tags experimental 才编入）'
                % '、'.join(view['experimental_off']))
        if view['experimental_on']:
            out('  实验池已启用：%s' % '、'.join(view['experimental_on']))
        if view['extra']:
            out('  服务端多出的 provider：%s' % '、'.join(view['extra']))
        if plex_counts or (args.plex and plex is not None):
            out('')
            out('Plex 侧失效 provider 条目')
            if not plex_counts:
                out('  无')
            for prov in sorted(plex_counts, key=lambda p: -plex_counts[p]):
                out('  %-16s %d' % (prov, plex_counts[prov]))
            out('  修复：providers 只负责发现，动数据请用 rematch --apply')

    if args.save_state or changed or old is None:
        _save_state(state_path, {
            'updated': time.strftime('%Y-%m-%d %H:%M'),
            'upstream': '%s@%s' % (repo, ref),
            'sha': head['sha'],
            'default': names_default,
            'experimental': names_exp,
        })
        if not args.json:
            out('')
            out('已更新基线：%s' % state_path)

    if changed and args.fail_on_change:
        return 1
    return 0


def main():
    setup_console()
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument('--config', help='path to config.json')
    common.add_argument('--token', help='X-Plex-Token (default: $PLEX_TOKEN, '
                                       'then Preferences.xml)')
    common.add_argument('--list', help='file with one video path per line')
    common.add_argument('--item', action='append', metavar='KEY',
                        help='Plex ratingKey (repeatable)')
    common.add_argument('--recent', type=int, metavar='DAYS',
                        help='items added in the last N days')
    common.add_argument('--limit', type=int, default=0,
                        help='cap the number of targets')
    common.add_argument('--all-files', action='store_true',
                        help='include files without a badge suffix')
    common.add_argument('--tag-filter', metavar='TAG',
                        help='only C / U / UC / LTX / RESTORED ...')
    common.add_argument('--reindex', action='store_true',
                        help='ignore the in-process Plex index')

    ap = argparse.ArgumentParser(
        description='Refresh MetaTube metadata and pin the new badge poster.')
    sub = ap.add_subparsers(dest='cmd')

    p = sub.add_parser('scan', parents=[common], help='list targets')
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser('refresh', parents=[common], help='re-scrape metadata')
    p.add_argument('--apply', action='store_true')
    p.add_argument('--timeout', type=int, default=45,
                   help='minutes to wait for the agent (default 45)')
    p.set_defaults(func=cmd_refresh)

    p = sub.add_parser('covers', parents=[common], help='switch covers')
    p.add_argument('--apply', action='store_true')
    p.set_defaults(func=cmd_covers)

    p = sub.add_parser('verify', parents=[common], help='check the result')
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser('providers', parents=[common],
                       help='watch the upstream provider registry')
    p.add_argument('--upstream-repo', default=pc.UPSTREAM_REPO)
    p.add_argument('--ref', default=pc.UPSTREAM_REF)
    p.add_argument('--state', help='snapshot file (default: next to config)')
    p.add_argument('--plex', action='store_true',
                   help='also count items stuck on a dropped provider')
    p.add_argument('--json', action='store_true')
    p.add_argument('--github-token', default='',
                   help='default: $GITHUB_TOKEN')
    p.add_argument('--save-state', action='store_true',
                   help='rewrite the snapshot even without changes')
    p.add_argument('--fail-on-change', action='store_true',
                   help='exit 1 when the provider set changed')
    p.set_defaults(func=cmd_providers)

    p = sub.add_parser('rematch', parents=[common],
                       help='re-match items whose provider no longer exists')
    p.add_argument('--apply', action='store_true')
    p.add_argument('--no-prefer-actors', action='store_true',
                   help='pick by provider order even when that source has no cast')
    p.add_argument('--cast-tries', type=int, default=3,
                   help='candidates to probe for actors (default 3)')
    p.set_defaults(func=cmd_rematch)

    p = sub.add_parser('all', parents=[common],
                       help='refresh + covers + verify')
    p.add_argument('--apply', action='store_true')
    p.add_argument('--yes', action='store_true', help=argparse.SUPPRESS)
    p.add_argument('--skip-refresh', action='store_true')
    p.add_argument('--timeout', type=int, default=45)
    p.set_defaults(func=cmd_all)

    args = ap.parse_args()
    if not getattr(args, 'func', None):
        ap.print_help()
        return 2
    if not hasattr(args, 'timeout'):
        args.timeout = 45

    cfg = pc.load_config(args.config)
    try:
        plex = pc.Plex(cfg, token=getattr(args, 'token', None))
    except Exception as exc:
        if args.cmd == 'providers':
            # provider watching only needs the MetaTube API, not Plex
            plex = None
        else:
            out('cannot talk to Plex: %s' % exc)
            out('check plex_url / prefs_path in %s'
                % (cfg.get('_config_file') or 'config.json'))
            return 2
    try:
        return args.func(cfg, plex, args)
    except pc.ConfigError as exc:
        out('error: %s' % exc)
        return 2


if __name__ == '__main__':
    sys.exit(main())
