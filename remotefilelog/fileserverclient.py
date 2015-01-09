# fileserverclient.py - client for communicating with the cache process
#
# Copyright 2013 Facebook, Inc.
#
# This software may be used and distributed according to the terms of the
# GNU General Public License version 2 or any later version.

from mercurial.i18n import _
from mercurial import util, sshpeer, hg, error, util
import os, socket, lz4, time, grp

# Statistics for debugging
fetchcost = 0
fetches = 0
fetched = 0
fetchmisses = 0

_downloading = _('downloading')

def makedirs(root, path, owner):
    os.makedirs(path)

    while path != root:
        stat = os.stat(path)
        if stat.st_uid == owner:
            os.chmod(path, 0o2775)
        path = os.path.dirname(path)

def getcachekey(reponame, file, id):
    pathhash = util.sha1(file).hexdigest()
    return os.path.join(reponame, pathhash[:2], pathhash[2:], id)

def getlocalkey(file, id):
    pathhash = util.sha1(file).hexdigest()
    return os.path.join(pathhash, id)

class cacheconnection(object):
    """The connection for communicating with the remote cache. Performs
    gets and sets by communicating with an external process that has the
    cache-specific implementation.
    """
    def __init__(self):
        self.pipeo = self.pipei = self.pipee = None
        self.subprocess = None
        self.connected = False

    def connect(self, cachecommand):
        if self.pipeo:
            raise util.Abort(_("cache connection already open"))
        self.pipei, self.pipeo, self.pipee, self.subprocess = \
            util.popen4(cachecommand)
        self.connected = True

    def close(self):
        self.connected = False
        # if the process is still open, close the pipes
        if self.pipeo:
            if self.subprocess and self.subprocess.poll() == None:
                self.pipei.write("exit\n")
                self.pipei.close()
                self.pipeo.close()
                self.pipee.close()
                self.subprocess.wait()
            self.subprocess = None
            self.pipeo = self.pipei = self.pipee = None

    def request(self, request, flush=True):
        if self.connected:
            try:
                self.pipei.write(request)
                if flush:
                    self.pipei.flush()
            except IOError:
                self.close()

    def receiveline(self):
        if not self.connected:
            return None
        try:
            result = self.pipeo.readline()[:-1]
            if not result:
                self.close()
        except IOError:
            self.close()

        return result

class fileserverclient(object):
    """A client for requesting files from the remote file server.
    """
    def __init__(self, repo):
        ui = repo.ui
        self.repo = repo
        self.ui = ui
        self.cacheprocess = ui.config("remotefilelog", "cacheprocess")
        if self.cacheprocess:
            self.cacheprocess = util.expandpath(self.cacheprocess)
        self.debugoutput = ui.configbool("remotefilelog", "debug")

        self.localcache = localcache(repo)
        self.remotecache = cacheconnection()

    def request(self, fileids):
        """Takes a list of filename/node pairs and fetches them from the
        server. Files are stored in the local cache.
        A list of nodes that the server couldn't find is returned.
        If the connection fails, an exception is raised.
        """
        if not self.remotecache.connected:
            self.connect()
        cache = self.remotecache
        localcache = self.localcache

        repo = self.repo
        count = len(fileids)
        request = "get\n%d\n" % count
        idmap = {}
        reponame = repo.name
        for file, id in fileids:
            fullid = getcachekey(reponame, file, id)
            request += fullid + "\n"
            idmap[fullid] = file

        cache.request(request)

        missing = []
        total = count
        self.ui.progress(_downloading, 0, total=count)

        fallbackpath = repo.fallbackpath

        missed = []
        count = 0
        while True:
            missingid = cache.receiveline()
            if not missingid:
                missedset = set(missed)
                for missingid in idmap.iterkeys():
                    if not missingid in missedset:
                        missed.append(missingid)
                self.ui.warn(_("warning: cache connection closed early - " +
                    "falling back to server\n"))
                break
            if missingid == "0":
                break
            if missingid.startswith("_hits_"):
                # receive progress reports
                parts = missingid.split("_")
                count += int(parts[2])
                self.ui.progress(_downloading, count, total=total)
                continue

            missed.append(missingid)

        global fetchmisses
        fetchmisses += len(missed)

        count = total - len(missed)
        self.ui.progress(_downloading, count, total=total)

        oldumask = os.umask(0o002)
        try:
            # receive cache misses from master
            if missed:
                verbose = self.ui.verbose
                try:
                    # When verbose is true, sshpeer prints 'running ssh...'
                    # to stdout, which can interfere with some command
                    # outputs
                    self.ui.verbose = False

                    if not fallbackpath:
                        raise util.Abort("no remotefilelog server configured - "
                            "is your .hg/hgrc trusted?")
                    remote = hg.peer(self.ui, {}, fallbackpath)
                    remote._callstream("getfiles")
                finally:
                    self.ui.verbose = verbose

                i = 0
                while i < len(missed):
                    # issue a batch of requests
                    start = i
                    end = min(len(missed), start + 10000)
                    i = end
                    for missingid in missed[start:end]:
                        # issue new request
                        versionid = missingid[-40:]
                        file = idmap[missingid]
                        sshrequest = "%s%s\n" % (versionid, file)
                        remote.pipeo.write(sshrequest)
                    remote.pipeo.flush()

                    # receive batch results
                    for j in range(start, end):
                        self.receivemissing(remote.pipei, missed[j])
                        count += 1
                        self.ui.progress(_downloading, count, total=total)

                remote.cleanup()
                remote = None

                # send to memcache
                count = len(missed)
                request = "set\n%d\n%s\n" % (count, "\n".join(missed))
                cache.request(request)

            self.ui.progress(_downloading, None)

            # mark ourselves as a user of this cache
            localcache.markrepo()
        finally:
            os.umask(oldumask)

        return missing

    def receivemissing(self, pipe, missingid):
        line = pipe.readline()[:-1]
        if not line:
            raise error.ResponseError(_("error downloading file " +
                "contents: connection closed early\n"), '')
        size = int(line)
        data = pipe.read(size)

        self.localcache.write(missingid, lz4.decompress(data))

    def connect(self):
        if self.cacheprocess:
            cmd = "%s %s" % (self.cacheprocess, self.localcache.cachepath)
            self.remotecache.connect(cmd)
        else:
            # If no cache process is specified, we fake one that always
            # returns cache misses.  This enables tests to run easily
            # and may eventually allow us to be a drop in replacement
            # for the largefiles extension.
            class simplecache(object):
                def __init__(self):
                    self.missingids = []
                    self.connected = True

                def close(self):
                    pass

                def request(self, value, flush=True):
                    lines = value.split("\n")
                    if lines[0] != "get":
                        return
                    self.missingids = lines[2:-1]
                    self.missingids.append('0')

                def receiveline(self):
                    if len(self.missingids) > 0:
                        return self.missingids.pop(0)
                    return None

            self.remotecache = simplecache()

    def close(self):
        if fetches and self.debugoutput:
            self.ui.warn(("%s files fetched over %d fetches - " +
                "(%d misses, %0.2f%% hit ratio) over %0.2fs\n") % (
                    fetched,
                    fetches,
                    fetchmisses,
                    float(fetched - fetchmisses) / float(fetched) * 100.0,
                    fetchcost))

        if self.remotecache.connected:
            self.remotecache.close()

    def prefetch(self, fileids, force=False):
        """downloads the given file versions to the cache
        """
        repo = self.repo
        localcache = self.localcache
        storepath = repo.sopener.vfs.base
        reponame = repo.name
        missingids = []
        for file, id in fileids:
            # hack
            # - we don't use .hgtags
            # - workingctx produces ids with length 42,
            #   which we skip since they aren't in any cache
            if file == '.hgtags' or len(id) == 42 or not repo.shallowmatch(file):
                continue

            cachekey = getcachekey(reponame, file, id)
            localkey = getlocalkey(file, id)
            idlocalpath = os.path.join(storepath, 'data', localkey)
            if cachekey in localcache:
                continue
            if not force and os.path.exists(idlocalpath):
                continue

            missingids.append((file, id))

        if missingids:
            global fetches, fetched, fetchcost
            fetches += 1
            fetched += len(missingids)
            start = time.time()
            missingids = self.request(missingids)
            if missingids:
                raise util.Abort(_("unable to download %d files") % len(missingids))
            fetchcost += time.time() - start

class localcache(object):
    def __init__(self, repo):
        self.ui = repo.ui
        self.repo = repo
        self.cachepath = self.ui.config("remotefilelog", "cachepath")
        self._validatecachelog = self.ui.config("remotefilelog", "validatecachelog")
        if self.cachepath:
            self.cachepath = util.expandpath(self.cachepath)
        self.uid = os.getuid()

        if not os.path.exists(self.cachepath):
            oldumask = os.umask(0o002)
            try:
                os.makedirs(self.cachepath)

                groupname = self.ui.config("remotefilelog", "cachegroup")
                if groupname:
                    gid = grp.getgrnam(groupname).gr_gid
                    if gid:
                        os.chown(self.cachepath, os.getuid(), gid)
                        os.chmod(self.cachepath, 0o2775)
            finally:
                os.umask(oldumask)

    def __contains__(self, key):
        path = os.path.join(self.cachepath, key)
        exists = os.path.exists(path)
        if exists and self._validatecachelog and not self._validatekey(path,
            'contains'):
            return False

        return exists

    def write(self, key, data):
        path = os.path.join(self.cachepath, key)
        dirpath = os.path.dirname(path)
        if not os.path.exists(dirpath):
            makedirs(self.cachepath, dirpath, self.uid)

        f = None
        try:
            f = util.atomictempfile(path, 'w')
            f.write(data)
        finally:
            if f:
                f.close()

        if self._validatecachelog:
            if not self._validatekey(path, 'write'):
                raise util.Abort(_("local cache write was corrupted %s") % path)

        stat = os.stat(path)
        if stat.st_uid == self.uid:
            os.chmod(path, 0o0664)

    def read(self, key):
        try:
            path = os.path.join(self.cachepath, key)
            with open(path, "r") as f:
                result = f.read()

            # we should never have empty files
            if not result:
                os.remove(path)
                raise KeyError("empty local cache file %s" % path)

            if self._validatecachelog and not self._validatedata(result):
                with open(self._validatecachelog, 'a+') as f:
                    f.write("corrupt %s during read\n" % path)
                raise KeyError("corrupt local cache file %s" % path)

            return result
        except IOError:
            raise KeyError("key not in local cache")

    def _validatekey(self, path, action):
        with open(path, 'r') as f:
            data = f.read()

        if self._validatedata(data):
            return True

        with open(self._validatecachelog, 'a+') as f:
            f.write("corrupt %s during %s\n" % (path, action))

        os.rename(path, path + ".corrupt")
        return False

    def _validatedata(self, data):
        try:
            if len(data) > 0:
                size = data.split('\0', 1)[0]
                size = int(size)
                if size < len(data):
                    # The data looks to be well formed.
                    return True
        except ValueError:
            pass

        return False

    def markrepo(self):
        repospath = os.path.join(self.cachepath, "repos")
        with open(repospath, 'a') as reposfile:
            reposfile.write(os.path.dirname(self.repo.path) + "\n")

        stat = os.stat(repospath)
        if stat.st_uid == self.uid:
            os.chmod(repospath, 0o0664)

    def gc(self, keepkeys):
        ui = self.ui
        cachepath = self.cachepath
        _removing = _("removing unnecessary files")
        _truncating = _("enforcing cache limit")

        # prune cache
        import Queue
        queue = Queue.PriorityQueue()
        originalsize = 0
        size = 0
        count = 0
        removed = 0

        # keep files newer than a day even if they aren't needed
        limit = time.time() - (60 * 60 * 24)

        ui.progress(_removing, count, unit="files")
        for root, dirs, files in os.walk(cachepath):
            for file in files:
                if file == 'repos':
                    continue

                ui.progress(_removing, count, unit="files")
                path = os.path.join(root, file)
                key = os.path.relpath(path, cachepath)
                count += 1
                stat = os.stat(path)
                originalsize += stat.st_size

                if key in keepkeys or stat.st_atime > limit:
                    queue.put((stat.st_atime, path, stat))
                    size += stat.st_size
                else:
                    os.remove(path)
                    removed += 1
        ui.progress(_removing, None)

        # remove oldest files until under limit
        limit = ui.configbytes("remotefilelog", "cachelimit", "1000 GB")
        if size > limit:
            excess = size - limit
            removedexcess = 0
            while queue and size > limit and size > 0:
                ui.progress(_truncating, removedexcess, unit="bytes", total=excess)
                atime, oldpath, stat = queue.get()
                os.remove(oldpath)
                size -= stat.st_size
                removed += 1
                removedexcess += stat.st_size
        ui.progress(_truncating, None)

        ui.status("finished: removed %s of %s files (%0.2f GB to %0.2f GB)\n" %
                  (removed, count, float(originalsize) / 1024.0 / 1024.0 / 1024.0,
                  float(size) / 1024.0 / 1024.0 / 1024.0))
