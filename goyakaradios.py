import ConfigParser
import calendar
import json
import time
from collections import defaultdict
from datetime import datetime
from httplib import HTTPException
from httplib2 import Http
from os import environ
from os.path import join, dirname
from re import compile as re_compile, findall, VERBOSE
from socket import error as SocketError
from time import sleep
from urllib import urlencode
from urllib2 import HTTPError, urlopen
from webapp2 import RequestHandler, WSGIApplication

from apiclient.discovery import build
from apiclient.errors import HttpError
from google.appengine.api import memcache, taskqueue, users
from google.appengine.ext import db
from oauth2client.client import AccessTokenRefreshError
from oauth2client.appengine import CredentialsModel, StorageByKeyName

from appengine_override import \
    OAuth2DecoratorFromClientSecrets_ApprovalPromptForce

CONFIG_FILE = join(dirname(__file__), 'config', 'fb_properties.cfg')
CLIENT_SECRETS = join(dirname(__file__), 'client_secrets.json')
YOUTUBE_RW_SCOPE = "https://www.googleapis.com/auth/youtube"
YOUTUBE_API_SERVICE_NAME = "youtube"
YOUTUBE_API_VERSION = "v3"
YOUTUBE_MAX_VIDEOS_PER_PLAYLIST = 200
YOUTUBE = build(YOUTUBE_API_SERVICE_NAME, YOUTUBE_API_VERSION)
# Default OAuth2DecoratorFromClientSecrets, but it fails to forward/
# honor additional **kwargs like approval_prompt='force', and it *has* to be
# set at init time, so I built this slightly modified version, which just
# adds one parameter. Better ways to do that very welcome.
DECORATOR = OAuth2DecoratorFromClientSecrets_ApprovalPromptForce(\
                                            CLIENT_SECRETS, YOUTUBE_RW_SCOPE)

'''
DataStore schemas used
'''

class MonthlyPlaylist(db.Model):
    '''
    DataStorage class to store the monthly playlist ID(s), Names (for namesake) along with 
    the number of videos currently in the playlist
    '''
    id = db.StringProperty()
    name = db.StringProperty()
    epochVal = db.IntegerProperty(required=True)
    date = db.DateTimeProperty()
    counter = db.IntegerProperty(default=0)

class GaeUser(db.Model):
    '''DataStorage class used to persist the user ID for cron tasks.'''
    id = db.StringProperty(required=True)
    date = db.DateTimeProperty()

class FBGroupPost(db.Model):
    '''
    DataStorage Class to store the top and bottom bounds of the fb posts
    to be used by since and until parameters in Graph API along with their timestamp
    recent_post: latest post indexed
    oldest_post: oldest post indexed
    '''
    recent_post = db.StringProperty(default='1405546110')
    oldest_post = db.StringProperty(default='1405546110')


'''
Helper Tools
'''

def getPostId(postType="since"):
    '''
    Module to return the since Id or until Id based on the type 
    of the cron job being run
    '''
    q = db.GqlQuery("SELECT * FROM FBGroupPost")
    acc = q.get()
    obj = db.get(acc.key())
    result = None
    if(postType == "since"):
        result = obj.recent_post
    else:
        result = obj.oldest_post
    return result

def setPostId(id, iscron=True):
    '''
    Module to set the since Id or until Id based on the type of
    cron job being run
    '''
    q = db.GqlQuery("SELECT * FROM FBGroupPost")
    acc = q.get()
    obj = db.get(acc.key())
    if iscron:
        obj.recent_post = id
    else:
        obj.oldest_post = id
    obj.put()

def parse_date(datestring):
    '''
    Extract the month and year from the datestring
    '''
    dateStr = datestring.split('T')[0]
    year = dateStr.split('-')[0]
    month = dateStr.split('-')[1]
    mmyy = month + '-' + year
    return mmyy

def toEpoch(givenTime):
    '''
    Convert Fb created_time to Epoch
    '''
    result = int(time.mktime(time.strptime(givenTime, '%Y-%m-%dT%H:%M:%S+0000'))) - time.timezone
    return str(result)

def getMonthinEpoch(mmyyString):
    '''
    Convert mm-yyyy values to epoch value
    '''
    tmpTime = '01-' + mmyyString
    result = int(time.mktime(time.strptime(tmpTime, '%d-%m-%Y'))) - time.timezone
    print 'epoch for ' + mmyyString + ' is ' + str(result)
    return result


def createPlaylistName(mmyyString, cntr=0):
    '''
    Create playlist Name from mm-yy value sent
    '''
    month = calendar.month_name[int(mmyyString.split('-')[0])]
    year = mmyyString.split('-')[1]
    playlistName = month + ', ' + year + ' #' + str(cntr)
    return playlistName


class FbHelper:
    '''
    Class and related functions to retrieve data using FB API 
    '''
    def __init__(self):
        print "Initializing FbHelper..."
        self.config = ConfigParser.ConfigParser()
        try:
            self.config.read(CONFIG_FILE)
        except:
            print 'Config file:'+str(CONFIG_FILE)+' cannot be read'
            return
        self.group_url = self.config.get('group','url')

    def process_data(self, isCron=True):
        print 'fbHelper:process_data: init'
        if not isCron:
            result = self.process_until_data()
        else:
            result = self.process_since_data()
        print 'fbHelper:process_data: end'
        return result
    
    def process_since_data(self):
        '''
        Since Logic, first get the first link with since=sinceId afterwards fetch next pages till
        all urls have been accessed using since=sinceId and paging.next url
        returns (sinceId, dic( mm-yy : <List of videos>))
        '''
        videos = defaultdict(list)
        sinceId = getPostId()
        print 'sinceId is ' + sinceId
        newSinceId = ''
        group_url = self.group_url
        additional_url_params = urlencode({'since': sinceId, 'limit': '100'})
        group_url = group_url + '&' + additional_url_params
        while (group_url is not None):
            print 'Extracting URLs in %s' % group_url
            data = urlopen(group_url).read()
            jsondata = json.loads(data)
            for i, post in enumerate(jsondata['data']):
                link_with_mmyy = self.extract_link_mmyy(post)
                if link_with_mmyy is not None:
                    videos[link_with_mmyy[0]].append(link_with_mmyy[1])
                if newSinceId == '':
                    newSinceId = toEpoch(post.get('created_time'))
            if(jsondata.get('paging') is not None):
                group_url = jsondata.get('paging').get('next')
                group_url = group_url + '&' + urlencode({'since': sinceId})
            else:
                print 'Extracted all URLs, I guess...'
                break
        print 'newSinceId is %s' % newSinceId
        print 'video dict is ' + str(videos)
        return (newSinceId, videos)


    def process_until_data(self):
        '''
        Until Logic, get data with until=untilId with limit=500
        returns (untilId, dict( mm-yy : <List of videos>))
        '''
        videos = defaultdict(list)
        untilId = getPostId('until')
        if(untilId == ''):
            print 'Alert! End of the world, untilId is %s' % untilId
            return
        newUntilId = ''
        group_url = self.group_url
        additional_url_params = urlencode({'until': untilId, 'limit': '500'})
        group_url = group_url + '&' + additional_url_params
        print 'Extracting URLs in ' + group_url
        data = urlopen(group_url).read()
        jsondata = json.loads(data)
        for i, post in enumerate(jsondata['data']):
            link_with_mmyy = self.extract_link_mmyy(post)
            if link_with_mmyy is not None:
                videos[link_with_mmyy[0]].append(link_with_mmyy[1])
            tmpPost = post
        if tmpPost is not None:
            newUntilId = toEpoch(tmpPost.get('created_time'))
        print 'newUntilId is %s' % newUntilId
        print 'videos dict is ' + str(videos)
        return (newUntilId, videos)
                    

    def extract_link_mmyy(self, post):
        '''
        Logic to extract the 'Month, year' from the post and the youtube url id if present
        URL Id precedence: link > message
        '''
        mmyy = parse_date(post.get('created_time'))
        id = ''
        embeds_re = re_compile(r'''
            (?:youtube(?:-nocookie)?\.com                        # youtube.com or youtube-nocookie.com
            |                                                    # or
            youtu\.be)/                                          # youtu.be
            (?:embed/|watch\?v=|watch/\?v=|embed/\?v=)?          # /embed/... or /watch?v=... or /watch/?v=... or /embed/?v=... or /...
            ([^\s\"\?&]+)                                        # capture & stop at whitespace " ? &
            ''', VERBOSE)
        link = post.get('link')
        embeds = []
        if link is not None:
            embeds = findall(embeds_re, link)
        if len(embeds) == 0:
            message = post.get('message')
            if message is not None:
                embeds = findall(embeds_re, message)
        if len(embeds) != 0:
            id = embeds[0]
            print 'Extracted: ' + mmyy + ', ' + id
            return (mmyy, id)
        return


class FetchHandler(RequestHandler):
    '''Oauth-decorated handler, for *manual* update with user present'''

    @DECORATOR.oauth_required
    def get(self):
        user_id = users.get_current_user().user_id()
        gae_user = GaeUser(id=user_id, date=datetime.now())
        gae_user.put()

        worker_url_params = urlencode({'user_id': user_id, 'iscron': 'true'})
        taskqueue.add(url='/fetchworker?' + worker_url_params, method='GET')
        self.response.headers['Content-Type'] = 'text/plain'
        self.response.write("Launched fetch with user_id %s" % user_id)

class InitializeDatastoreWorker(RequestHandler):
    '''Initialize datastore for once'''

    @DECORATOR.oauth_required
    def get(self):
        user_id = users.get_current_user().user_id()
        gae_user = GaeUser(id=user_id, date=datetime.now())
        gae_user.put()

        fbpostObj = FBGroupPost()
        fbpostObj.put()

        playlist = MonthlyPlaylist(epochVal=0)
        playlist.put()


class CronFetchHandler(RequestHandler):
    '''
    Non-Oauth-decorated handler, for *cron* update without user present.
    Will not work unless a manual fetch has been done at least once.
    '''

    def get(self):
        print "CronFetchHandler:get"
        user_id = GaeUser.all().order('-date').get().id
        query_string = urlencode({'user_id': user_id, 'iscron': 'true'})
        taskqueue.add(url='/fetchworker?' + query_string, method='GET')
        self.response.headers['Content-Type'] = 'text/plain'
        self.response.write("Launched fetch with user_id %s" % user_id)

class InitCronFetchHandler(RequestHandler):
    '''
    Non-Oauth-decorated handler, for initial *cron* update without user present.
    Will not work unless a manual fetch has been done at least once.
    '''

    def get(self):
        print "InitCronFetchHandler:get"
        user_id = GaeUser.all().order('-date').get().id
        query_string = urlencode({'user_id': user_id, 'iscron': 'false'})
        taskqueue.add(url='/fetchworker?' + query_string, method='GET')
        self.response.headers['Content-Type'] = 'text/plain'
        self.response.write("Launched fetch with user_id %s" % user_id)


class FetchWorker(RequestHandler):
    '''
    Daily worker, launched as a GAE Task, because it will likely exceed 60s:
        1. Crawl the goyaka radios feed to extract video ids
        2. Add each video id to the corresponding month playlist
    '''

    def __init__(self, request, response):
        self.initialize(request, response)  # required by GAE
        self.user_id = self.request.get('user_id')
        self.iscron = True
        if(self.request.get('iscron') == 'false'):
            self.iscron = False

    def get(self):
        print "FetchWorker:get"
        self.manage_feed()


    def manage_feed(self):
        '''Main module which calls the functions to extract urls, create playlists and insert videos'''
        print 'manage_feed begin'
        result = FbHelper().process_data(self.iscron)
        '''returns tuple(since/untilId, dic( mm-yy : <List of videos>))'''

        if result is None or len(result[1].items()) == 0:
            print 'Nothing to do here... Peace out...'
            return
        '''Handling work with Youtube API here'''
        dict_videos = result[1]
        for playlistName, videos in dict_videos.iteritems():
            self.addtogoyaka(playlistName, videos)
        setPostId(result[0], self.iscron)
        print 'manage_feed end'

    def addtogoyaka(self, name, videos):
        ''' Module to add videos of a month to corresponding youtube playlist'''
        count = len(videos)
        addedCnt = 0
        while addedCnt < count:
            playlist = self.getPlaylistObj(name)
            '''returns playlistObj id, name, epochVal, date, counter)'''
            possible_videos_count = YOUTUBE_MAX_VIDEOS_PER_PLAYLIST - playlist.counter
            if (count - addedCnt) <= possible_videos_count:
                self.insert_videos(playlist.id, videos[addedCnt:])
                playlist.counter = playlist.counter + count - addedCnt
                playlist.put()
                break
            else:
                endIndex = addedCnt + possible_videos_count
                self.insert_videos(playlist.id, videos[addedCnt:endIndex])
                addedCnt = endIndex
                playlist.counter = playlist.counter + possible_videos_count
                playlist.put()
                '''
                The issue is right here. Indexes takes longer time to build. So, the same old playlist
                is returned even though the size is updated!
                '''
                
    def getPlaylistObj(self, name):
        ''' 
        1. generate epoch value from monthname input. 
        2. Search the db based on epoch value, order by date
        3. Top entry: if count < 200, return playlistObj
        4. else, createplaylist and then return the object
        '''
        print 'Inside getPlaylistObj:'
        epochValue=getMonthinEpoch(name)
        print 'epochValue is %s' % str(epochValue)
        playlists = MonthlyPlaylist.all().filter("epochVal =",epochValue)
        if playlists is not None:
            playlistList = playlists.order('-date').fetch(limit=1)
        '''Should be better way to do this'''
        if playlistList is not None and len(playlistList) != 0 and playlistList[0].counter < 200:
            return playlistList[0]
        elif len(playlistList) == 0:
            playlistName = createPlaylistName(name)
            return self.create_playlist(playlistName, epochValue)
        else:
            cntr = int(playlistList[0].name.split('#')[1]) + 1
            playlistName = createPlaylistName(name, cntr)
            return self.create_playlist(playlistName, epochValue)


    def create_playlist(self, playlistName, epochValue):
        '''
        Creates a new playlist on YouTube with given name and persist it
        as a MonthlyPlaylist instance in datastore.
        '''

        print "create_playlist start"
        credentials = StorageByKeyName(
            CredentialsModel, self.user_id, 'credentials').get()
        print "create_playlist got creds"
        http = credentials.authorize(Http())
        print "create_playlist authorized creds"
        request = YOUTUBE.playlists().insert(
            part="snippet,status",
                body=dict(
                    snippet=dict(
                        title=playlistName,
                        description="Songs added in %s" % playlistName
                    ),
                    status=dict(
                        privacyStatus="public"
                    )
                )
            )
        response = request.execute(http=http)
        print "create_playlist executed req"
        playlist_id = response["id"]

        playlist = MonthlyPlaylist(id=playlist_id, name=playlistName, epochVal=epochValue, date=datetime.now(), counter=0)
        playlist.put()

        print "Playlist created: http://www.youtube.com/id?list=%s" % playlist_id
        self.memcache_today_playlists()
        return playlist


    def insert_videos(self, playlist_id, videos):
        '''Inserts the instance videos into the instance YouTube playlist.'''

        credentials = StorageByKeyName(
            CredentialsModel, self.user_id, 'credentials').get()
        http = credentials.authorize(Http())

        print "Adding videos to playlist %s :" % playlist_id
        nb_videos_inserted = 0
        for video in videos:

            if (nb_videos_inserted >= YOUTUBE_MAX_VIDEOS_PER_PLAYLIST):
                break
            else:
                body_add_video = dict(
                  snippet=dict(
                    playlistId=playlist_id,
                    resourceId=dict(
                      kind="youtube#video",
                      videoId=video
                    )
                  )
                )
                try:
                    request = YOUTUBE.playlistItems().insert(
                        part=",".join(body_add_video.keys()),
                        body=body_add_video
                        )
                    request.execute(http=http)
                    print "  %s: %s ..." % (nb_videos_inserted, video)
                    nb_videos_inserted += 1
                except HttpError:
                    print "  %s: KO, insertion of %s failed" % \
                        (nb_videos_inserted, video)
                except AccessTokenRefreshError:
                    print "  %s: KO, access token refresh error on %s" % \
                        (nb_videos_inserted, video)

                sleep(0.1)  # seems required to avoid YT-thrown exception

    def memcache_today_playlists(self):
        today_playlists_key = 'playlists_%s' % datetime.now().date()
        recent_playlists = MonthlyPlaylist.all().order('-date').fetch(limit=5)
        if memcache.get(today_playlists_key) is None:
            memcache.add(today_playlists_key, recent_playlists, 86400)
        else:
            memcache.set(today_playlists_key, recent_playlists, 86400)


class GetPlaylistJs(RequestHandler):
    '''
    Returns a .json containing the latest playlist ID. Necessary because we
    can't write static files in GAE (static files are stored separately).
    Comes from the original static architecture, might be better rewritten
    by making the home page dynamic and avoiding the need for a separate js.
    '''

    def get(self):
        today_playlists_key = 'playlists_%s' % datetime.now().date()
        recent_playlists = memcache.get(today_playlists_key)
        if recent_playlists is None:
            recent_playlists = MonthlyPlaylist.all().order('-date').fetch(limit=5)

        dgjs = 'goyakaradios = {"playlists":['
        for playlist in recent_playlists:
            dgjs += '["' + playlist.name + '","' + playlist.id + '"],'
        dgjs += ']};'
        self.response.headers['Content-Type'] = 'application/javascript'
        self.response.write(dgjs)

MAIN_PAGE_TEMPLATE = '''
<html>
<head>
<title> Goyaka Radios </title>
<body>
<h2> Goyaka Radios Playlist Curator (Temporary Landing Page) </h2>
<a href="https://github.com/sugavaneshb/GoyakaRadios"><img style="position: absolute; top: 0; right: 0; border: 0;" src="https://camo.githubusercontent.com/38ef81f8aca64bb9a64448d0d70f1308ef5341ab/68747470733a2f2f73332e616d617a6f6e6177732e636f6d2f6769746875622f726962626f6e732f666f726b6d655f72696768745f6461726b626c75655f3132313632312e706e67" alt="Fork me on GitHub" data-canonical-src="https://s3.amazonaws.com/github/ribbons/forkme_right_darkblue_121621.png"></a>

<p> - Curates playlists from fb group <a href="https://www.facebook.com/groups/goyakaradios/">Goyaka Radios</a>. Playlists curated are present <a href="https://www.youtube.com/channel/UCywfYeRDOP6BSDmF7OZjCnA/playlists">here</a> </p>
<p> <b> Contact for suggestions and bugs: </b> <a href="mailto:sugavaneshb@gmail.com"> Sugavanesh B </a> </p>
</body>
</html>

'''


class MainPage(RequestHandler):
    '''
    Just a landing page. 
    Will have to use this section to generate stats
    '''

    def get(self):
        self.response.headers['Content-Type'] = 'text/html'
        self.response.write(MAIN_PAGE_TEMPLATE)

APP_ROUTES = [
          ('/', MainPage),
          ('/goyakaradios.js', GetPlaylistJs),
          ('/cronfetch', CronFetchHandler),
          ('/initcronfetch', InitCronFetchHandler),
          ('/fetch', FetchHandler),
          ('/fetchworker', FetchWorker),
          ('/initdatastore', InitializeDatastoreWorker),
          (DECORATOR.callback_path, DECORATOR.callback_handler()),
          ]
app = WSGIApplication(APP_ROUTES, debug=True)
