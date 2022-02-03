#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CDSL Corpus Management

Download dictionaries from https://www.sanskrit-lexicon.uni-koeln.de/
"""

###############################################################################

import json
import logging
from pathlib import Path
from dataclasses import dataclass, field
from functools import lru_cache
import zipfile

from peewee import fn
from playhouse.db_url import connect

import bs4
import requests
from requests_downloader import downloader

from .models import (
    MWLexicon, MWEntry,
    AP90Lexicon, AP90Entry,
    lexicon_constructor, entry_constructor
)

###############################################################################

LOGGER = logging.getLogger(__name__)

###############################################################################

SERVER_URL = "https://www.sanskrit-lexicon.uni-koeln.de"

###############################################################################


DICTIONARIES = {
    "MW": (MWLexicon, MWEntry),
    "AP90": (AP90Lexicon, AP90Entry),
}

DEFAULT_DICTIONARIES = [
    # Sanskrit-English
    # ----------------
    "MW", "AP90",
    # "WIL", "YAT", "GST", "MW72", "BEN", "LAN", "CAE", "MD", "SHS",

    # English-Sanskrit
    # ----------------
    "MWE", "AE",  # "BOR",

    # Sanskrit-Sanskrit
    # -----------------
    # "VCP", "SKD", "ARMH",

    # Special
    # ---------
    # "INM", "MCI", "VEI" "PUI", "PE", "SNP",
]

ENGLISH_DICTIONARIES = ["MWE", "BOR", "AE"]

###############################################################################

HOME_DIR = Path.home()
COLOGNE_DIR = HOME_DIR / "cdsl_data"

###############################################################################


@dataclass(eq=False)
class CDSLDict:
    """Dictionary from CDSL"""
    id: str
    date: str
    name: str
    url: str = field(repr=False)
    db: str = field(repr=False, default=None)
    transliterate_keys: bool = field(repr=False, default=True)
    transliterate_data: bool = field(repr=False, default=True)

    # ----------------------------------------------------------------------- #

    def download(self, download_dir):
        """Download and extract dictionary data

        Parameters
        ----------
        download_dir : str or Path
            Full path of directory where the dictionary data should be
            downloaded and extracted

        Returns
        -------
        bool
            True if successfully downloaded or already up-to-date
        """
        up_to_date = False

        html = requests.get(self.url).content.decode()
        soup = bs4.BeautifulSoup(html, "html.parser")
        footer = soup.find("div", attrs={"id": "footer"})
        last_modified = footer.find("p").get_text().split(":", 1)[-1].strip()
        LOGGER.debug(f"Last modified at {last_modified}")

        download_dir = Path(download_dir)
        download_dir.mkdir(parents=True, exist_ok=True)

        last_modified_file = download_dir / "last_modified.txt"

        if last_modified_file.exists():
            local_last_modified = last_modified_file.read_text().strip()
            up_to_date = (local_last_modified == last_modified)

        if not up_to_date:
            # not up-to-date
            # backup current file
            download_path = download_dir / f"{self.id}.web.zip"
            backup_path = download_dir / f"{self.id}.web.zip.bak"
            if download_path.exists():
                backup_path = download_path.rename(backup_path)

            # find download link
            lis = soup.find_all("li")
            for li in lis:
                if "Directory 'web' containing displays" in li.get_text():
                    break
            else:
                LOGGER.error("No download link for 'web' displays was found.")
                return False

            web_url = li.find("a")["href"]
            web_url = requests.compat.urljoin(self.url, web_url)

            # download
            success = downloader.download(web_url, download_path=download_path)

            if not success:
                # download failed - restore backup
                LOGGER.error("Something went wrong.")
                if backup_path.exists():
                    LOGGER.debug("Restoring ..")
                    backup_path.rename(download_path)
                return False

            # download was sucessful - remove backup
            if backup_path.exists():
                backup_path.unlink()

            # update last modified info
            last_modified_file.write_text(last_modified)

            # extract
            with zipfile.ZipFile(download_path, "r") as zipref:
                zipref.extractall(download_dir)
        else:
            LOGGER.info(f"Data for dictionary '{self.id}' is up-to-date.")
        return True

    def setup(self, data_dir, symlink_dir=None, update=False):
        """Setup the dictionary database path

        Parameters
        ----------
        data_dir : str or Path
            Full path of directory where the dictionary data is stored
        symlink_dir : str or Path, optional
            Full path of the directory where the symbolink links to the
            SQLite database of dictionary will be created
            If None, symbolic links aren't created.
            The default is None.
        update : bool, optional
            If True, an attempt to update dictionary data will be made.
            The default is False.

        Returns
        -------
        bool
            True if the setup was successful
        """
        # setup database path
        data_dir = Path(data_dir)
        database_filename = f"{self.id.lower()}.sqlite"
        database_path = data_dir / "web" / "sqlite" / database_filename
        self.db = str(database_path)

        status = (
            (not update and database_path.exists())
            or
            self.download(download_dir=data_dir)
        )
        if not status:
            LOGGER.error(f"Couldn't setup dictionary '{self.id}'.")
            return False

        # create symlink
        if symlink_dir is not None:
            symlink_dir = Path(symlink_dir)
            symlink_dir.mkdir(parents=True, exist_ok=True)
            symlink_path = symlink_dir / f"{self.id}.db"
            if not symlink_path.exists():
                symlink_path.symlink_to(database_path)
            self.db = str(symlink_path)

        return True

    # ----------------------------------------------------------------------- #

    def connect(self, lexicon_model=None, entry_model=None):
        """Connect to the SQLite database"""
        if lexicon_model is not None and entry_model is not None:
            self._lexicon = lexicon_model
            self._entry = entry_model
        else:
            if self.id in DICTIONARIES:
                self._lexicon, self._entry = DICTIONARIES[self.id]
            else:
                self._lexicon = lexicon_constructor(
                    self.id,
                    transliterate_keys=self.transliterate_keys,
                    transliterate_data=self.transliterate_data
                )
                self._entry = entry_constructor(self.id)

        db_url = f"sqlite:///{self.db}"
        self._lexicon.bind(connect(db_url))
        self.search.cache_clear()
        self.stats.cache_clear()

    # ----------------------------------------------------------------------- #

    @lru_cache(maxsize=1)
    def stats(self):
        lex = self._lexicon
        total_count = lex.select().count()
        distinct_query = (
            lex
            .select(
                lex.id, lex.key, lex.data, fn.COUNT(lex.key).alias("count")
            )
            .group_by(lex.key)
            .order_by(fn.COUNT(lex.key).desc())
        )
        top = [(item.key, item.count) for item in distinct_query.limit(10)]
        distinct_count = distinct_query.count()
        return {
            "total": total_count,
            "distinct": distinct_count,
            "top": top
        }

    # ----------------------------------------------------------------------- #

    @lru_cache(maxsize=4096)
    def search(self, search_key, ignore_case=False):
        """Search in the dictionary

        Parameters
        ----------
        search_key : str
            Search key, may contain wildcards.
        ignore_case : bool, optional
            Ignore case while performing lookup.
            The default is False.

        Returns
        -------
        list
            List of matching entries
        """
        query = self._lexicon.select().where(self._lexicon.key % search_key)
        iquery = self._lexicon.select().where(self._lexicon.key ** search_key)
        search_query = iquery if ignore_case else query
        return [
            self._entry(result)
            for result in search_query
        ]

    def entry(self, entry_id):
        """Get an entry by ID"""
        return self._entry(self._lexicon.get(self._lexicon.id == entry_id))

    def dump(self, output=None):
        """Dump data as JSON"""
        data = [{
            "id": str(entry.id),
            "key": entry.key,
            "data": entry.data,
            "text": entry.meaning
        } for entry in (
            self._entry(result) for result in self._lexicon.select()
        )]
        if output is not None:
            with open(output, mode="w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
        return data


###############################################################################


@dataclass
class CDSLCorpus:
    """
    CDSL Corpus Class

    Refers to a CDSL installation instance at the location `data_dir`.
    """
    data_dir: str = field(default=None)
    transliterate_keys: bool = field(repr=False, default=True)
    transliterate_data: bool = field(repr=False, default=True)

    # ----------------------------------------------------------------------- #

    def __post_init__(self):
        self.data_dir = (
            Path(COLOGNE_DIR)
            if self.data_dir is None
            else Path(self.data_dir)
        )
        self.dict_dir = self.data_dir / "dict"
        self.db_dir = self.data_dir / "db"
        self.dicts = {}
        self.get_available_dicts()

    def __getattr__(self, attr: str):
        if attr in self.dicts:
            return self.dicts[attr]
        else:
            LOGGER.exception(f"Invalid attribute {attr}")
            raise AttributeError

    # ----------------------------------------------------------------------- #

    def setup(self, dict_ids: list = None, update: bool = False):
        """Setup CDSL dictionaries in bulk

        Calls `CDSLDict.setup()` on every `CDSLDict`, and if successful, also
        calls `CDSLDict.connect()` to establish a connection to the database

        Parameters
        ----------
        dict_ids : list, optional
            List of dictionary IDs to setup.
            If `None`, the dictionaries from `DEFAULT_DICTIONARIES` as well as
            locally installed dictionaries will be setup.
            The default is None.
        update : bool, optional
            If True, and update check is performed for every dictionary in
            `dict_ids`, and if available, the updated version is installed
            The default is False.

        Returns
        -------
        bool
            True, if the setup of all the dictionaries from `dict_ids`
            is successful.
            i.e. If every `CDSLDict.setup()` call returns True.

        Raises
        ------
        ValueError
            If `dict_ids` is not a `list` or `None`.
        """
        if dict_ids is None:
            dict_ids = DEFAULT_DICTIONARIES + list(self.get_installed_dicts())

        if isinstance(dict_ids, list):
            dict_ids = set(dict_ids)
            setup_dicts = {
                dict_id: cdsl_dict
                for dict_id, cdsl_dict in self.available_dicts.items()
                if dict_id in dict_ids
            }
        else:
            raise ValueError("`dict_ids` must be a `list` or `None`")

        status = []
        for dict_id, cdsl_dict in setup_dicts.items():
            dict_dir = self.dict_dir / dict_id.upper()
            success = cdsl_dict.setup(
                data_dir=dict_dir,
                symlink_dir=self.db_dir,
                update=update
            )
            status.append(success)
            if success:
                cdsl_dict.connect()
                self.dicts[dict_id] = cdsl_dict

        return bool(status) and all(status)

    # ----------------------------------------------------------------------- #

    def get_available_dicts(self):
        """
        Fetch a list of dictionaries available for download from CDSL

        Homepage of CDSL Project (`SERVER_URL`) is fetched and parsed to obtain
        this list.
        """
        html = requests.get(SERVER_URL).content.decode()
        soup = bs4.BeautifulSoup(html, "html.parser")
        dl_tags = soup.find_all("a", attrs={"title": "Downloads"})
        dictionaries = {}
        for dl_tag in dl_tags:
            row = dl_tag.find_parent("tr")
            cells = row.find_all("td")
            assert(len(cells) == 4)
            dict_id = cells[0].get_text(" ").strip().split()[0]
            dict_date = cells[1].get_text(" ").strip().split()[0]
            dict_name = cells[2].find("a").get_text(" ").strip()
            dict_download = f"{SERVER_URL}{dl_tag['href']}"
            dict_transliterate_keys = (
                dict_id not in ENGLISH_DICTIONARIES
                and
                self.transliterate_keys
            )
            dict_transliterate_data = self.transliterate_data

            dictionaries[dict_id] = CDSLDict(
                id=dict_id,
                date=dict_date,
                name=dict_name,
                url=dict_download,
                transliterate_keys=dict_transliterate_keys,
                transliterate_data=dict_transliterate_data
            )

        self.available_dicts = dictionaries
        return dictionaries

    # ----------------------------------------------------------------------- #

    def get_installed_dicts(self):
        """Fetch a list of dictionaries installed locally"""
        dictionaries = {}
        dict_ids = [path.name for path in self.dict_dir.glob("*")]
        for dict_id in dict_ids:
            if dict_id not in self.available_dicts:
                LOGGER.error(f"Invalid dictionary '{dict_id}'")
                continue

            db_filename = f"{dict_id.lower()}.sqlite"
            db_path = self.dict_dir / dict_id / "web" / "sqlite" / db_filename
            if db_path.is_file():
                dictionaries[dict_id] = self.available_dicts[dict_id]

        return dictionaries

###############################################################################
