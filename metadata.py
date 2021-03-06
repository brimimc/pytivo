#!/usr/bin/env python

import os
import subprocess
import sys
from datetime import datetime
from xml.dom import minidom
from xml.parsers import expat
try:
    import plistlib
except:
    pass

import mutagen
from lrucache import LRUCache

import config
import plugins.video.transcode

# Something to strip
TRIBUNE_CR = ' Copyright Tribune Media Services, Inc.'

TV_RATINGS = {'TV-Y7': 1, 'TV-Y': 2, 'TV-G': 3, 'TV-PG': 4, 'TV-14': 5,
              'TV-MA': 6, 'TV-NR': 7, 'TVY7': 1, 'TVY': 2, 'TVG': 3,
              'TVPG': 4, 'TV14': 5, 'TVMA': 6, 'TVNR': 7, 'Y7': 1,
              'Y': 2, 'G': 3, 'PG': 4, '14': 5, 'MA': 6, 'NR': 7,
              'UNRATED': 7, 'X1': 1, 'X2': 2, 'X3': 3, 'X4': 4, 'X5': 5,
              'X6': 6, 'X7': 7}

MPAA_RATINGS = {'G': 1, 'PG': 2, 'PG-13': 3, 'PG13': 3, 'R': 4, 'X': 5,
                'NC-17': 6, 'NC17': 6, 'NR': 8, 'UNRATED': 8, 'G1': 1,
                'P2': 2, 'P3': 3, 'R4': 4, 'X5': 5, 'N6': 6, 'N8': 8}

STAR_RATINGS = {'1': 1, '1.5': 2, '2': 3, '2.5': 4, '3': 5, '3.5': 6,
                '4': 7, '*': 1, '**': 3, '***': 5, '****': 7, 'X1': 1,
                'X2': 2, 'X3': 3, 'X4': 4, 'X5': 5, 'X6': 6, 'X7': 7}

HUMAN = {'mpaaRating': {1: 'G', 2: 'PG', 3: 'PG-13', 4: 'R', 5: 'X',
                        6: 'NC-17', 8: 'NR'},
         'tvRating': {1: 'Y7', 2: 'Y', 3: 'G', 4: 'PG', 5: '14',
                      6: 'MA', 7: 'NR'},
         'starRating': {1: '1', 2: '1.5', 3: '2', 4: '2.5', 5: '3',
                        6: '3.5', 7: '4'}}

BOM = '\xef\xbb\xbf'

tivo_cache = LRUCache(50)
mp4_cache = LRUCache(50)
dvrms_cache = LRUCache(50)
nfo_cache = LRUCache(50)

mswindows = (sys.platform == "win32")

def get_mpaa(rating):
    return HUMAN['mpaaRating'].get(rating, 'NR')

def get_tv(rating):
    return HUMAN['tvRating'].get(rating, 'NR')

def get_stars(rating):
    return HUMAN['starRating'].get(rating, '')

def tag_data(element, tag):
    for name in tag.split('/'):
        new_element = element.getElementsByTagName(name)
        if not new_element:
            return ''
        element = new_element[0]
    if not element.firstChild:
        return ''
    return element.firstChild.data

def _vtag_data(element, tag):
    for name in tag.split('/'):
        new_element = element.getElementsByTagName(name)
        if not new_element:
            return []
        element = new_element[0]
    elements = element.getElementsByTagName('element')
    return [x.firstChild.data for x in elements if x.firstChild]

def _vtag_data_alternate(element, tag):
    elements = [element]
    for name in tag.split('/'):
        new_elements = []
        for elmt in elements:
            new_elements += elmt.getElementsByTagName(name)
        elements = new_elements
    return [x.firstChild.data for x in elements if x.firstChild]

def _tag_value(element, tag):
    item = element.getElementsByTagName(tag)
    if item:
        value = item[0].attributes['value'].value
        return int(value[0])

def from_moov(full_path):
    if full_path in mp4_cache:
        return mp4_cache[full_path]

    metadata = {}
    len_desc = 0

    try:
        mp4meta = mutagen.File(unicode(full_path, 'utf-8'))
        assert(mp4meta)
    except:
        mp4_cache[full_path] = {}
        return {}

    # The following 1-to-1 correspondence of atoms to pyTivo
    # variables is TV-biased
    keys = {'tvnn': 'callsign', 'tven': 'episodeNumber',
            'tvsh': 'seriesTitle'}

    for key, value in mp4meta.items():
        if type(value) == list:
            value = value[0]
        if key == 'stik':
            metadata['isEpisode'] = ['false', 'true'][value == 'TV Show']
        elif key in keys:
            metadata[keys[key]] = value
        # These keys begin with the copyright symbol \xA9
        elif key == '\xa9day':
            if len(value) == 4:
                value += '-01-01T16:00:00Z'
            metadata['originalAirDate'] = value
            #metadata['time'] = value
        elif key in ['\xa9gen', 'gnre']:
            for k in ('vProgramGenre', 'vSeriesGenre'):
                if k in metadata:
                    metadata[k].append(value)
                else:
                    metadata[k] = [value]
        elif key == '\xa9nam':
            if 'tvsh' in mp4meta:
                metadata['episodeTitle'] = value
            else:
                metadata['title'] = value

        # Description in desc, cmt, and/or ldes tags. Keep the longest.
        elif key in ['desc', '\xa9cmt', 'ldes'] and len(value) > len_desc:
            metadata['description'] = value
            len_desc = len(value)

        # A common custom "reverse DNS format" tag
        elif (key == '----:com.apple.iTunes:iTunEXTC' and
              ('us-tv' in value or 'mpaa' in value)):
            rating = value.split("|")[1].upper()
            if rating in TV_RATINGS and 'us-tv' in value:
                metadata['tvRating'] = TV_RATINGS[rating]
            elif rating in MPAA_RATINGS and 'mpaa' in value:
                metadata['mpaaRating'] = MPAA_RATINGS[rating]

        # Actors, directors, producers, AND screenwriters may be in a long
        # embedded XML plist.
        elif (key == '----:com.apple.iTunes:iTunMOVI' and
              'plistlib' in sys.modules):
            items = {'cast': 'vActor', 'directors': 'vDirector',
                     'producers': 'vProducer', 'screenwriters': 'vWriter'}
            data = plistlib.readPlistFromString(value)
            for item in items:
                if item in data:
                    metadata[items[item]] = [x['name'] for x in data[item]]

    mp4_cache[full_path] = metadata
    return metadata

def from_mscore(rawmeta):
    metadata = {}
    keys = {'title': ['Title'],
            'description': ['Description', 'WM/SubTitleDescription'],
            'episodeTitle': ['WM/SubTitle'],
            'callsign': ['WM/MediaStationCallSign'],
            'displayMajorNumber': ['WM/MediaOriginalChannel'],
            'originalAirDate': ['WM/MediaOriginalBroadcastDateTime'],
            'rating': ['WM/ParentalRating'],
            'credits': ['WM/MediaCredits'], 'genre': ['WM/Genre']}

    for tagname in keys:
        for tag in keys[tagname]:
            try:
                if tag in rawmeta:
                    value = rawmeta[tag][0]
                    if type(value) not in (str, unicode):
                        value = str(value)
                    if value:
                        metadata[tagname] = value
            except:
                pass

    if 'episodeTitle' in metadata and 'title' in metadata:
        metadata['seriesTitle'] = metadata['title']
    if 'genre' in metadata:
        value = metadata['genre'].split(',')
        metadata['vProgramGenre'] = value
        metadata['vSeriesGenre'] = value
        del metadata['genre']
    if 'credits' in metadata:
        value = [x.split('/') for x in metadata['credits'].split(';')]
        if len(value) > 3:
            metadata['vActor'] = [x for x in (value[0] + value[3]) if x]
            metadata['vDirector'] = [x for x in value[1] if x]
        del metadata['credits']
    if 'rating' in metadata:
        rating = metadata['rating']
        if rating in TV_RATINGS:
            metadata['tvRating'] = TV_RATINGS[rating]
        del metadata['rating']

    return metadata

def from_dvrms(full_path):
    if full_path in dvrms_cache:
        return dvrms_cache[full_path]

    try:
        rawmeta = mutagen.File(unicode(full_path, 'utf-8'))
        assert(rawmeta)
    except:
        dvrms_cache[full_path] = {}
        return {}

    metadata = from_mscore(rawmeta)
    dvrms_cache[full_path] = metadata
    return metadata

def from_eyetv(full_path):
    keys = {'TITLE': 'title', 'SUBTITLE': 'episodeTitle',
            'DESCRIPTION': 'description', 'YEAR': 'movieYear',
            'EPISODENUM': 'episodeNumber'}
    metadata = {}
    path = os.path.dirname(unicode(full_path, 'utf-8'))
    eyetvp = [x for x in os.listdir(path) if x.endswith('.eyetvp')][0]
    eyetvp = os.path.join(path, eyetvp)
    eyetv = plistlib.readPlist(eyetvp)
    if 'epg info' in eyetv:
        info = eyetv['epg info']
        for key in keys:
            if info[key]:
                metadata[keys[key]] = info[key]
        if info['SUBTITLE']:
            metadata['seriesTitle'] = info['TITLE']
        if info['ACTORS']:
            metadata['vActor'] = [x.strip() for x in info['ACTORS'].split(',')]
        if info['DIRECTOR']:
            metadata['vDirector'] = [info['DIRECTOR']]

        for ptag, etag, ratings in [('tvRating', 'TV_RATING', TV_RATINGS),
                              ('mpaaRating', 'MPAA_RATING', MPAA_RATINGS),
                              ('starRating', 'STAR_RATING', STAR_RATINGS)]:
            x = info[etag].upper()
            if x and x in ratings:
                metadata[ptag] = ratings[x]

        # movieYear must be set for the mpaa/star ratings to work
        if (('mpaaRating' in metadata or 'starRating' in metadata) and
            'movieYear' not in metadata):
            metadata['movieYear'] = eyetv['info']['start'].year
    return metadata

def from_text(full_path):
    metadata = {}
    full_path = unicode(full_path, 'utf-8')
    path, name = os.path.split(full_path)
    title, ext = os.path.splitext(name)

    search_paths = []
    ptmp = full_path
    while ptmp:
        parent = os.path.dirname(ptmp)
        if ptmp != parent:
            ptmp = parent
        else:
            break
        search_paths.append(os.path.join(ptmp, 'default.txt'))

    search_paths.append(os.path.join(path, title) + '.properties')
    search_paths.reverse()

    search_paths += [full_path + '.txt',
                     os.path.join(path, '.meta', 'default.txt'),
                     os.path.join(path, '.meta', name) + '.txt']

    for metafile in search_paths:
        if os.path.exists(metafile):
            sep = ':='[metafile.endswith('.properties')]
            for line in file(metafile, 'U'):
                if line.startswith(BOM):
                    line = line[3:]
                if line.strip().startswith('#') or not sep in line:
                    continue
                key, value = [x.strip() for x in line.split(sep, 1)]
                if not key or not value:
                    continue
                if key.startswith('v'):
                    if key in metadata:
                        metadata[key].append(value)
                    else:
                        metadata[key] = [value]
                else:
                    metadata[key] = value

    for rating, ratings in [('tvRating', TV_RATINGS),
                            ('mpaaRating', MPAA_RATINGS),
                            ('starRating', STAR_RATINGS)]:
        x = metadata.get(rating, '').upper()
        if x in ratings:
            metadata[rating] = ratings[x]

    return metadata

def basic(full_path):
    base_path, name = os.path.split(full_path)
    title, ext = os.path.splitext(name)
    mtime = os.stat(unicode(full_path, 'utf-8')).st_mtime
    if (mtime < 0):
        mtime = 0
    originalAirDate = datetime.utcfromtimestamp(mtime)

    metadata = {'title': title,
                'originalAirDate': originalAirDate.isoformat()}
    ext = ext.lower()
    if ext in ['.mp4', '.m4v', '.mov']:
        metadata.update(from_moov(full_path))
    elif ext in ['.dvr-ms', '.asf', '.wmv']:
        metadata.update(from_dvrms(full_path))
    elif 'plistlib' in sys.modules and base_path.endswith('.eyetv'):
        metadata.update(from_eyetv(full_path))
    metadata.update(from_nfo(full_path))
    metadata.update(from_text(full_path))

    return metadata

def from_container(xmldoc):
    metadata = {}

    keys = {'title': 'Title', 'episodeTitle': 'EpisodeTitle',
            'description': 'Description', 'seriesId': 'SeriesId',
            'episodeNumber': 'EpisodeNumber', 'tvRating': 'TvRating',
            'displayMajorNumber': 'SourceChannel', 'callsign': 'SourceStation',
            'showingBits': 'ShowingBits', 'mpaaRating': 'MpaaRating'}

    details = xmldoc.getElementsByTagName('Details')[0]

    for key in keys:
        data = tag_data(details, keys[key])
        if data:
            if key == 'description':
                data = data.replace(TRIBUNE_CR, '')
            elif key == 'tvRating':
                data = int(data)
            elif key == 'displayMajorNumber':
                if '-' in data:
                    data, metadata['displayMinorNumber'] = data.split('-')
            metadata[key] = data

    return metadata

def from_details(xml):
    metadata = {}

    xmldoc = minidom.parse(xml)
    showing = xmldoc.getElementsByTagName('showing')[0]
    program = showing.getElementsByTagName('program')[0]

    items = {'description': 'program/description',
             'title': 'program/title',
             'episodeTitle': 'program/episodeTitle',
             'episodeNumber': 'program/episodeNumber',
             'seriesId': 'program/series/uniqueId',
             'seriesTitle': 'program/series/seriesTitle',
             'originalAirDate': 'program/originalAirDate',
             'isEpisode': 'program/isEpisode',
             'movieYear': 'program/movieYear',
             'partCount': 'partCount',
             'partIndex': 'partIndex',
             'time': 'time'}

    for item in items:
        data = tag_data(showing, items[item])
        if data:
            if item == 'description':
                data = data.replace(TRIBUNE_CR, '')
            metadata[item] = data

    vItems = ['vActor', 'vChoreographer', 'vDirector',
              'vExecProducer', 'vProgramGenre', 'vGuestStar',
              'vHost', 'vProducer', 'vWriter']

    for item in vItems:
        data = _vtag_data(program, item)
        if data:
            metadata[item] = data

    sb = showing.getElementsByTagName('showingBits')
    if sb:
        metadata['showingBits'] = sb[0].attributes['value'].value

    #for tag in ['starRating', 'mpaaRating', 'colorCode']:
    for tag in ['starRating', 'mpaaRating']:
        value = _tag_value(program, tag)
        if value:
            metadata[tag] = value

    rating = _tag_value(showing, 'tvRating')
    if rating:
        metadata['tvRating'] = rating

    return metadata

def _nfo_vitems(source, metadata):

    vItems = {'vGenre': 'genre',
              'vWriter': 'credits',
              'vDirector': 'director',
              'vActor': 'actor/name'}

    for key in vItems:
        data = _vtag_data_alternate(source, vItems[key])
        if data:
            metadata.setdefault(key, [])
            for dat in data:
                if not dat in metadata[key]:
                    metadata[key].append(dat)

    if 'vGenre' in metadata:
        metadata['vSeriesGenre'] = metadata['vProgramGenre'] = metadata['vGenre']

    return metadata

def _parse_nfo(nfo_path, nfo_data=None):
    # nfo files can contain XML or a URL to seed the XBMC metadata scrapers
    # It's also possible to have both (a URL after the XML metadata)
    # pyTivo only parses the XML metadata, but we'll try to stip the URL
    # from mixed XML/URL files.  Returns `None` when XML can't be parsed.
    if nfo_data is None:
        nfo_data = [line.strip() for line in file(nfo_path, 'rU')]
    xmldoc = None
    try:
        xmldoc = minidom.parseString(os.linesep.join(nfo_data))
    except expat.ExpatError, err:
        if expat.ErrorString(err.code) == expat.errors.XML_ERROR_INVALID_TOKEN:
            # might be a URL outside the xml
            while len(nfo_data) > err.lineno:
                if len(nfo_data[-1]) == 0:
                    nfo_data.pop()
                else:
                    break
            if len(nfo_data) == err.lineno:
                # last non-blank line contains the error
                nfo_data.pop()
                return _parse_nfo(nfo_path, nfo_data)
    return xmldoc

def _from_tvshow_nfo(tvshow_nfo_path):
    if tvshow_nfo_path in nfo_cache:
        return nfo_cache[tvshow_nfo_path]

    items = {'description': 'plot',
             'title': 'title',
             'seriesTitle': 'showtitle',
             'starRating': 'rating',
             'tvRating': 'mpaa'}

    nfo_cache[tvshow_nfo_path] = metadata = {}

    xmldoc = _parse_nfo(tvshow_nfo_path)
    if not xmldoc:
        return metadata

    tvshow = xmldoc.getElementsByTagName('tvshow')
    if tvshow:
        tvshow = tvshow[0]
    else:
        return metadata

    for item in items:
        data = tag_data(tvshow, items[item])
        if data:
            metadata[item] = data

    metadata = _nfo_vitems(tvshow, metadata)

    nfo_cache[tvshow_nfo_path] = metadata
    return metadata

def _from_episode_nfo(nfo_path, xmldoc):
    metadata = {}

    items = {'description': 'plot',
             'episodeTitle': 'title',
             'seriesTitle': 'showtitle',
             'originalAirDate': 'aired',
             'starRating': 'rating',
             'tvRating': 'mpaa'}

    # find tvshow.nfo
    path = nfo_path
    while True:
        basepath = os.path.dirname(path)
        if path == basepath:
            break
        path = basepath
        tv_nfo = os.path.join(path, 'tvshow.nfo')
        if os.path.exists(tv_nfo):
            metadata.update(_from_tvshow_nfo(tv_nfo))
            break

    episode = xmldoc.getElementsByTagName('episodedetails')
    if episode:
        episode = episode[0]
    else:
        return metadata

    metadata['isEpisode'] = 'true'
    for item in items:
        data = tag_data(episode, items[item])
        if data:
            metadata[item] = data

    season = tag_data(episode, 'displayseason')
    if not season or season == "-1":
        season = tag_data(episode, 'season')
    if not season:
        season = 1

    ep_num = tag_data(episode, 'displayepisode')
    if not ep_num or ep_num == "-1":
        ep_num = tag_data(episode, 'episode')
    if ep_num and ep_num != "-1":
        metadata['episodeNumber'] = "%d%02d" % (int(season), int(ep_num))

    if 'originalAirDate' in metadata:
        metadata['originalAirDate'] += 'T00:00:00Z'

    metadata = _nfo_vitems(episode, metadata)

    return metadata

def _from_movie_nfo(xmldoc):
    metadata = {}

    movie = xmldoc.getElementsByTagName('movie')
    if movie:
        movie = movie[0]
    else:
        return metadata

    items = {'description': 'plot',
             'title': 'title',
             'movieYear': 'year',
             'starRating': 'rating',
             'mpaaRating': 'mpaa'}

    metadata['isEpisode'] = 'false'

    for item in items:
        data = tag_data(movie, items[item])
        if data:
            metadata[item] = data

    metadata['movieYear'] = "%04d" % int(metadata.get('movieYear', 0))

    metadata = _nfo_vitems(movie, metadata)
    return metadata

def from_nfo(full_path):
    if full_path in nfo_cache:
        return nfo_cache[full_path]

    metadata = nfo_cache[full_path] = {}

    nfo_path = "%s.nfo" % os.path.splitext(full_path)[0]
    if not os.path.exists(nfo_path):
        return metadata

    xmldoc = _parse_nfo(nfo_path)
    if not xmldoc:
        return metadata

    if xmldoc.getElementsByTagName('episodedetails'):
        # it's an episode
        metadata.update(_from_episode_nfo(nfo_path, xmldoc))
    elif xmldoc.getElementsByTagName('movie'):
        # it's a movie
        metadata.update(_from_movie_nfo(xmldoc))

    # common nfo cleanup
    if 'starRating' in metadata:
        # .NFO 0-10 -> TiVo 1-7
        rating = int(float(metadata['starRating']) * 6 / 10 + 1.5)
        metadata['starRating'] = rating

    for key, mapping in [('mpaaRating', MPAA_RATINGS),
                         ('tvRating', TV_RATINGS)]:
        if key in metadata:
            rating = mapping.get(metadata[key], None)
            if rating:
                metadata[key] = str(rating)
            else:
                del metadata[key]

    nfo_cache[full_path] = metadata
    return metadata

def from_tivo(full_path):
    if full_path in tivo_cache:
        return tivo_cache[full_path]

    tdcat_path = config.get_bin('tdcat')
    tivo_mak = config.get_server('tivo_mak')
    try:
        assert(tdcat_path and tivo_mak)
        fname = unicode(full_path, 'utf-8')
        if mswindows:
            fname = fname.encode('iso8859-1')
        tcmd = [tdcat_path, '-m', tivo_mak, '-2', fname]
        tdcat = subprocess.Popen(tcmd, stdout=subprocess.PIPE)
        metadata = from_details(tdcat.stdout)
        tivo_cache[full_path] = metadata
    except:
        metadata = {}

    return metadata

def force_utf8(text):
    if type(text) == str:
        try:
            text = text.decode('utf8')
        except:
            if sys.platform == 'darwin':
                text = text.decode('macroman')
            else:
                text = text.decode('iso8859-1')
    return text.encode('utf-8')

def dump(output, metadata):
    for key in metadata:
        value = metadata[key]
        if type(value) == list:
            for item in value:
                output.write('%s: %s\n' % (key, item.encode('utf-8')))
        else:
            if key in HUMAN and value in HUMAN[key]:
                output.write('%s: %s\n' % (key, HUMAN[key][value]))
            else:
                output.write('%s: %s\n' % (key, value.encode('utf-8')))

if __name__ == '__main__':
    if len(sys.argv) > 1:
        metadata = {}
        fname = force_utf8(sys.argv[1])
        ext = os.path.splitext(fname)[1].lower()
        if ext == '.tivo':
            config.init([])
            metadata.update(from_tivo(fname))
        elif ext in ['.mp4', '.m4v', '.mov']:
            metadata.update(from_moov(fname))
        elif ext in ['.dvr-ms', '.asf', '.wmv']:
            metadata.update(from_dvrms(fname))
        elif ext == '.wtv':
            vInfo = plugins.video.transcode.video_info(fname)
            metadata.update(from_mscore(vInfo['rawmeta']))
        dump(sys.stdout, metadata)
