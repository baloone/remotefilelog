# shallowrepo.py - shallow repository that uses remote filelogs
#
# Copyright 2013 Facebook, Inc.
#
# This software may be used and distributed according to the terms of the
# GNU General Public License version 2 or any later version.

from mercurial.node import hex, nullid, nullrev, bin
from mercurial.i18n import _
from mercurial import localrepo, context, util, match, scmutil
from mercurial.extensions import wrapfunction
import remotefilelog, remotefilectx, fileserverclient, shallowbundle, os

requirement = "remotefilelog"

def wraprepo(repo):
    class shallowrepository(repo.__class__):
        @util.propertycache
        def name(self):
            return self.ui.config('remotefilelog', 'reponame', '')

        @util.propertycache
        def fallbackpath(self):
            return repo.ui.config("remotefilelog", "fallbackpath",
                    # fallbackrepo is the old, deprecated name
                     repo.ui.config("remotefilelog", "fallbackrepo",
                       repo.ui.config("paths", "default")))

        def file(self, f):
            if f[0] == '/':
                f = f[1:]

            if self.shallowmatch(f):
                return remotefilelog.remotefilelog(self.sopener, f, self)
            else:
                return super(shallowrepository, self).file(f)

        def filectx(self, path, changeid=None, fileid=None):
            if self.shallowmatch(path):
                return remotefilectx.remotefilectx(self, path, changeid, fileid)
            else:
                return super(shallowrepository, self).filectx(path, changeid, fileid)

        def prefetch(self, revs, base=None, pats=None, opts=None):
            """Prefetches all the necessary file revisions for the given revs
            """
            fallbackpath = self.fallbackpath
            if fallbackpath:
                # If we know a rev is on the server, we should fetch the server
                # version of those files, since our local file versions might
                # become obsolete if the local commits are stripped.
                localrevs = repo.revs('outgoing(%s)', fallbackpath)
                if base is not None and base != nullrev:
                    serverbase = list(repo.revs('first(reverse(::%s) - %ld)', base,
                                     localrevs))
                    if serverbase:
                        base = serverbase[0]
            else:
                localrevs = repo

            mf = repo.manifest
            if base is not None:
                mfdict = mf.read(repo[base].manifestnode())
                skip = set(mfdict.iteritems())
            else:
                skip = set()

            # Copy the skip set to start large and avoid constant resizing,
            # and since it's likely to be very similar to the prefetch set.
            files = skip.copy()
            serverfiles = skip.copy()
            visited = set()
            visited.add(nullrev)
            for rev in sorted(revs):
                ctx = repo[rev]
                if pats:
                    m = scmutil.match(ctx, pats, opts)

                mfnode = ctx.manifestnode()
                mfrev = mf.rev(mfnode)

                # Decompressing manifests is expensive.
                # When possible, only read the deltas.
                p1, p2 = mf.parentrevs(mfrev)
                if p1 in visited and p2 in visited:
                    mfdict = mf.readfast(mfnode)
                else:
                    mfdict = mf.read(mfnode)

                diff = (pf for pf in mfdict.iteritems() if not pats or m(pf[0]))
                if rev not in localrevs:
                    serverfiles.update(diff)
                else:
                    files.update(diff)

                visited.add(mfrev)

            files.difference_update(skip)
            serverfiles.difference_update(skip)

            # Fetch files known to be on the server
            if serverfiles:
                results = [(path, hex(fnode)) for (path, fnode) in serverfiles]
                repo.fileservice.prefetch(results, force=True)

            # Fetch files that may or may not be on the server
            if files:
                results = [(path, hex(fnode)) for (path, fnode) in files]
                repo.fileservice.prefetch(results)

    # Wrap dirstate.status here so we can prefetch all file nodes in
    # the lookup set before localrepo.status uses them.
    def status(orig, match, subrepos, ignored, clean, unknown):
        lookup, modified, added, removed, deleted, unknown, ignored, \
            clean = orig(match, subrepos, ignored, clean, unknown)

        if lookup:
            files = []
            parents = repo.parents()
            for fname in lookup:
                for ctx in parents:
                    if fname in ctx:
                        fnode = ctx.filenode(fname)
                        files.append((fname, hex(fnode)))

            repo.fileservice.prefetch(files)

        return (lookup, modified, added, removed, deleted, unknown, \
                ignored, clean)

    wrapfunction(repo.dirstate, 'status', status)

    repo.__class__ = shallowrepository

    repo.shallowmatch = match.always(repo.root, '')
    repo.fileservice = fileserverclient.fileserverclient(repo)

    repo.includepattern = repo.ui.configlist("remotefilelog", "includepattern", None)
    repo.excludepattern = repo.ui.configlist("remotefilelog", "excludepattern", None)
    if repo.includepattern or repo.excludepattern:
        repo.shallowmatch = match.match(repo.root, '', None,
            repo.includepattern, repo.excludepattern)

    localpath = os.path.join(repo.sopener.vfs.base, 'data')
    if not os.path.exists(localpath):
        os.makedirs(localpath)
