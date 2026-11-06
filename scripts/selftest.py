# -*- coding: utf-8 -*-
"""Offline self-test: no Plex, no NAS. Run after editing the scripts.

    python selftest.py
"""
import os
import shutil
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import plex_common as pc  # noqa: E402
import plex_badge_refresh as br  # noqa: E402

PASS = []
FAIL = []


def check(name, got, want):
    ok = got == want
    (PASS if ok else FAIL).append(name)
    print('%-2s %-46s %r' % ('OK' if ok else '!!', name, got))
    if not ok:
        print('   expected %r' % (want,))


def test_badge_rule():
    check('tag -C', pc.tag_of('ABP-123-C'), 'C')
    check('tag -U', pc.tag_of('ABP-123-U'), 'U')
    check('tag -UC', pc.tag_of('ABP-984-UC'), 'UC')
    check('tag -LTX', pc.tag_of('SSIS-724-LTX'), 'LTX')
    check('tag underscore', pc.tag_of('ABC_123_UC'), 'UC')
    check('no separator -> none', pc.tag_of('FSDSS983C'), None)
    check('plain', pc.tag_of('JUR-891'), None)
    check('expect UC', pc.expect_for('UC'), ('uc.png',))
    check('expect U allows uc', 'uc.png' in pc.expect_for('U'), True)


def test_paths():
    check('smb -> volume1',
          pc.smb_to_plex('\\\\192.168.100.221\\Plex\\Movies\\a.mp4'),
          '/volume1/Plex/Movies/a.mp4')
    check('posix passthrough', pc.smb_to_plex('/volume1/x/a.mp4'),
          '/volume1/x/a.mp4')
    check('ident colon',
          pc.ident_of('com.plexapp.agents.metatube://FANZA:ssis00724?lang=zh'),
          'ssis00724')
    check('ident slash',
          pc.ident_of('com.plexapp.agents.metatube://FANZA/ssis465'), 'ssis465')
    check('ident of empty', pc.ident_of(''), None)


def write_log(path, lines):
    with open(path, 'a', encoding='utf-8') as fh:
        for line in lines:
            fh.write(line + '\n')


def test_submissions(tmp):
    log = os.path.join(tmp, 'a.log')
    write_log(log, [
        '2026-11-07 02:00:00 DEBUG (__init__:455) - Poster submitted: '
        'http://h:8080/v1/images/primary/FANZA/abc001?badge=u.png',
        '2026-11-07 02:01:00 DEBUG (__init__:433) - Badge detected: zimu.png',
    ])
    # rotated file is older, the live log must win
    write_log(log + '.1', [
        '2026-11-07 01:00:00 DEBUG (__init__:455) - Poster submitted: '
        'http://h:8080/v1/images/primary/FANZA/abc001?badge=zimu.png',
    ])
    subs = pc.latest_submissions(log)
    check('submission ident', sorted(subs), ['abc001'])
    check('live log wins over .log.1', subs['abc001'][1], 'u.png')

    write_log(log, [
        '2026-11-07 02:05:00 DEBUG (__init__:455) - Poster submitted: '
        'http://h:8080/v1/images/primary/FANZA/abc002?pos=0&badge=uc.png',
    ])
    check('badge after other params',
          pc.latest_submissions(log)['abc002'][1], 'uc.png')


def test_track(tmp):
    log = os.path.join(tmp, 'b.log')
    open(log, 'w').close()
    hit = {'key': '1', 'stem': 'ABC-001-U',
           'guid': 'com.plexapp.agents.metatube://FANZA:abc001?lang=zh'}
    result = {}

    def worker():
        result['value'] = br.track([hit], {}, log, 20, quiet=True)

    thread = threading.Thread(target=worker)
    thread.start()
    time.sleep(1.0)
    write_log(log, [
        '2026-11-07 03:00:00 DEBUG (__init__:200) - Badge detected: u.png',
        '2026-11-07 03:00:01 DEBUG (__init__:455) - Poster submitted: '
        'http://h:8080/v1/images/primary/FANZA/abc001?badge=u.png',
    ])
    thread.join(15)
    done, pending = result.get('value', (set(), set()))
    check('track detects completion', sorted(done), ['1'])
    check('track leaves nothing pending', pending, set())


def test_track_timeout(tmp):
    log = os.path.join(tmp, 'c.log')
    open(log, 'w').close()
    hit = {'key': '9', 'stem': 'X', 'guid': 'x://P:id1'}
    done, pending = br.track([hit], {}, log, 1, quiet=True)
    check('track times out cleanly', (done, sorted(pending)), (set(), ['9']))


def main():
    tmp = tempfile.mkdtemp(prefix='plex-badge-')
    try:
        test_badge_rule()
        test_paths()
        test_submissions(tmp)
        test_track(tmp)
        test_track_timeout(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print('\n%d passed, %d failed' % (len(PASS), len(FAIL)))
    return 1 if FAIL else 0


if __name__ == '__main__':
    sys.exit(main())
