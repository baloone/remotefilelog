  $ . "$TESTDIR/library.sh"

  $ hginit master
  $ cd master
  $ cat >> .hg/hgrc <<EOF
  > [remotefilelog]
  > server=True
  > serverexpiration=-1
  > EOF
  $ echo x > x
  $ hg commit -qAm x
  $ cd ..

  $ hgcloneshallow ssh://user@dummy/master shallow -q
  1 files fetched over 1 fetches - (1 misses, 0.00% hit ratio) over *s (glob)

# commit a new version of x so we can gc the old one

  $ cd master
  $ echo y > x
  $ hg commit -qAm y
  $ cd ..

  $ cd shallow
  $ hg pull -q
  $ hg update -q
  1 files fetched over 1 fetches - (1 misses, 0.00% hit ratio) over *s (glob)
  $ cd ..

# gc client cache

  $ find $CACHEDIR -type f -exec touch -d "last week" {} \;

  $ find $CACHEDIR -type f | wc -l
  3
  $ hg gc
  finished: removed 1 of 2 files (0.00 GB to 0.00 GB)
  $ find $CACHEDIR -type f | wc -l
  2

# gc server cache

  $ find master/.hg/remotefilelogcache -type f | wc -l
  2
  $ hg gc master
  finished: removed 0 of 1 files (0.00 GB to 0.00 GB)
  $ find master/.hg/remotefilelogcache -type f | wc -l
  1
