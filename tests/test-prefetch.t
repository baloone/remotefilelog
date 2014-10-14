  $ . "$TESTDIR/library.sh"

  $ hginit master
  $ cd master
  $ cat >> .hg/hgrc <<EOF
  > [remotefilelog]
  > server=True
  > EOF
  $ echo x > x
  $ echo z > z
  $ hg commit -qAm x
  $ echo x2 > x
  $ echo y > y
  $ hg commit -qAm y
  $ hg bookmark foo

  $ cd ..

# prefetch a revision

  $ hgcloneshallow ssh://user@dummy/master shallow --noupdate
  streaming all changes
  2 files to transfer, 528 bytes of data
  transferred 528 bytes in 0.0 seconds (*/sec) (glob)
  $ cd shallow

  $ hg prefetch -r 0
  2 files fetched over 1 fetches - (2 misses, 0.00% hit ratio) over *s (glob)

  $ hg cat -r 0 x
  x

# prefetch a range of revisions

  $ clearcache
  $ hg prefetch -r 0::1
  4 files fetched over 1 fetches - (4 misses, 0.00% hit ratio) over *s (glob)

  $ hg cat -r 0 x
  x
  $ hg cat -r 1 x
  x2

# prefetch certain files

  $ clearcache
  $ hg prefetch -r 1 x
  1 files fetched over 1 fetches - (1 misses, 0.00% hit ratio) over *s (glob)

  $ hg cat -r 1 x
  x2

  $ hg cat -r 1 y
  y
  1 files fetched over 1 fetches - (1 misses, 0.00% hit ratio) over *s (glob)

# prefetch on pull when configured

  $ printf "[remotefilelog]\npullprefetch=bookmark()\n" >> .hg/hgrc
  $ hg strip tip^
  saved backup bundle to $TESTTMP/shallow/.hg/strip-backup/97b1645a8f26-backup.hg

  $ clearcache
  $ hg pull
  pulling from ssh://user@dummy/master
  requesting all changes
  adding changesets
  adding manifests
  adding file changes
  added 2 changesets with 0 changes to 0 files
  adding remote bookmark foo
  (run 'hg update' to get a working copy)
  prefetching file contents
  3 files fetched over 1 fetches - (3 misses, 0.00% hit ratio) over *s (glob)

  $ hg up tip
  3 files updated, 0 files merged, 0 files removed, 0 files unresolved

# prefetch only fetches changes not in working copy

  $ hg strip tip
  1 files updated, 0 files merged, 1 files removed, 0 files unresolved
  saved backup bundle to $TESTTMP/shallow/.hg/strip-backup/109c3a557a73-backup.hg
  1 files fetched over 1 fetches - (1 misses, 0.00% hit ratio) over *s (glob)
  $ clearcache

  $ hg pull
  pulling from ssh://user@dummy/master
  searching for changes
  adding changesets
  adding manifests
  adding file changes
  added 1 changesets with 0 changes to 0 files
  updating bookmark foo
  (run 'hg update' to get a working copy)
  prefetching file contents
  2 files fetched over 1 fetches - (2 misses, 0.00% hit ratio) over *s (glob)

# Make some local commits that produce the same file versions as are on the
# server. To simulate a situation where we have local commits that were somehow
# pushed, and we will soon pull.

  $ hg prefetch -r 'all()'
  2 files fetched over 1 fetches - (2 misses, 0.00% hit ratio) over *s (glob)
  $ hg strip -q -r 0
  $ echo x > x
  $ echo z > z
  $ hg commit -qAm x
  $ echo x2 > x
  $ echo y > y
  $ hg commit -qAm y

# prefetch server versions, even if local versions are available

  $ clearcache
  $ hg strip -q tip
  $ hg pull
  pulling from ssh://user@dummy/master
  searching for changes
  adding changesets
  adding manifests
  adding file changes
  added 1 changesets with 0 changes to 0 files
  updating bookmark foo
  (run 'hg update' to get a working copy)
  prefetching file contents
  2 files fetched over 1 fetches - (2 misses, 0.00% hit ratio) over *s (glob)
