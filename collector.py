#!/usr/bin/env python3
"""Bearing collector: reads the RSS/Atom feeds listed in the `sources` table
and saves new items into the private `inbox` table. No AI, no scraping.
Needs two environment variables: SUPABASE_URL and SUPABASE_SERVICE_KEY."""
import os, re, sys, json, html, datetime as dt
import urllib.request, urllib.error
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

UA = 'BearingCollector/1.0 (+https://toniegics.github.io/Bearing-app/)'
NS = {'dc': 'http://purl.org/dc/elements/1.1/', 'atom': 'http://www.w3.org/2005/Atom'}
GENERIC = {'latest news', 'uncategorized', 'hero', 'insights', 'featured'}
STALE_DAYS = 30

# Keyword rules that SUGGEST topics. The editor always has the final say.
TOPIC_RULES = {
    'NSW Surveying': r'\b(nsw|new south wales|sydney|newcastle|wollongong|isnsw|bossi|spatial services|lrs)\b',
    'GNSS': r'\b(gnss|gps|rtk|cors|positioning|galileo|beidou|glonass|satnav)\b',
    'Construction': r'\b(construction|infrastructure|bim|civil|rail|tunnel|bridge|excavation)\b',
    'GIS': r'\b(gis|arcgis|qgis|esri|spatial data|geospatial analytics)\b',
    'AI & automation': r'\b(ai|artificial intelligence|machine learning|automation|automated)\b',
    'Regulation': r'\b(regulations?|regulatory|legislation|standards|registration|land registry|titles office|compliance|mandatory)\b',
    'Drones & LiDAR': r'\b(drones?|uavs?|uncrewed|unmanned|lidar|photogrammetry|point clouds?)\b',
    'Software': r'\b(software|apps?|platform|cloud|arcgis|qgis|autodesk|civil 3d|12d|saas|plugin)\b',
    'Careers': r'\b(jobs?|job vacancies|careers?|graduates?|apprentice(ship)?s?|scholarships?|hiring|vacanc(y|ies)|internships?)\b',
}

def strip_html(h):
    t = re.sub(r'<[^>]+>', ' ', h or '')
    return re.sub(r'\s+', ' ', html.unescape(t)).strip()

def to_utc(s):
    if not s:
        return None
    try:
        d = parsedate_to_datetime(s)
    except Exception:
        try:
            d = dt.datetime.fromisoformat(s.replace('Z', '+00:00'))
        except Exception:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return d.astimezone(dt.timezone.utc).isoformat()

def parse_feed(data):
    root = ET.fromstring(data)
    items = []
    if root.tag.endswith('feed'):  # Atom
        for e in root.findall('atom:entry', NS):
            link = ''
            for l in e.findall('atom:link', NS):
                if l.get('rel', 'alternate') == 'alternate':
                    link = l.get('href', '')
                    break
            items.append(dict(
                guid=(e.findtext('atom:id', '', NS) or link).strip(), url=link.strip(),
                title=strip_html(e.findtext('atom:title', '', NS)),
                published_at=to_utc(e.findtext('atom:published', '', NS) or e.findtext('atom:updated', '', NS)),
                author=(e.findtext('atom:author/atom:name', '', NS) or '').strip(),
                categories=[c.get('term', '') for c in e.findall('atom:category', NS) if c.get('term')],
                excerpt=strip_html(e.findtext('atom:summary', '', NS))[:500]))
    else:  # RSS 2.0
        for it in root.iter('item'):
            g = lambda t: (it.findtext(t) or '').strip()
            desc = strip_html(g('description'))
            desc = re.sub(r'\s*The post .* appeared first on .*$', '', desc)  # WordPress boilerplate
            items.append(dict(
                guid=g('guid') or g('link'), url=g('link'), title=strip_html(g('title')),
                published_at=to_utc(g('pubDate')),
                author=(it.findtext('dc:creator', '', NS) or '').strip(),
                categories=[strip_html(c.text) for c in it.findall('category') if c.text],
                excerpt=desc[:500]))
    return [i for i in items if i['url'].startswith('http') and i['title'] and i['guid']]

def suggest_topics(item):
    text = ' '.join([item['title'], ' '.join(item['categories']), item['excerpt']]).lower()
    return [t for t, rx in TOPIC_RULES.items() if re.search(rx, text)]

def suggest_category(item, topics):
    if topics:
        return topics[0]
    for c in item['categories']:
        if c.lower() not in GENERIC:
            return c
    return 'News'

def flags_for(item, markers):
    seen = [c.lower() for c in item['categories']] + [item['author'].lower()]
    return ['sponsored'] if any(m.lower() in seen for m in markers) else []

# ---- Supabase REST helpers ----
def rest(method, path, body=None, prefer=None):
    url = os.environ['SUPABASE_URL'].rstrip('/') + '/rest/v1/' + path
    key = os.environ['SUPABASE_SERVICE_KEY']
    h = {'apikey': key, 'Content-Type': 'application/json'}
    if not key.startswith('sb_'):  # older JWT-style service keys also go in Authorization
        h['Authorization'] = 'Bearer ' + key
    if prefer:
        h['Prefer'] = prefer
    req = urllib.request.Request(url, method=method, headers=h,
                                 data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=30) as f:
            t = f.read().decode()
            return json.loads(t) if t else None
    except urllib.error.HTTPError as e:
        raise RuntimeError('Supabase %s: %s' % (e.code, e.read().decode()[:300]))

def fetch(url):
    req = urllib.request.Request(url, headers={
        'User-Agent': UA,
        'Accept': 'application/rss+xml, application/atom+xml, application/xml;q=0.9, */*;q=0.5'})
    with urllib.request.urlopen(req, timeout=30) as f:
        return f.read()

def main():
    sources = rest('GET', 'sources?enabled=eq.true&select=*')
    if not sources:
        print('No enabled sources found.')
        return 0
    failures = 0
    now = dt.datetime.now(dt.timezone.utc)
    for s in sources:
        status, new, latest = 'ok', 0, None
        try:
            items = parse_feed(fetch(s['feed_url']))
            if not items:
                raise ValueError('Feed returned no usable items')
            dates = [i['published_at'] for i in items if i['published_at']]
            latest = max(dates) if dates else None
            rows, seen = [], set()
            for i in items:
                if i['guid'] in seen:
                    continue
                seen.add(i['guid'])
                topics = suggest_topics(i)
                rows.append(dict(source_id=s['id'], guid=i['guid'], url=i['url'], title=i['title'],
                                 published_at=i['published_at'], author=i['author'] or None,
                                 categories=i['categories'], excerpt=i['excerpt'],
                                 suggested_topics=topics, suggested_category=suggest_category(i, topics),
                                 flags=flags_for(i, s.get('sponsored_markers') or [])))
            res = rest('POST', 'inbox?on_conflict=source_id,guid', rows,
                       'resolution=ignore-duplicates,return=representation')
            new = len(res or [])
            if latest and (now - dt.datetime.fromisoformat(latest)).days > STALE_DAYS:
                status = 'stale: newest item is %s' % latest[:10]
        except Exception as e:
            status, failures = 'error: %s' % str(e)[:200], failures + 1
        rest('PATCH', 'sources?id=eq.%s' % s['id'],
             {'last_checked': now.isoformat(), 'last_status': status, 'last_item_at': latest})
        print('%s | %s | %d new' % (s['name'], status, new))
    return 1 if failures == len(sources) else 0

if __name__ == '__main__':
    sys.exit(main())
