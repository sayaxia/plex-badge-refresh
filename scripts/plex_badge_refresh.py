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
                        help='only C / U / UC / LTX ...')
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
        out('cannot talk to Plex: %s' % exc)
        out('check plex_url / prefs_path in %s' % (cfg.get('_config_file')
                                                   or 'config.json'))
        return 2
    try:
        return args.func(cfg, plex, args)
    except pc.ConfigError as exc:
        out('error: %s' % exc)
        return 2


if __name__ == '__main__':
    sys.exit(main())
