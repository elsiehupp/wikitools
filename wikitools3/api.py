﻿#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# Copyright 2008-2014 Alex Zaddach <mrzmanwiki@gmail.com>
# Copyright 2014 Mark A. Hershberger <https://github.com/hexmode>
# Copyright 2021 Elsie Hupp <wikitools3@elsiehupp.com>

# This file is part of wikitools3.
# wikitools3 is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# wikitools3 is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

# You should have received a copy of the GNU General Public License
# along with wikitools3.  If not, see <http://www.gnu.org/licenses/>.

# This module is documented at http://code.google.com/p/python-wikitools/wiki/api

import base64
import copy
import re
import sys
import time
import urllib
import urllib.request
import warnings
from urllib.parse import quote_plus

import wikitools3.wiki as wiki
from poster3.encode import multipart_encode

canupload = True

import json

try:
    import gzip
    import io
except:
    gzip = False


class APIError(Exception):
    """Base class for errors"""


class APIDisabled(APIError):
    """API not enabled"""


class APIRequest:
    """A request to the site's API"""

    def __init__(self, wiki, data, write=False, multipart=False):
        """
        wiki - A Wiki object
        data - API parameters in the form of a dict
        write - set to True if doing a write query, so it won't try again on error
        multipart - use multipart data transfer, required for file uploads,
        requires the poster3 package

        maxlag is set by default to 5 but can be changed
        format is always set to json
        """
        if not canupload and multipart:
            raise APIError("The poster3 module is required for multipart support")
        self.sleep = 5
        self.data = data.copy()
        self.data["format"] = "json"
        self.iswrite = write
        if wiki.assertval is not None and self.iswrite:
            self.data["assert"] = wiki.assertval
        if "maxlag" not in self.data and wiki.maxlag >= 0:
            self.data["maxlag"] = wiki.maxlag
        self.multipart = multipart
        if self.multipart:
            (datagen, self.headers) = multipart_encode(self.data)
            self.encodeddata = ""
            for singledata in datagen:
                self.encodeddata = self.encodeddata + singledata
        else:
            self.encodeddata = urlencode(self.data, 1)
            self.headers = {
                "Content-Type": "application/x-www-form-urlencoded",
                "Content-Length": str(len(self.encodeddata)),
            }
        self.headers["User-agent"] = wiki.useragent
        if gzip:
            self.headers["Accept-Encoding"] = "gzip"
        self.wiki = wiki
        self.response = False
        if wiki.auth:
            self.headers["Authorization"] = "Basic {0}".format(
                base64.encodebytes(f"{wiki.auth}:{wiki.httppass}".encode()).decode()
            ).replace("\n", "")
        if hasattr(wiki, "passman"):
            self.opener = urllib.request.build_opener(
                urllib.request.HTTPDigestAuthHandler(wiki.passman),
                urllib.request.HTTPCookieProcessor(wiki.cookies),
            )
        else:
            self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(wiki.cookies))
        self.request = urllib.request.Request(self.wiki.apibase, self.encodeddata, self.headers)

    def setMultipart(self, multipart=True):
        """Enable multipart data transfer, required for file uploads."""
        if not canupload and multipart:
            raise APIError("The poster3 package is required for multipart support")
        self.multipart = multipart
        if multipart:
            (datagen, headers) = multipart_encode(self.data)
            self.headers.pop("Content-Length")
            self.headers.pop("Content-Type")
            self.headers.update(headers)
            self.encodeddata = ""
            for singledata in datagen:
                self.encodeddata = self.encodeddata + singledata
        else:
            self.encodeddata = urlencode(self.data, 1)
            self.headers["Content-Length"] = str(len(self.encodeddata))
            self.headers["Content-Type"] = "application/x-www-form-urlencoded"

    def changeParam(self, param, value):
        """Change or add a parameter after making the request object

        Simply changing self.data won't work as it needs to update other things.

        value can either be a normal string value, or a file-like object,
        which will be uploaded, if setMultipart was called previously.

        """
        if param == "format":
            raise APIError("You can not change the result format")
        self.data[param] = value
        if self.multipart:
            (datagen, headers) = multipart_encode(self.data)
            self.headers.pop("Content-Length")
            self.headers.pop("Content-Type")
            self.headers.update(headers)
            self.encodeddata = ""
            for singledata in datagen:
                self.encodeddata = self.encodeddata + singledata
        else:
            self.encodeddata = urlencode(self.data, 1)
            self.headers["Content-Length"] = str(len(self.encodeddata))
            self.headers["Content-Type"] = "application/x-www-form-urlencoded"
        self.request = urllib.request.Request(self.wiki.apibase, self.encodeddata, self.headers)

    def query(self, querycontinue=True):
        """Actually do the query here and return usable stuff

        querycontinue - look for query-continue in the results and continue querying
        until there is no more data to retrieve (DEPRECATED: use queryGen as a more
        reliable and efficient alternative)

        """
        if querycontinue and self.data["action"] == "query":
            warnings.warn(
                """The querycontinue option is deprecated and will be removed
in a future release, use the new queryGen function instead
for queries requring multiple requests""",
                FutureWarning,
            )
        data = False
        while not data:
            rawdata = self.__getRaw()
            data = self.__parseJSON(rawdata)
            if not data and type(data) is APIListResult:
                break
        if "error" in data:
            if self.iswrite and data["error"]["code"] == "blocked":
                raise wiki.UserBlocked(data["error"]["info"])
            raise APIError(data["error"]["code"], data["error"]["info"])
        if "query-continue" in data and querycontinue:
            data = self.__longQuery(data)
        return data

    def queryGen(self):
        """Unlike the old query-continue method that tried to stitch results
        together, which could work poorly for complex result sets and could
        use a lot of memory, this yield each set returned by the API and lets
        the user process the data.
        Loosely based on the recommended implementation on mediawiki.org

        """
        reqcopy = copy.deepcopy(self.request)
        self.changeParam("continue", "")
        while True:
            data = False
            while not data:
                rawdata = self.__getRaw()
                data = self.__parseJSON(rawdata)
                if not data and type(data) is APIListResult:
                    break
            if "error" in data:
                if self.iswrite and data["error"]["code"] == "blocked":
                    raise wiki.UserBlocked(data["error"]["info"])
                raise APIError(data["error"]["code"], data["error"]["info"])
            yield data
            if "continue" not in data:
                break
            self.request = copy.deepcopy(reqcopy)
            for param in data["continue"]:
                self.changeParam(param, data["continue"][param])

    def __longQuery(self, initialdata):
        """For queries that require multiple requests"""
        self._continues = set()
        self._generator = ""
        total = initialdata
        res = initialdata
        params = self.data
        numkeys = len(res["query-continue"].keys())
        while numkeys > 0:
            key1 = ""
            key2 = ""
            possiblecontinues = res["query-continue"].keys()
            if len(possiblecontinues) == 1:
                key1 = possiblecontinues[0]
                keylist = res["query-continue"][key1].keys()
                if len(keylist) == 1:
                    key2 = keylist[0]
                else:
                    for key in keylist:
                        if len(key) < 11:
                            key2 = key
                            break
                    else:
                        key2 = keylist[0]
            else:
                for posskey in possiblecontinues:
                    keylist = res["query-continue"][posskey].keys()
                    for key in keylist:
                        if len(key) < 11:
                            key1 = posskey
                            key2 = key
                            break
                    if key1:
                        break
                else:
                    key1 = possiblecontinues[0]
                    key2 = res["query-continue"][key1].keys()[0]
            if isinstance(res["query-continue"][key1][key2], int):
                cont = res["query-continue"][key1][key2]
            else:
                cont = res["query-continue"][key1][key2].encode("utf-8")
            if len(key2) >= 11 and key2.startswith("g"):
                self._generator = key2
                for ckey in self._continues:
                    params.pop(ckey, None)
            else:
                self._continues.add(key2)
            params[key2] = cont
            req = APIRequest(self.wiki, params)
            res = req.query(False)
            for type in possiblecontinues:
                total = resultCombine(type, total, res)
            numkeys = len(res["query-continue"].keys()) if "query-continue" in res else 0
        return total

    def __getRaw(self):
        data = False
        while not data:
            try:
                if self.sleep >= self.wiki.maxwaittime or self.iswrite:
                    catcherror = None
                else:
                    catcherror = Exception

                # In Python 3, urlopen does not accept a string as data
                # so we need to encode it first
                if isinstance(self.request.data, str):
                    self.request.data = self.request.data.encode("utf-8")
                data = self.opener.open(self.request)
                # then decode it back to a string
                if isinstance(self.request.data, bytes):
                    self.request.data = self.request.data.decode("utf-8")

                self.response = data.info()
                if gzip:
                    encoding = self.response.get("Content-encoding")
                    if encoding in ("gzip", "x-gzip"):
                        data = gzip.open(io.BytesIO(data.read()), "rb")
            except catcherror as exc:
                errname = sys.exc_info()[0].__name__
                errinfo = exc
                print(
                    "%s: %s trying request again in %d seconds"
                    % (errname, errinfo, self.sleep)
                )
                time.sleep(self.sleep + 0.5)
                self.sleep += 5
        return data

    def __parseJSON(self, data):
        maxlag = True
        while maxlag:
            try:
                maxlag = False
                parsed = json.loads(data.read())
                content = None
                if isinstance(parsed, dict):
                    content = APIResult(parsed)
                    content.response = self.response.items()
                elif isinstance(parsed, list):
                    content = APIListResult(parsed)
                    content.response = self.response.items()
                else:
                    content = parsed
                if "error" in content:
                    error = content["error"]["code"]
                    if error == "maxlag":
                        lagtime = int(
                            re.search("(\d+) seconds", content["error"]["info"]).group(
                                1
                            )
                        )
                        lagtime = min(lagtime, self.wiki.maxwaittime)
                        print(f"Server lag, sleeping for {str(lagtime)} seconds")
                        maxlag = True
                        time.sleep(int(lagtime) + 0.5)
                        return False
            except:  # Something's wrong with the data...
                data.seek(0)
                if (
                    "MediaWiki API is not enabled for this site. Add the following line to your LocalSettings.php<pre><b>$wgEnableAPI=true;</b></pre>"
                    in data.read()
                ):
                    raise APIDisabled("The API is not enabled on this site")
                print("Invalid JSON, trying request again")
                # FIXME: Would be nice if this didn't just go forever if its never going to work
                return False
        return content


class APIResult(dict):
    response = []


class APIListResult(list):
    response = []


def resultCombine(type, old, new):
    """Experimental-ish result-combiner thing

    If the result isn't something from action=query,
    this will just explode, but that shouldn't happen hopefully?

    """
    ret = old
    if type in new["query"]:  # Basic list, easy
        ret["query"][type].extend(new["query"][type])
    else:  # Else its some sort of prop=thing and/or a generator query
        for key in new["query"]["pages"].keys():  # Go through each page
            if key not in old["query"]["pages"]:  # if it only exists in the new one
                ret["query"]["pages"][key] = new["query"]["pages"][
                    key
                ]  # add it to the list
            elif type not in new["query"]["pages"][key]:
                continue
            elif type not in ret["query"]["pages"][key]:  # if only the new one does, just add it to the return
                ret["query"]["pages"][key][type] = new["query"]["pages"][key][type]
            else:  # Need to check for possible duplicates for some, this is faster than just iterating over new and checking for dups in ret
                retset = {tuple(entry.items()) for entry in ret["query"]["pages"][key][type]}
                newset = {tuple(entry.items()) for entry in new["query"]["pages"][key][type]}
                retset.update(newset)
                ret["query"]["pages"][key][type] = [dict(entry) for entry in retset]
    return ret


def urlencode(query, doseq=0):
    """
    Hack of urllib's urlencode function, which can handle
    utf-8, but for unknown reasons, chooses not to by
    trying to encode everything as ascii
    """
    if hasattr(query, "items"):
        # mapping objects
        query = query.items()
    else:
        # it's a bother at times that strings and string-like objects are
        # sequences...
        try:
            # non-sequence items should not work with len()
            # non-empty strings will fail this
            if len(query) and not isinstance(query[0], tuple):
                raise TypeError
            # zero-length sequences of all types will get here and succeed,
            # but that's a minor nit - since the original implementation
            # allowed empty dicts that type of behavior probably should be
            # preserved for consistency
        except TypeError:
            ty, va, tb = sys.exc_info()
            raise TypeError("not a valid non-string sequence or mapping object", tb)

    l = []
    if not doseq:
        # preserve old behavior
        for k, v in query:
            k = quote_plus(str(k))
            v = quote_plus(str(v))
            l.append(f"{k}={v}")
    else:
        for k, v in query:
            k = quote_plus(str(k))
            if isinstance(v, str):
                v = quote_plus(v)
                l.append(f"{k}={v}")
            elif isinstance(v, (int, float)):
                v = quote_plus(str(v))
                l.append(f"{k}={v}")
            elif v.type(str): # TODO: .type() broken for python 3

                # is there a reasonable way to convert to ASCII?
                # encode generates a string, but "replace" or "ignore"
                # lose information and "strict" can raise UnicodeError
                v = quote_plus(v.encode("utf8", "replace"))
                l.append(f"{k}={v}")
            else:
                try:
                    # is this a sufficient test for sequence-ness?
                    x = len(v)
                except TypeError:
                    # not a sequence
                    v = quote_plus(str(v))
                    l.append(f"{k}={v}")
                else:
                    # loop over the sequence
                    l.extend(f"{k}={quote_plus(str(elt))}" for elt in v)
    return "&".join(l)
