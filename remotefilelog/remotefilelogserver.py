# remotefilelogserver.py - server logic for a remotefilelog server
#
# Copyright 2013 Facebook, Inc.
#
# This software may be used and distributed according to the terms of the
# GNU General Public License version 2 or any later version.

from mercurial import wireproto, changegroup, match, util, changelog
from mercurial.extensions import wrapfunction
from mercurial.node import bin, hex, nullid, nullrev
from mercurial.i18n import _
import shallowrepo
import stat, os, lz4, time

def setupserver(ui, repo):
    """Sets up a normal Mercurial repo so it can serve files to shallow repos.
    """
    onetimesetup(ui)

    # don't send files to shallow clients during pulls
    def generatefiles(orig, self, changedfiles, linknodes, commonrevs, source):
        caps = self._bundlecaps or []
        if shallowrepo.requirement in caps:
            # only send files that don't match the specified patterns
            includepattern = None
            excludepattern = None
            for cap in (self._bundlecaps or []):
                if cap.startswith("includepattern="):
                    includepattern = cap[len("includepattern="):].split('\0')
                elif cap.startswith("excludepattern="):
                    excludepattern = cap[len("excludepattern="):].split('\0')

            m = match.always(repo.root, '')
            if includepattern or excludepattern:
                m = match.match(repo.root, '', None,
                    includepattern, excludepattern)

            changedfiles = list([f for f in changedfiles if not m(f)])
        return orig(self, changedfiles, linknodes, commonrevs, source)

    wrapfunction(changegroup.cg1packer, 'generatefiles', generatefiles)

    # add incoming hook to continuously generate file blobs
    ui.setconfig("hooks", "changegroup.remotefilelog", incominghook)

onetime = False
def onetimesetup(ui):
    """Configures the wireprotocol for both clients and servers. 
    """
    global onetime
    if onetime:
        return
    onetime = True

    # support file content requests
    wireproto.commands['getfiles'] = (getfiles, '')

    class streamstate(object):
        match = None
        shallowremote = False
    state = streamstate()

    def stream_out_shallow(repo, proto, other):
        includepattern = None
        excludepattern = None
        raw = other.get('includepattern')
        if raw:
            includepattern = raw.split('\0')
        raw = other.get('excludepattern')
        if raw:
            excludepattern = raw.split('\0')

        oldshallow = state.shallowremote
        oldmatch = state.match
        try:
            state.shallowremote = True
            state.match = match.always(repo.root, '')
            if includepattern or excludepattern:
                state.match = match.match(repo.root, '', None,
                    includepattern, excludepattern)
            return wireproto.stream(repo, proto)
        finally:
            state.shallowremote = oldshallow
            state.match = oldmatch

    wireproto.commands['stream_out_shallow'] = (stream_out_shallow, '*')

    # don't clone filelogs to shallow clients
    def _walkstreamfiles(orig, repo):
        if state.shallowremote:
            # if we are shallow ourselves, stream our local commits
            if shallowrepo.requirement in repo.requirements:
                striplen = len(repo.store.path) + 1
                readdir = repo.store.rawvfs.readdir
                visit = [os.path.join(repo.store.path, 'data')]
                while visit:
                    p = visit.pop()
                    for f, kind, st in readdir(p, stat=True):
                        fp = p + '/' + f
                        if kind == stat.S_IFREG:
                            if not fp.endswith('.i') and not fp.endswith('.d'):
                                n = util.pconvert(fp[striplen:])
                                yield (store.decodedir(n), n, st.st_size)
                        if kind == stat.S_IFDIR:
                            visit.append(fp)

            # Return .d and .i files that do not match the shallow pattern
            match = state.match or match.always(repo.root, '')
            for (u, e, s) in repo.store.datafiles():
                f = u[5:-2]  # trim data/...  and .i/.d
                if not state.match(f):
                    yield (u, e, s)

            for x in repo.store.topfiles():
                yield x
        elif shallowrepo.requirement in repo.requirements:
            # don't allow cloning from a shallow repo to a full repo
            # since it would require fetching every version of every
            # file in order to create the revlogs.
            raise util.Abort(_("Cannot clone from a shallow repo "
                             + "to a full repo."))
        else:
            for x in orig(repo):
                yield x

    wrapfunction(wireproto, '_walkstreamfiles', _walkstreamfiles)

    # We no longer use getbundle_shallow commands, but we must still
    # support it for migration purposes
    def getbundleshallow(repo, proto, others):
        bundlecaps = others.get('bundlecaps', '')
        bundlecaps = set(bundlecaps.split(','))
        bundlecaps.add('remotefilelog')
        others['bundlecaps'] = ','.join(bundlecaps)

        return wireproto.commands["getbundle"][0](repo, proto, others)

    wireproto.commands["getbundle_shallow"] = (getbundleshallow, '*')

    # expose remotefilelog capabilities
    def capabilities(orig, repo, proto):
        caps = orig(repo, proto)
        if (shallowrepo.requirement in repo.requirements or
            ui.configbool('remotefilelog', 'server')):
            caps += " " + shallowrepo.requirement
        return caps
    wrapfunction(wireproto, 'capabilities', capabilities)

def getfiles(repo, proto):
    """A server api for requesting particular versions of particular files.
    """
    if shallowrepo.requirement in repo.requirements:
        raise util.Abort(_('cannot fetch remote files from shallow repo'))

    def streamer():
        fin = proto.fin
        opener = repo.sopener

        cachepath = repo.ui.config("remotefilelog", "servercachepath")
        if not cachepath:
            cachepath = os.path.join(repo.path, "remotefilelogcache")

        # everything should be user & group read/writable
        oldumask = os.umask(0o002)
        try:
            while True:
                request = fin.readline()[:-1]
                if not request:
                    break

                node = bin(request[:40])
                if node == nullid:
                    yield '0\n'
                    continue

                path = request[40:]

                filecachepath = os.path.join(cachepath, path, hex(node))
                if not os.path.exists(filecachepath):
                    filectx = repo.filectx(path, fileid=node)
                    if filectx.node() == nullid:
                        repo.changelog = changelog.changelog(repo.sopener)
                        filectx = repo.filectx(path, fileid=node)

                    text = createfileblob(filectx)
                    text = lz4.compressHC(text)

                    dirname = os.path.dirname(filecachepath)
                    if not os.path.exists(dirname):
                        os.makedirs(dirname)
                    try:
                        with open(filecachepath, "w") as f:
                            f.write(text)
                    except IOError:
                        # Don't abort if the user only has permission to read,
                        # and not write.
                        pass
                else:
                    with open(filecachepath, "r") as f:
                        text = f.read()

                yield '%d\n%s' % (len(text), text)

                # it would be better to only flush after processing a whole batch
                # but currently we don't know if there are more requests coming
                proto.fout.flush()
        finally:
            os.umask(oldumask)

    return wireproto.streamres(streamer())


def incominghook(ui, repo, node, source, url, **kwargs):
    """Server hook that produces the shallow file blobs immediately after
    a commit, in anticipation of them being requested soon.
    """
    cachepath = repo.ui.config("remotefilelog", "servercachepath")
    if not cachepath:
        cachepath = os.path.join(repo.path, "remotefilelogcache")

    heads = repo.revs("heads(%s::)" % node)

    # everything should be user & group read/writable
    oldumask = os.umask(0o002)
    try:
        count = 0
        for head in heads:
            mf = repo[head].manifest()
            for filename, filenode in mf.iteritems():
                filecachepath = os.path.join(cachepath, filename, hex(filenode))
                if os.path.exists(filecachepath):
                    continue

                # This can be a bit slow. Don't block the commit returning
                # for large commits.
                if count > 500:
                    break
                count += 1

                filectx = repo.filectx(filename, fileid=filenode)

                text = createfileblob(filectx)
                text = lz4.compressHC(text)

                dirname = os.path.dirname(filecachepath)
                if not os.path.exists(dirname):
                    os.makedirs(dirname)
                f = open(filecachepath, "w")
                try:
                    f.write(text)
                finally:
                    f.close()
    finally:
        os.umask(oldumask)


def createfileblob(filectx):
    text = filectx.data()

    ancestors = [filectx]
    ancestors.extend([f for f in filectx.ancestors()])

    ancestortext = ""
    for ancestorctx in ancestors:
        parents = ancestorctx.parents()
        p1 = nullid
        p2 = nullid
        if len(parents) > 0:
            p1 = parents[0].filenode()
        if len(parents) > 1:
            p2 = parents[1].filenode()

        copyname = ""
        rename = ancestorctx.renamed()
        if rename:
            copyname = rename[0]
        linknode = ancestorctx.node()
        ancestortext += "%s%s%s%s%s\0" % (
            ancestorctx.filenode(), p1, p2, linknode,
            copyname)

    return "%d\0%s%s" % (len(text), text, ancestortext)

def gcserver(ui, repo):
    if not repo.ui.configbool("remotefilelog", "server"):
        return

    neededfiles = set()
    heads = repo.revs("heads(all())")

    cachepath = repo.join("remotefilelogcache")
    for head in heads:
        mf = repo[head].manifest()
        for filename, filenode in mf.iteritems():
            filecachepath = os.path.join(cachepath, filename, hex(filenode))
            neededfiles.add(filecachepath)

    # delete unneeded older files
    days = repo.ui.configint("remotefilelog", "serverexpiration", 30)
    expiration = time.time() - (days * 24 * 60 * 60)

    _removing = _("removing old server cache")
    count = 0
    ui.progress(_removing, count, unit="files")
    for root, dirs, files in os.walk(cachepath):
        for file in files:
            filepath = os.path.join(root, file)
            count += 1
            ui.progress(_removing, count, unit="files")
            if filepath in neededfiles:
                continue

            stat = os.stat(filepath)
            if stat.st_mtime < expiration:
                os.remove(filepath)

    ui.progress(_removing, None)
