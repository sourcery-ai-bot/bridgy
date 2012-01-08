"""Twitter source code and datastore model classes.
"""

__author__ = ['Ryan Barrett <bridgy@ryanb.org>']

import datetime
import json
import logging
import os
import re
import urllib

import appengine_config
import models
import tasks
import util

from google.appengine.api import taskqueue
from google.appengine.api import urlfetch
from google.appengine.api import users
from google.appengine.ext import db
from google.appengine.ext import webapp
from google.appengine.ext.webapp.util import run_wsgi_app

HARD_CODED_DEST = 'WordPressSite'


class TwitterSearch(models.Source):
  """A Twitter search.

  The key name is the base url to search for.
  """

  TYPE_NAME = 'Twitter'

  def __init__(self, *args, **kwargs):
    super(TwitterSearch, self).__init__(*args, **kwargs)
    if 'url' in kwargs:
      self.picture = util.favicon_for_url(kwargs['url'])

  @staticmethod
  def new(properties, handler):
    """Creates and saves a TwitterSearch.

    Args:
      properties: dict
      handler: the current webapp.RequestHandler

    Returns: TwitterSearch
    """
    url = properties['url']
    existing = TwitterSearch.get_by_key_name(url)
    search = TwitterSearch(key_name=url,
                           url=url,
                           owner=models.User.get_current_user(),
                           )
    if existing:
      logging.warning('Overwriting TwitterSearch %s! Old version:\n%s' %
                      (id, search.to_xml()))
      handler.messages.append('Updated existing %s search: %s' %
                              (existing.type_display_name(), existing.display_name()))
    else:
      handler.messages.append('Added %s search: %s' %
                              (search.type_display_name(), search.display_name()))

    # TODO: ugh, *all* of this should be transactional
    search.save()
    taskqueue.add(name=tasks.Poll.make_task_name(search), queue_name='poll')
    return search

  def poll(self):
    # TODO: make generic and expand beyond single hard coded destination.
    # GQL so i don't have to import the model class definition.
    query = 'SELECT * FROM %s WHERE url = :1' % HARD_CODED_DEST
    dest = db.GqlQuery(query, self.url).get()
    replies = []

    # find tweets with links that include our base url.
    # search response is JSON tweets:
    # https://dev.twitter.com/docs/api/1/get/search
    results = self.search('%s filter:links' % util.reduce_url(self.url))

    # maps username to list of @ mention search results, which includes replies
    mentions = {}
    for result in results:
      user = result['from_user']
      if user not in mentions:
        mentions[user] = self.search('@%s filter:links' % user)        

    for result in results:
      post_user = result['from_user']
      post_id = result['id']

      # extract destination post url from tweet entities
      # https://dev.twitter.com/docs/tweet-entities
      dest_post_url = None
      post_tweet_url = self.tweet_url(post_user, post_id)
      for url in result['entities'].get('urls', []):
        if url['expanded_url'].startswith(self.url):
          dest_post_url = url['expanded_url']
          logging.debug('Found post %s in tweet %s', dest_post_url, post_tweet_url)

      if not dest_post_url:
        logging.info("Tweet %s should have %s link but doesn't. Maybe shortened?",
                     post_tweet_url, self.url)
        continue

      # find and convert comments
      logging.debug('Looking at %s\'s mentions', post_user)
      for mention in mentions[post_user]:
        logging.debug('Looking at mention: %s', mention)
        if mention.get('in_reply_to_status_id') == post_id:
          reply_id = mention['id']
          reply_user = mention['from_user']
          source_post_url = self.tweet_url(reply_user, reply_id)
          author_name = (mention['from_user_name'] if mention['from_user_name']
                         else '@' + reply_user)
          logging.debug('Found reply %s', source_post_url)

          # parse the timestamp, format e.g. 'Sun, 01 Jan 2012 11:44:57 +0000'
          created_at = re.sub(' \+[0-9]{4}$', '', mention['created_at'])
          created = datetime.datetime.strptime(created_at,
                                               '%a, %d %b %Y %H:%M:%S')

          replies.append(TwitterReply(
              key_name=str(reply_id),
              source=self,
              dest=dest,
              source_post_url=source_post_url,
              dest_post_url=dest_post_url,
              created=created,
              author_name=author_name,
              author_url=self.user_url(reply_user),
              content=self.linkify(mention['text']),
              username=reply_user,
              ))

    return replies

  @staticmethod
  def search(query):
    """Searches for tweets using the Twitter Search API.

    Background:
    https://dev.twitter.com/docs/using-search
    https://dev.twitter.com/docs/api/1/get/search
    http://stackoverflow.com/questions/2693553/replies-to-a-particular-tweet-twitter-api

    Args:
      query: string (not url-encoded)

    Returns: dict, JSON results
    """
    url = ('http://search.twitter.com/search.json'
           '?q=%s&include_entities=true&result_type=recent&rpp=100' %
           urllib.quote_plus(query))
    resp = urlfetch.fetch(url, deadline=999)
    assert resp.status_code == 200, resp.status_code
    return json.loads(resp.content)['results']

  @staticmethod
  def tweet_url(username, id):
    """Returns the URL of a tweet.
    """
    return 'http://twitter.com/%s/status/%d' % (username, id)

  @staticmethod
  def user_url(username):
    """Returns a user's URL.
    """
    return 'http://twitter.com/%s' % username

  @staticmethod
  def linkify(text):
    """Converts @mentions and hashtags to HTML links.
    """
    # twitter usernames can only have \w chars, ie letters, numbers, or
    # underscores. the pattern matches @, *not* preceded by a \w char, followed
    # one or more \w chars.
    text = re.sub(r'(?<!\w)[@#](\w+)',
                 r'<a href="http://twitter.com/\1">\g<0></a>',
                 text)

    # no explicit info about hashtag chars, but i assume the same.
    text = re.sub(r'(?<!\w)[@#](\w+)',
                 r'<a href="http://twitter.com/search?q=%23\1">\g<0></a>',
                 text)
    return text


class TwitterReply(models.Comment):
  """Key name is the tweet (aka status) id.
  """

  # user who wrote the comment
  username = db.StringProperty(required=True)


class AddTwitterSearch(util.Handler):
  def post(self):
    search = TwitterSearch.new(self.request.params, self)
    self.redirect('/')


class DeleteTwitterSearch(util.Handler):
  def post(self):
    search = TwitterSearch.get_by_key_name(self.request.params['key_name'])
    # TODO: remove tasks, etc.
    msg = 'Deleted %s source: %s' % (search.type_display_name(),
                                     search.display_name())
    search.delete()
    self.redirect('/?msg=' + msg)


application = webapp.WSGIApplication([
    ('/twitter/add', AddTwitterSearch),
    ('/twitter/delete', DeleteTwitterSearch),
    ], debug=appengine_config.DEBUG)

def main():
  run_wsgi_app(application)


if __name__ == '__main__':
  main()
