# Copyright (C) 2008-2010 Adam Olsen
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2, or (at your option)
# any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 675 Mass Ave, Cambridge, MA 02139, USA.
#
#
# The developers of the Exaile media player hereby grant permission
# for non-GPL compatible GStreamer and Exaile plugins to be used and
# distributed together with GStreamer and Exaile. This permission is
# above and beyond the permissions granted by the GPL license by which
# Exaile is covered. If you modify this code, you may extend this
# exception to your version of the code, but you are not obligated to
# do so. If you do not wish to do so, delete this exception statement
# from your version.

import logging
import os
import hashlib
try:
    import cPickle as pickle
except ImportError:
    import pickle

import gobject
import gio

from xl import common, providers, event, settings, xdg, trax

logger = logging.getLogger(__name__)


# TODO: maybe this could go into common.py instead? could be
# useful in other areas.
class Cacher(object):
    """
        Simple on-disk cache.

        Note that as entries are stored as
        individual files, the data being stored should be of significant
        size (several KB) or a lot of disk space will likely be wasted.
    """
    def __init__(self, cache_dir):
        """
            :param cache_dir: directory to use for the cache. will be
                created if it does not exist.
        """
        try:
            os.makedirs(cache_dir)
        except OSError:
            pass
        self.cache_dir = cache_dir

    def add(self, data):
        """
            Adds an entry to the cache.  Returns a key that can be used
            to retrieve the data from the cache.

            :param data: The data to store, as a bytestring.
        """
        # FIXME: this doesnt handle hash collisions at all. with
        # 2^256 possible keys its unlikely that we'll have a collision,
        # but we should handle it anyway.
        h = hashlib.sha256()
        h.update(data)
        key = h.hexdigest()
        path = os.path.join(self.cache_dir, key)
        open(path, "wb").write(data)
        return key

    def remove(self, key):
        """
            Remove an entry from the cache.

            :param key: The key to remove data for.
        """
        path = os.path.join(self.cache_dir, key)
        try:
            os.remove(path)
        except OSError:
            pass

    def get(self, key):
        """
            Retrieve an entry from the cache.  Returns None if the given
            key does not exist.

            :param key: The key to retrieve data for.
        """
        path = os.path.join(self.cache_dir, key)
        if os.path.exists(path):
            return open(path, "rb").read()
        return None


class CoverManager(providers.ProviderHandler):
    """
        Handles finding covers from various sources.
    """
    def __init__(self, location):
        """
            :param location: The directory to load and store data in.
        """
        providers.ProviderHandler.__init__(self, "covers")
        self.__cache = Cacher(os.path.join(location, 'cache'))
        self.location = location
        self.methods = {}
        self.order = settings.get_option(
                'covers/preferred_order', [])
        self.db = {}
        self.load()
        for method in self.get_providers():
            self.on_provider_added(method)
        self.__save_timer_id = 0

        default_cover_file = open(xdg.get_data_path('images', 'nocover.png'), 'rb')
        self.default_cover_data = default_cover_file.read()
        default_cover_file.close()

        self.tag_fetcher = TagCoverFetcher()
        self.localfile_fetcher = LocalFileCoverFetcher()

        if settings.get_option('covers/use_tags', True):
            providers.register('covers', self.tag_fetcher)
        if settings.get_option('covers/use_localfile', True):
            providers.register('covers', self.localfile_fetcher)

        event.add_callback(self._on_setting_change, 'covers_option_set')

    def _on_setting_change(self, name, obj, data):
        if data == "covers/use_tags":
            if settings.get_option("covers/use_tags"):
                providers.register('covers', self.tag_fetcher)
            else:
                providers.unregister('covers', self.tag_fetcher)
        elif data == "covers/use_localfile":
            if settings.get_option("covers/use_localfile"):
                providers.register('covers', self.localfile_fetcher)
            else:
                providers.unregister('covers', self.localfile_fetcher)


    def _get_methods(self, fixed=False):
        """
            Returns a list of Methods, sorted by preference

            :param fixed: If true, include fixed-position backends in the
                    returned list.
        """
        methods = []
        for name in self.order:
            if name in self.methods:
                methods.append(self.methods[name])
        for k, method in self.methods.iteritems():
            if method not in methods:
                methods.append(method)
        nonfixed = [m for m in methods if not m.fixed]
        if fixed:
            fixed = [m for m in methods if m.fixed]
            fixed.sort(key=lambda x: x.fixed_priority)
            for i, v in enumerate(fixed):
                if v.fixed_priority > 50:
                    methods = fixed[:i] + nonfixed + fixed[i:]
                    break
            else:
                methods = fixed + nonfixed
        else:
            methods = nonfixed
        return methods

    def _get_track_key(self, track):
        """
            Get the db mapping key for a track
        """
        album = track.get_tag_raw("album", join=True)
        compilation = track.get_tag_raw("__compilation")

        if compilation:
            value = track.get_tag_raw('albumartist')
            if value:
                tag = 'albumartist'
            else:
                tag = 'compilation'
                value = compilation
        elif album:
            tag = 'album'
            value = album
        else:
            # no album info, cant store it
            return None
        return (tag, tuple(value))

    @common.synchronized
    @common.cached(5)
    def find_covers(self, track, limit=-1, local_only=False):
        """
            Find all covers for a track

            :param track: The track to find covers for
            :param limit: maximum number of covers to return. -1=unlimited.
            :param local_only: If True, will only return results from local
                    sources.
        """
        if track is None:
            return
        covers = []
        for method in self._get_methods(fixed=True):
            if local_only and method.use_cache:
                continue
            new = method.find_covers(track, limit=limit)
            new = ["%s:%s"%(method.name, x) for x in new]
            covers.extend(new)
            if limit != -1 and len(covers) >= limit:
                break
        return covers

    def set_cover(self, track, db_string, data=None):
        """
            Sets the cover for a track. This will overwrite any existing
            entry.

            :param track: The track to set the cover for
            :param db_string: the string identifying the source of the
                    cover, in "method:key" format.
            :param data: The raw cover data to store for the track.  Will
                    only be stored if the method has use_cache=True
        """
        name = db_string.split(":", 1)[0]
        method = self.methods.get(name)
        if method and method.use_cache and data:
            db_string = "cache:%s"%self.__cache.add(data)
        key = self._get_track_key(track)
        if key:
            self.db[key] = db_string
            self.__set_save_timeout()

    def remove_cover(self, track):
        """
            Remove the saved cover entry for a track, if it exists.
        """
        if track is None:
            return
        key = self._get_track_key(track)
        db_string = self.db.get(key)
        if db_string:
            del self.db[key]
            self.__cache.remove(db_string)
            self.__set_save_timeout()

    def get_cover(self, track, save_cover=True, set_only=False,
            use_default=False):
        """
            get the cover for a given track.
            if the track has no set cover, backends are
            searched until a cover is found or we run out of backends.

            :param track: the Track to get the cover for.
            :param save_cover: if True, a set_cover call will be made
                    to store the cover for later use.
            :param set_only: Only retrieve covers that have been set
                    in the db.
            :param use_default: If True, returns the default cover instead
                    of None when no covers are found.
        """
        if track is None:
            return self.get_default_cover() if use_default else None

        key = self._get_track_key(track)
        db_string = self.db.get(key)
        if db_string:
            return self.get_cover_data(db_string, use_default=use_default)

        if set_only:
            return self.get_default_cover() if use_default else None

        covers = self.find_covers(track, limit=1)
        if covers:
            cover = covers[0]
            data = self.get_cover_data(cover, use_default=use_default)
            if save_cover and data != self.get_default_cover():
                self.set_cover(track, cover, data)
            return data

        return self.get_default_cover() if use_default else None

    def get_cover_data(self, db_string, use_default=False):
        """
            Get the raw image data for a cover.

            :param db_string: The db_string identifying the cover to get.
            :param use_default: If True, returns the default cover instead
                    of None when no covers are found.
        """
        source, data = db_string.split(":", 1)
        ret = None
        if source == "cache":
            ret = self.__cache.get(data)
        else:
            method = self.methods.get(source)
            if method:
                ret = method.get_cover_data(data)
        if ret == None and use_default == True:
            ret = self.get_default_cover()
        return ret

    def get_default_cover(self):
        """
            Get the raw image data for the cover to show if there is no
            cover to display.
        """
        # TODO: wrap this into get_cover_data and get_cover somehow?
        return self.default_cover_data

    def load(self):
        """
            Load the saved db
        """
        path = os.path.join(self.location, 'covers.db')
        data = None
        for loc in [path, path+".old", path+".new"]:
            try:
                f = open(loc, 'rb')
                data = pickle.load(f)
                f.close()
            except IOError:
                pass
            except EOFError:
                try:
                    os.remove(loc)
                except:
                    pass
            if data:
                break
        if data:
            self.db = data

    def save(self):
        """
            Save the db
        """
        if self.__save_timer_id:
            gobject.source_remove(self.__save_timer_id)

        path = os.path.join(self.location, 'covers.db')
        try:
            f = open(path + ".new", 'wb')
            pickle.dump(self.db, f, common.PICKLE_PROTOCOL)
            f.close()
        except IOError:
            return
        try:
            os.rename(path, path + ".old")
        except OSError:
            pass # if it doesn'texist we don't care
        os.rename(path + ".new", path)
        try:
            os.remove(path + ".old")
        except OSError:
            pass

    def __set_save_timeout(self):
        if self.__save_timer_id:
            gobject.source_remove(self.__save_timer_id)
        self.__save_timer_id = gobject.timeout_add_seconds(60, self.save)

    def on_provider_added(self, provider):
        self.methods[provider.name] = provider
        if provider.name not in self.order:
            self.order.append(provider.name)

    def on_provider_removed(self, provider):
        try:
            del self.methods[provider.name]
        except KeyError:
            pass
        if provider.name in self.order:
            self.order.remove(provider.name)

    def set_preferred_order(self, order):
        """
            Sets the preferred search order

            :param order: a list containing the order you'd like to search
                first
        """
        if not type(order) in (list, tuple):
            raise TypeError("order must be a list or tuple")
        self.order = order
        settings.set_option('covers/preferred_order', list(order))


class CoverSearchMethod(object):
    """
        Base class for creating cover search methods.

        Search methods do not have to inherit from this class, it's
        intended more as a template to demonstrate the needed interface.
    """
    #: If true, cover results will be cached for faster lookup
    use_cache = True
    #: A name uniquely identifing the search method.
    name = "base"
    #: Whether the backend should have a fixed priority instead of being
    #  configurable.
    fixed = False
    #: Priority for fixed-position backends. Lower is earlier, non-fixed
    #  backends will always be 50.
    fixed_priority = 50
    def find_covers(self, track, limit=-1):
        """
            Find the covers for a given track.

            :param track: The track to find covers for.
            :param limit: Maximal number of covers to return.
            :returns: A list of strings that can be passed to get_cover_data.
        """
        raise NotImplementedError

    def get_cover_data(self, db_string):
        """
            Get the image data for a cover

            :param db_string: A method-dependent string that identifies the
                    cover to get.
        """
        raise NotImplementedError


class TagCoverFetcher(CoverSearchMethod):
    """
        Cover source that looks for images embedded in tags.
    """
    use_cache = False
    name = "tags"
    cover_tags = ["cover", "coverart"]
    fixed = True
    fixed_priority = 30
    def find_covers(self, track, limit=-1):
        data = None
        tagname = None
        for tag in self.cover_tags:
            try:
                data = track.get_tag_disk(tag)
                tagname = tag
                break
            except KeyError:
                pass
        if data:
            # path format: tagname:track_uri
            path = "%s:%s"%(tagname, track.get_loc_for_io())
            return [path]
        return []

    def get_cover_data(self, db_string):
        tag, uri = db_string.split(":", 1)
        tr = trax.Track(uri, scan=False)
        data = tr.get_tag_disk(tag)
        if not data:
            return None
        if isinstance(data, list):
            data = data[0]
        b64_valid = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz" + \
                "01234567890+/"
        for c in data.rstrip("="):
            if c not in b64_valid:
                break
        else:
            try:
                data = data.decode("base64")
            except UnicodeDecodeError:
                pass
        return data

class LocalFileCoverFetcher(CoverSearchMethod):
    """
        Cover source that looks for images in the same directory as the
        Track.
    """
    use_cache = False
    name = "localfile"
    uri_types = ['file', 'smb', 'sftp', 'nfs']
    extensions = ['.png', '.jpg', '.jpeg', '.gif']
    preferred_names = ['album', 'cover']
    fixed = True
    fixed_priority = 31
    def find_covers(self, track, limit=-1):
        # TODO: perhaps should instead check to see if its mounted in
        # gio, rather than basing this on uri type. file:// should
        # always be checked, obviously.
        if track.get_type() not in self.uri_types:
            return []
        basedir = gio.File(track.get_loc_for_io()).get_parent()
        if not basedir.query_info("standard::type").get_file_type() == \
                gio.FILE_TYPE_DIRECTORY:
            return []
        covers = []
        for fileinfo in basedir.enumerate_children("standard::type"
                ",standard::name"):
            gloc = basedir.get_child(fileinfo.get_name())
            if not fileinfo.get_file_type() == gio.FILE_TYPE_REGULAR:
                continue
            filename = gloc.get_basename()
            base, ext = os.path.splitext(filename)
            if ext.lower() not in self.extensions:
                continue
            if base in self.preferred_names:
                covers.insert(0, gloc.get_uri())
            else:
                covers.append(gloc.get_uri())
        if limit == -1:
            return covers
        else:
            return covers[:limit]

    def get_cover_data(self, db_string):
        try:
            handle = gio.File(db_string).read()
            data = handle.read()
            return data
        except gobject.GError:
            return None



MANAGER = CoverManager(location=xdg.get_data_home_path("covers",
        check_exists=False))

