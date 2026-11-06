# -*- coding: utf-8 -*-
"""Shared helpers for the Plex / MetaTube badge workflow.

Everything environment-specific (Plex address, SMB paths, sections) is read
from a JSON config so the same code runs against another NAS unchanged.
"""
import glob
import hashlib
import io
import json
import os
import re
import unicodedata
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_NAME = 'config.json'

DEFAULTS = {
    'plex_url': 'http://127.0.0.1:32400',
    'prefs_path': '',
    'plugin_log': '',
    'agent': 'com.plexapp.agents.metatube',
    'sections': [],
    'share_root': '/volume1',
    'list_file': '',
    'timeout': 90,
}

# The plug-in decides a badge from the file stem: the token has to follow a
# separator, otherwise "FSDSS983C" is just part of the code. Keep this rule
# identical to utils.py, or we would refresh files the agent cannot see.
TAG_RE = re.compile(r'[-_\s](UNCENSORED|UC|C|CH|U|LTX|BASICVSR\+\+|BASICVSR)$',
                    re.IGNORECASE)

VIDEO_EXT = ('.mp4', '.mkv', '.avi', '.wmv', '.mov', '.m4v', '.ts', '.iso',
             '.mpg', '.mpeg', '.flv', '.webm', '.rmvb', '.strm')

# badge -> which images the agent may legitimately render for it. U and LTX
# become uc.png when the folder also carries Chinese subtitles.
BADGE_EXPECT = {
    'C': ('zimu.png',),
    'CH': ('zimu.png',),
    'U': ('u.png', 'uc.png'),
    'UNCENSORED': ('u.png', 'uc.png'),
    'UC': ('uc.png',),
    'LTX': ('u.png', 'uc.png'),
}
BADGE_LABEL = {'zimu.png': '中文字幕', 'u.png': '无码破解', 'uc.png': '中文无码'}

SUBMIT_RE = re.compile(r'Poster submitted: (\S+)')
BADGE_RE = re.compile(r'badge=([^&\s]+)')
IMAGE_RE = re.compile(r'/primary/([^/]+)/([^?]+)')


class ConfigError(Exception):
    pass


def _derive_host(plex_url):
    match = re.match(r'https?://([^:/]+)', plex_url or '')
    return match.group(1) if match else '127.0.0.1'


def load_config(path=None):
    """--config > $PLEX_BADGE_CONFIG > <skill>/config.json > ./.plex-badge.json"""
    candidates = []
    if path:
        candidates.append(path)
    env = os.environ.get('PLEX_BADGE_CONFIG')
    if env:
        candidates.append(env)
    candidates.append(os.path.join(SKILL_DIR, CONFIG_NAME))
    candidates.append(os.path.join(os.getcwd(), '.plex-badge.json'))

    cfg = dict(DEFAULTS)
    used = None
    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            with io.open(candidate, encoding='utf-8-sig') as fh:
                cfg.update(json.load(fh))
            used = candidate
            break
    cfg['_config_file'] = used

    # On a Synology/Docker package install the Preferences.xml and the plug-in
    # log always sit in the same share, so derive them when they are omitted.
    host = _derive_host(cfg.get('plex_url'))
    base = '//%s/Plex/Library/Application Support/Plex Media Server' % host
    if not cfg.get('prefs_path'):
        cfg['prefs_path'] = base + '/Preferences.xml'
    if not cfg.get('plugin_log'):
        cfg['plugin_log'] = (base + '/Logs/PMS Plugin Logs/%s.log'
                             % cfg.get('agent', DEFAULTS['agent']))
    return cfg


def read_token(prefs_path):
    with open(prefs_path, 'rb') as fh:
        raw = fh.read().decode('utf-8', 'replace')
    match = re.search(r'PlexOnlineToken="([^"]+)"', raw)
    if not match:
        raise ConfigError('no PlexOnlineToken in %s' % prefs_path)
    return match.group(1)


def norm(text):
    """Compare-friendly form: NFC, lower case, forward slashes."""
    if not text:
        return ''
    text = unicodedata.normalize('NFC', text)
    return text.replace('\\', '/').rstrip('/').lower()


def is_unc(path):
    return bool(re.match(r'^\s*\\\\', path or ''))


def smb_to_plex(path, share_root='/volume1'):
    """\\\\host\\share\\a\\b.mp4 -> /volume1/share/a/b.mp4"""
    raw = (path or '').strip()
    root = share_root.rstrip('/') or ''
    if root and raw.startswith(root + '/'):
        return raw  # already in Plex's own namespace
    parts = [p for p in re.split(r'[\\/]+', raw) if p]
    if is_unc(raw) and len(parts) > 1:
        parts = parts[1:]  # drop the host segment
    if not parts:
        return ''
    return root + '/' + '/'.join(parts)


def tag_of(stem):
    match = TAG_RE.search(stem or '')
    return match.group(1).upper() if match else None


def expect_for(tag):
    return BADGE_EXPECT.get((tag or '').upper())


class Plex(object):
    def __init__(self, cfg, token=None):
        self.cfg = cfg
        self.url = cfg['plex_url'].rstrip('/')
        self.agent = cfg.get('agent') or DEFAULTS['agent']
        self.timeout = cfg.get('timeout') or 90
        self.token = (token or os.environ.get('PLEX_TOKEN')
                      or read_token(cfg['prefs_path']))
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}))
        self._index = None

    def request(self, path, method='GET', timeout=None):
        req = urllib.request.Request(
            self.url + path, method=method,
            headers={'X-Plex-Token': self.token})
        return self._opener.open(req, timeout=timeout or self.timeout).read()

    def xml(self, path):
        return ET.fromstring(self.request(path))

    # ---------------------------------------------------------------- index
    def section_keys(self):
        keys = self.cfg.get('sections') or []
        if keys:
            return [str(k) for k in keys]
        root = self.xml('/library/sections')
        return [d.get('key') for d in root.findall('Directory')
                if d.get('key')]

    def index(self, reload=False):
        """basename -> [{key, file, title, guid, section}]

        Cached per process: one ``all`` run touches it four times.
        """
        if self._index is not None and not reload:
            return self._index
        index = {}
        for section in self.section_keys():
            try:
                data = self.request('/library/sections/%s/all?includeMedia=1'
                                    % section)
            except Exception as exc:
                print('  section %s skipped: %s' % (section, exc))
                continue
            root = ET.fromstring(data)
            for node in root.findall('Video') + root.findall('Directory'):
                files = []
                for media in node.findall('Media'):
                    for part in media.findall('Part'):
                        if part.get('file'):
                            files.append(part.get('file'))
                if not files:
                    continue
                for path in files:
                    index.setdefault(norm(os.path.basename(path)), []).append({
                        'key': node.get('ratingKey'),
                        'file': path,
                        'title': node.get('title') or '',
                        'guid': node.get('guid') or '',
                        'addedAt': node.get('addedAt') or '',
                        'section': section,
                    })
        self._index = index
        return index

    def metadata(self, key):
        root = self.xml('/library/metadata/%s' % key)
        node = root.find('Video')
        if node is None:
            node = root.find('Directory')
        return node

    def item_files(self, key):
        node = self.metadata(key)
        if node is None:
            return []
        return [p.get('file') for m in node.findall('Media')
                for p in m.findall('Part') if p.get('file')]

    # -------------------------------------------------------------- posters
    def posters(self, key):
        root = self.xml('/library/metadata/%s/posters' % key)
        return [{'ratingKey': p.get('ratingKey') or '',
                 'selected': p.get('selected') == '1',
                 'provider': p.get('provider') or ''}
                for p in root.findall('Photo')]

    def pin(self, key, image_url, dry_run=False):
        """Make the poster entry built from ``image_url`` the selected one.

        Plex names a poster ``<agent>_<sha1(url)>``, so the digest identifies
        the entry without needing the ratingKey that only exists after the
        agent update has been saved.
        """
        digest = hashlib.sha1(image_url.encode()).hexdigest()
        items = self.posters(key)
        hit = [p for p in items if p['ratingKey'].endswith(digest)]
        if hit and hit[0]['selected']:
            return 'ok'
        if dry_run:
            return 'would-switch' if hit else 'poster-missing'
        target = hit[0]['ratingKey'] if hit else image_url
        self.request('/library/metadata/%s/poster?url=%s'
                     % (key, urllib.parse.quote(target, safe='')), method='PUT')
        after = [p for p in self.posters(key)
                 if p['ratingKey'].endswith(digest)]
        if not after:
            return 'poster-missing'
        return 'switched' if after[0]['selected'] else 'switch-failed'

    def selected_digest(self, key):
        sel = [p for p in self.posters(key) if p['selected']]
        if not sel:
            return '', 0
        total = len(self.posters(key))
        return sel[0]['ratingKey'].rsplit('_', 1)[-1], total

    # ------------------------------------------------------------- refresh
    def refresh(self, key):
        """force=1 is required; a bare refresh returns 200 and does nothing."""
        self.request('/library/metadata/%s/refresh?force=1&async=1' % key,
                     method='PUT')

    def rematch(self, key, guid, title):
        self.request('/library/metadata/%s/match?guid=%s&name=%s'
                     % (key, urllib.parse.quote(guid or '', safe=''),
                        urllib.parse.quote(title or '', safe='')), method='PUT')


# ---------------------------------------------------------------- file list
def read_list(path):
    entries = []
    with io.open(path, encoding='utf-8-sig') as fh:
        for raw in fh:
            line = raw.strip().strip('"')
            if not line:
                continue
            ext = os.path.splitext(line)[1].lower()
            if ext not in VIDEO_EXT:
                continue
            stem = os.path.splitext(os.path.basename(line))[0]
            entries.append({'path': line, 'stem': stem, 'ext': ext,
                            'tag': tag_of(stem)})
    return entries


def match_entries(entries, index):
    """Attach Plex items to list entries; exact path wins over basename."""
    hits, missing, ambiguous = [], [], []
    for entry in entries:
        candidates = index.get(norm(os.path.basename(entry['path']))) or []
        if not candidates:
            missing.append(entry)
            continue
        wanted = norm(smb_to_plex(entry['path']))
        exact = [c for c in candidates if norm(c['file']) == wanted]
        pick = exact[0] if exact else candidates[0]
        if len(candidates) > 1 and not exact:
            ambiguous.append((entry, candidates))
        hit = dict(entry)
        hit.update({'key': pick['key'], 'plex_file': pick['file'],
                    'title': pick['title'], 'guid': pick['guid'],
                    'section': pick['section']})
        hits.append(hit)
    return hits, missing, ambiguous


def item_targets(plex, keys):
    """Build targets straight from ratingKeys (files decide the badge)."""
    out = []
    for key in keys:
        files = plex.item_files(key)
        stem = os.path.splitext(os.path.basename(files[0]))[0] if files else ''
        tag = None
        for path in files:
            found = tag_of(os.path.splitext(os.path.basename(path))[0])
            if found:
                tag, stem = found, os.path.splitext(os.path.basename(path))[0]
                break
        node = plex.metadata(key)
        out.append({'key': key, 'stem': stem or key, 'tag': tag,
                    'title': (node.get('title') if node is not None else '') or '',
                    'guid': (node.get('guid') if node is not None else '') or '',
                    'path': files[0] if files else '',
                    'plex_file': files[0] if files else ''})
    return out


def recent_targets(plex, days, limit=0):
    """Items added in the last ``days`` days, newest first."""
    import time
    cutoff = time.time() - days * 86400
    index = plex.index()
    seen = {}
    for rows in index.values():
        for row in rows:
            try:
                added = int(row.get('addedAt') or 0)
            except ValueError:
                added = 0
            if added >= cutoff:
                seen.setdefault(row['key'], row)
    ordered = sorted(seen.values(), key=lambda r: int(r.get('addedAt') or 0),
                     reverse=True)
    if limit:
        ordered = ordered[:limit]
    return item_targets(plex, [r['key'] for r in ordered])


# --------------------------------------------------------------------- log
def log_files(log_path):
    """Newest first: the live log, then .log.1 ... (PMS rotates, .log.1 holds
    the tail of the previous run and often the only evidence of a batch).
    Sorted numerically - a plain string sort puts .log.10 before .log.9."""
    out = []
    if os.path.exists(log_path):
        out.append(log_path)
    rotated = []
    for path in glob.glob(log_path + '.*'):
        match = re.search(r'\.log\.(\d+)$', path)
        if match:
            rotated.append((int(match.group(1)), path))
    out.extend(path for _, path in sorted(rotated))
    return out


def log_size(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def log_since(path, offset):
    try:
        size = log_size(path)
        if offset > size:  # rotated underneath us
            offset = 0
        with open(path, 'rb') as fh:
            fh.seek(offset)
            return fh.read().decode('utf-8', 'replace')
    except (OSError, IOError):
        return ''


def latest_submissions(log_path, tail_bytes=8 * 1024 * 1024):
    """ident -> (image_url, badge) from the newest 'Poster submitted' line.

    Plex silently ignores a poster whose url it already knows, so the entry
    that carries the current badge is *not* necessarily the last one in the
    candidate list - the log is the only reliable source.
    """
    found = {}
    for candidate in log_files(log_path):
        try:
            with open(candidate, 'rb') as fh:
                fh.seek(0, 2)
                size = fh.tell()
                fh.seek(max(0, size - tail_bytes))
                text = fh.read().decode('utf-8', 'replace')
        except (OSError, IOError):
            continue
        # Within one file the last line wins; across files the newest file
        # wins, and log_files() walks newest first - so setdefault is right.
        chunk = {}
        for line in text.splitlines():
            if 'Poster submitted' not in line:
                continue
            match = SUBMIT_RE.search(line)
            if not match:
                continue
            url = match.group(1)
            image = IMAGE_RE.search(url)
            badge = BADGE_RE.search(url)
            if not image or not badge:
                continue
            chunk[image.group(2).lower()] = (url, badge.group(1))
        for ident, value in chunk.items():
            found.setdefault(ident, value)
    return found


def ident_of(guid):
    """com.plexapp.agents.metatube://FANZA:ssis804?lang=zh -> ssis804"""
    match = re.search(r'://[^/:]+[:/]([^?]+)', guid or '')
    return match.group(1).lower() if match else None


def badge_label(badge):
    if not badge:
        return '-'
    return BADGE_LABEL.get(badge, badge)
