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
    check('tag _LTX', pc.tag_of('ABP-321_LTX'), 'LTX')
    check('tag __restored', pc.tag_of('ABP-321__restored'), 'RESTORED')
    check('tag -restored', pc.tag_of('ABP-321-restored'), 'RESTORED')
    check('tag _LTX__restored', pc.tag_of('ABP-321_LTX__restored'), 'RESTORED')
    check('no separator -> none', pc.tag_of('FSDSS983C'), None)
    check('plain', pc.tag_of('JUR-891'), None)
    check('expect UC', pc.expect_for('UC'), ('uc.png',))
    check('expect U allows uc', 'uc.png' in pc.expect_for('U'), True)
    check('expect RESTORED is uncensored',
          pc.expect_for('RESTORED'), ('u.png', 'uc.png'))
    check('expect BASICVSR++ is uncensored',
          pc.expect_for('BASICVSR++'), ('u.png', 'uc.png'))


def test_paths():
    check('smb -> volume1',
          pc.smb_to_plex('\\\\nas-server\\Plex\\Movies\\a.mp4'),
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


def test_rematch_codes():
    guid = 'com.plexapp.agents.metatube://MGS:259LUXU-1826?lang=zh'
    check('code from guid', pc.code_candidates(guid, '')[:1], ['259LUXU-1826'])
    check('composite id, tail first',
          pc.code_candidates('com.plexapp.agents.metatube://AVBASE:'
                             'shoroutoclover%3ASTCV-066?lang=zh', '')[:2],
          ['STCV-066', 'shoroutoclover:STCV-066'])
    check('scene prefix stripped',
          '200GANA-1600' in pc.code_candidates('', '/x/[456k.me]200GANA-1600.mp4'),
          True)
    check('quality suffix stripped',
          '259LUXU-1040' in pc.code_candidates('', '/x/259LUXU-1040.FHD.mkv'),
          True)
    check('badge tag stripped',
          'CRNX-231' in pc.code_candidates('', '/x/CRNX-231-U.mp4'), True)
    check('norm_code ignores separators',
          pc.norm_code('259luxu-1826') == pc.norm_code('259LUXU 1826'), True)
    check('provider from guid',
          pc.guid_provider('com.plexapp.agents.metatube://AVBASE:x?id=1'),
          'avbase')


def test_upstream_registry(tmp):
    register = """
import (
	_ "github.com/metatube-community/metatube-sdk-go/provider/fanza"
	_ "github.com/metatube-community/metatube-sdk-go/provider/caribbeancompr"
	_ "github.com/metatube-community/metatube-sdk-go/provider/javbus"
)
"""
    check('parse register.go imports',
          pc.IMPORT_RE.findall(register),
          ['fanza', 'caribbeancompr', 'javbus'])
    source = 'const (\n\tName     = "KIN8"\n\tURL      = "https://x"\n)'
    match = pc.NAME_RE.search(source)
    check('parse Name const', match.group(1) if match else None, 'KIN8')

    old = {'fanza': 'FANZA', 'kin8tengoku': 'KIN8', 'arzon': 'ARZON'}
    new = {'fanza': 'FANZA', 'kin8tengoku': 'KIN8', 'fc2db': 'FC2DB',
           'mgstage': 'MGS'}
    change = pc.diff_providers(old, new)
    check('diff added', change['added'], ['fc2db', 'mgstage'])
    check('diff removed', change['removed'], ['arzon'])
    check('diff renamed', change['renamed'], [])
    check('diff rename detected',
          pc.diff_providers({'jav321': 'JAV321'},
                            {'jav321': 'JAV321-NEW'})['renamed'], ['jav321'])
    check('diff ignores order', pc.diff_providers({'a': 'A'}, {'a': 'A'}),
          {'added': [], 'removed': [], 'renamed': []})

    view = pc.classify_providers(
        ['FANZA', 'KIN8', 'Gfriends'], ['AVBASE', 'MadouQu'],
        {'fanza', 'kin8', 'gfriends', 'javbus'})
    check('classify missing', view['missing'], [])
    check('classify experimental off', view['experimental_off'],
          ['AVBASE', 'MadouQu'])
    check('classify extra', view['extra'], ['javbus'])
    check('classify provider case-insensitive',
          pc.classify_providers(['FanZa'], [], {'fanza'})['missing'], [])

    state = os.path.join(tmp, 'provider_state.json')
    br._save_state(state, {'updated': '2026-11-07 05:20', 'sha': 'abc',
                           'default': {'fanza': 'FANZA'}, 'experimental': {}})
    loaded = br._load_state(state)
    check('state roundtrip', loaded['default'], {'fanza': 'FANZA'})
    check('missing state is None',
          br._load_state(os.path.join(tmp, 'nope.json')), None)
    check('state path next to config',
          os.path.basename(br._state_path(
              {'_config_file': os.path.join(tmp, 'config.json')}, None)),
          'provider_state.json')


def test_pick_with_cast():
    cands = [{'provider': 'MGS', 'id': 'x1'},
             {'provider': 'FANZA', 'id': 'x2'},
             {'provider': 'JavBus', 'id': 'x3'}]
    casts = {('MGS', 'x1'): [], ('FANZA', 'x2'): ['A', 'B'],
             ('JavBus', 'x3'): ['C']}

    def fake_info(_cfg, provider, ident):
        return {'actors': casts.get((provider, ident), [])}

    pick, cast = br.pick_with_cast(None, cands, info_fn=fake_info)
    check('pick prefers a provider with cast', pick['provider'], 'FANZA')
    check('pick reports the cast', cast, ['A', 'B'])

    pick, cast = br.pick_with_cast(None, cands, prefer_actors=False)
    check('no-prefer keeps provider order', pick['provider'], 'MGS')
    check('no-prefer skips probing', cast, None)

    pick, cast = br.pick_with_cast(None, cands, tries=1, info_fn=fake_info)
    check('tries=1 keeps first', pick['provider'], 'MGS')
    check('tries=1 cast empty', cast, [])

    empty = [{'provider': 'MGS', 'id': 'none'}]
    pick, cast = br.pick_with_cast(None, empty, info_fn=fake_info)
    check('single candidate survives', pick['provider'], 'MGS')
    check('single candidate no cast', cast, [])
    check('empty candidate list', br.pick_with_cast(None, [])[0], None)


def main():
    tmp = tempfile.mkdtemp(prefix='plex-badge-')
    try:
        test_badge_rule()
        test_paths()
        test_submissions(tmp)
        test_track(tmp)
        test_track_timeout(tmp)
        test_rematch_codes()
        test_upstream_registry(tmp)
        test_pick_with_cast()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print('\n%d passed, %d failed' % (len(PASS), len(FAIL)))
    return 1 if FAIL else 0


if __name__ == '__main__':
    sys.exit(main())
