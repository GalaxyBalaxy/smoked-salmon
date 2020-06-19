import asyncio
import html
import re
import sys
import time
from ratelimit import limits, sleep_and_retry
from collections import namedtuple
from json.decoder import JSONDecodeError

import click
import requests
from bs4 import BeautifulSoup
from requests.exceptions import ConnectTimeout, ReadTimeout

from salmon import config
from salmon.constants import RELEASE_TYPES
from salmon.errors import (
    LoginError,
    RateLimitError,
    RequestError,
    RequestFailedError,
)



loop = asyncio.get_event_loop()

ARTIST_TYPES = [
    "main",
    "guest",
    "remixer",
    "composer",
    "conductor",
    "djcompiler",
    "producer",
]

INVERTED_RELEASE_TYPES = {
    **dict(zip(RELEASE_TYPES.values(), RELEASE_TYPES.keys())),
    1024: "Guest Appearance",
    1023: "Remixed By",
    1022: "Composition",
    1021: "Produced By",
}



SearchReleaseData = namedtuple(
    "SearchReleaseData",
    ["lossless", "lossless_web", "year", "artist", "album", "release_type", "url"],
)




class BaseGazelleApi:
    def __init__(self):
        "Base init class shouldn't really be geting used"
        self.headers = {
            "Connection": "keep-alive",
            "Cache-Control": "max-age=0",
            "User-Agent": config.USER_AGENT,

        }
        self.site_code = 'RED'
        self.base_url = 'https://redacted.ch'
        self.tracker_url = 'https://flacsfor.me'
        self.site_string = 'RED'
        self.cookie = config.RED_SESSION
        if 'SITE_API_KEY' in tracker_details.keys():
            self.api_key = config.RED_API_KEY

        self.session = requests.Session()
        self.session.headers.update(self.headers)

        self.authkey = None
        self.passkey = None
        self.authenticate()


    @property
    def announce(self):
        return f"{self.tracker_url}/{self.passkey}/announce"

    def authenticate(self):
        """Make a request to the site API with the saved cookie and get our authkey."""
        self.session.cookies.clear()
        self.session.cookies["session"] = self.cookie
        try:
            acctinfo = loop.run_until_complete(self.request("index"))
        except RequestError:
            raise LoginError
        self.authkey = acctinfo["authkey"]
        self.passkey = acctinfo["passkey"]

    @sleep_and_retry
    @limits(5, 10)
    async def request(self, action, **kwargs):
        """
        Make a request to the site API, accomodating the rate limit.
        This method will create a counter and a timestamp, and ensure that
        the 5 requests / 10 seconds rate limit isn't violated, while allowing
        short bursts of requests without a 2 second wait after each one
        (at the expense of a potentially longer wait later).
        """

        url = self.base_url + "/ajax.php"
        params = {"action": action, **kwargs}

        try:
            resp = await loop.run_in_executor(
                None,
                lambda: self.session.get(
                    url, params=params, timeout=5, allow_redirects=False
                ),
            )

            # print(url,params,resp)  debug
            resp = resp.json()
        except JSONDecodeError:
            raise RateLimitError
        except (ConnectTimeout, ReadTimeout):
            click.secho(
                f"Connection to API timed out, try script again later. Gomen!",
                fg="red",
            )
            sys.exit(1)

        if resp["status"] != "success":
            raise RequestFailedError
        return resp["response"]

    async def torrentgroup(self, group_id):
        """Get information about a torrent group."""
        return await self.request("torrentgroup", id=group_id)

    async def get_request(self, id):
        data = {"id": id}
        return await self.request("request", **data)

    async def artist_rls(self, artist):
        """
        Get all the torrent groups belonging to an artist on site.
        All groups without a FLAC will be highlighted.
        """
        resp = await self.request("artist", artistname=artist)
        releases = []
        for group in resp["torrentgroup"]:
            # We do not put compilations or guest appearances in this list.
            if not group["artists"]:
                continue
            if group["releaseType"] == 7:
                if not group["extendedArtists"]["6"] or artist.lower() not in {
                    a["name"].lower() for a in group["extendedArtists"]["6"]
                }:
                    continue
            if group["releaseType"] in {1023, 1021, 1022, 1024}:
                continue

            releases.append(
                SearchReleaseData(
                    lossless=any(t["format"] == "FLAC" for t in group["torrent"]),
                    lossless_web=any(
                        t["format"] == "FLAC" and t["media"] == "WEB"
                        for t in group["torrent"]
                    ),
                    year=group["groupYear"],
                    artist=html.unescape(
                        compile_artists(group["artists"], group["releaseType"])
                    ),
                    album=html.unescape(group["groupName"]),
                    release_type=INVERTED_RELEASE_TYPES[group["releaseType"]],
                    url=f'{self.base_url}/torrents.php?id={group["groupId"]}',
                )
            )

        releases = list({r.url: r for r in releases}.values())  # Dedupe

        return resp["id"], releases
        
    async def label_rls(self, label):
        """
        Get all the torrent groups from a label on site.
        All groups without a FLAC will be highlighted.
        """
        first_request = await self.request("browse", remasterrecordlabel=label)
        if 'pages' in first_request.keys():
            pages = first_request['pages']
        else:
            return []
        all_results=first_request['results']
        for i in range(1,max(3,pages)):
            new_results = await self.request("browse", remasterrecordlabel=label,page=str(i))
            all_results += new_results['results']
            
        resp2= await self.request("browse", recordlabel=label)
        all_results=all_results+resp2["results"]
        releases = []
        for group in all_results:
            if not group["artist"]:
                artist=html.unescape(
                    compile_artists(group["artists"], group["releaseType"])
                    )
            else:
                artist=group["artist"]
            releases.append(
                SearchReleaseData(
                    lossless=any(t["format"] == "FLAC" for t in group["torrents"]),
                    lossless_web=any(
                        t["format"] == "FLAC" and t["media"] == "WEB"
                        for t in group["torrents"]
                    ),
                    year=group["groupYear"],
                    artist=artist,
                    album=html.unescape(group["groupName"]),
                    release_type=group["releaseType"],
                    url=f'{self.base_url}/torrents.php?id={group["groupId"]}',
                )
            )

        releases = list({r.url: r for r in releases}.values())  # Dedupe

        return  releases


    async def api_key_upload(self, data, files):
        """Attempt to upload a torrent to the site."""
        url = self.base_url + "/ajax.php?action=upload"
        data["auth"] = self.authkey
        '''Shallow copy.
        We don't want the future requests to send the api key.'''
        api_key_headers = {**self.headers, "Authorization": self.api_key}
        resp = await loop.run_in_executor(
            None,
            lambda: self.session.post(
                url, data=data, files=files, headers=api_key_headers
            ),
        )
        resp = resp.json()
        # print(resp) debug
        try:
            if resp["status"] != "success":
                raise RequestError(f"API upload failed: {resp['error']}")
            elif resp["status"] == "success":
                if 'requestid' in resp['response'].keys() and resp['response']['requestid']:
                    click.secho("Filled request:"
                    f"{self.base_url}/requests.php?action=view&id={resp['response']['requestid']}",fg="green")
                return resp["response"]["torrentid"]
        except TypeError:
            raise RequestError(f"API upload failed, response text: {resp.text}")
        

    async def site_page_upload(self, data, files):
        url = self.base_url + "/upload.php"
        data["auth"] = self.authkey
        resp = await loop.run_in_executor(
            None,
            lambda: self.session.post(
                url, data=data, files=files, headers=self.headers
            ),
        )

        if self.announce in resp.text:
            match = re.search(
                r'<p style="color: red; text-align: center;">(.+)<\/p>', resp.text,
            )
            if match:
                raise RequestError(f"Site upload failed: {match[1]} ({resp.status_code})")
        if 'requests.php' in resp.url:
            try:
                click.secho(f"Filled request:{resp.url}",fg="green")
                return parse_torrent_id_from_filled_request_page(resp.text)
            except TypeError:
                raise RequestError(f"Site upload failed, response text: {resp.text}")
        try:
            return parse_most_recent_torrent_and_group_id_from_group_page(
                resp.url, resp.text
            )
        except TypeError:
            raise RequestError(f"Site upload failed, response text: {resp.text}")

    async def upload(self, data, files):
        """Upload a torrent using upload.php or API key."""
        if hasattr(self, 'api_key'):
            return await self.api_key_upload(data, files)
        else:
            return await self.site_page_upload(data, files)

    async def report_lossy_master(self, torrent_id, comment, source):
        """Automagically report a torrent for lossy master/web approval."""
        url = self.base_url + "/reportsv2.php"
        params = {"action": "takereport"}
        #type_ = "lossywebapproval" if source == "WEB" else "lossyapproval" (only works on RED)
        type_="lossyapproval"
        data = {
            "auth": self.authkey,
            "torrentid": torrent_id,
            "categoryid": 1,
            "type": type_,
            "extra": comment,
            "submit": True,
        }
        r = await loop.run_in_executor(
            None,
            lambda: self.session.post(
                url, params=params, data=data, headers=self.headers
            ),
        )
        if "torrents.php" in r.url:
            return True
        raise RequestError(
            f"Failed to report the torrent for lossy master, code {r.status_code}."
        )


def compile_artists(artists, release_type):
    """Generate a string to represent the artists."""
    if release_type == 7 or len(artists) > 3:
        return config.VARIOUS_ARTIST_WORD
    return " & ".join([a["name"] for a in artists])


def parse_most_recent_torrent_and_group_id_from_group_page(url, text):
    """
    Given the HTML (ew) response from a successful upload, find the most
    recently uploaded torrent (it better be ours).
    """
    torrent_ids = []
    soup = BeautifulSoup(text, "html.parser")
    for pl in soup.find_all("a", class_="tooltip"):
        torrent_url = re.search(r"torrents.php\?torrentid=(\d+)", pl["href"])
        if torrent_url:
            torrent_ids.append(
                int(torrent_url[1])
            )
    return max(torrent_ids)

def parse_torrent_id_from_filled_request_page(text):
    """
    Given the HTML (ew) response from filling a request, 
    find the filling torrent
    """
    torrent_ids = []
    soup = BeautifulSoup(text, "html.parser")
    for pl in soup.find_all("a", string="Yes"):
        torrent_url = re.search(r"torrents.php\?torrentid=(\d+)", pl["href"])
        if torrent_url:
            torrent_ids.append(
                int(torrent_url[1])
            )
    return max(torrent_ids)