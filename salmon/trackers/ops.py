import re

from salmon.trackers.base import BaseGazelleApi

from salmon import config
import click
import requests
from requests.exceptions import ConnectTimeout, ReadTimeout

from bs4 import BeautifulSoup


class OpsApi(BaseGazelleApi):
    def __init__(self):
        self.headers = {
            "Connection": "keep-alive",
            "Cache-Control": "max-age=0",
            "User-Agent": config.USER_AGENT,
        }
        self.site_code = 'OPS'
        self.base_url = 'https://orpheus.network'
        self.tracker_url = 'https://home.opsfet.ch'
        self.site_string = 'OPS'
        if config.OPS_DOTTORRENTS_DIR:
            self.dot_torrents_dir = config.OPS_DOTTORRENTS_DIR
        else:
            self.dot_torrents_dir = config.DOTTORRENTS_DIR

        self.cookie = config.OPS_SESSION

        self.session = requests.Session()
        self.session.headers.update(self.headers)

        self.authkey = None
        self.passkey = None
        self.authenticate()

    def parse_most_recent_torrent_and_group_id_from_group_page(self, text):
        """
        Given the HTML (ew) response from a successful upload, find the most
        recently uploaded torrent (it better be ours).
        """
        torrent_ids = []
        soup = BeautifulSoup(text, "html.parser")
        for pl in soup.find_all("a", class_="tooltip"):
            torrent_url = re.search(r"torrents.php\?id=(\d+)", pl["href"])
            if torrent_url:
                torrent_ids.append(int(torrent_url[1]))
        return max(torrent_ids)
