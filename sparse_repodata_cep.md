# CEP for Sparse Repodata

We propose a new "repodata" format that can be sparsely fetched. That means, generally, smaller fetches (only fetch what you need) and faster updates of existing repodata (only fetch what has changed).

We also change the encoding from JSON to MSGPACK for faster decoding.

## Motivation

The current repodata format is a JSON file that contains all the packages in a given channel. Unfortunately, that means it grows with the number of packages in the channel. This is a problem for large channels like conda-forge, which has over 150,000+ packages. It becomes very slow to fetch, parse and update the repodata.

## Previous work

In a previous CEP, [JLAP](https://github.com/conda-incubator/ceps/pull/20) was introduced. 
With JLAP only the changes to an initially downloaded `repodata.json` file have to be downloaded which means the user drastically saves on bandwidth which in turn makes fetching repodata much faster. 
However, in practice patching the original repodata can be a very expensive operation, both in terms of memory and in terms of compute.
JLAP also does not save anything with a cold cache which is often the case for CI runners.
Finally, the implementation of JLAP is quite complex which makes it hard to adopt for implementers. 

## Proposal

We propose a "sharded" repodata format. It works by splitting the repodata into multiple files (one per package name) and recursively fetching the "shards".

The shards are stored by the hash of their content (e.g. "content-addressable"). 
That means that the URL of the shard is derived from the content of the shard. This allows for efficient caching and deduplication of shards.

An index file stores the mapping from package name to shard URL.

Although not explicitly required the server SHOULD support HTTP/2 to reduce the overhead of doing a massive number of requests. 

### Repodata shard index

The shard index is a file that is stored under `<channel>/<subdir>/repodata_shards.msgpack.zst`. It is a zstandard compressed `msgpack` file that contains a mapping from package name to shard hash.

We suggest serving the file with a short lived `Cache-Control` `max-age` header of 60 seconds to an hour.

The contents look like the following (written in JSON for readability):

```json
{
  "version": 1,
  "info": {
    "base_url": "https://example.com/channel/subdir/",
    "created_at": "2022-01-01T00:00:00Z",
    "...": "other metadata"
  },
  "shards": {
    // note that the hashes are stored as binary data (hex encoding just for visualization)
    "python": b"ad2c69dfa11125300530f5b390aa0f7536d1f566a6edc8823fd72f9aa33c4910",
    "numpy": b"27ea8f80237eefcb6c587fb3764529620aefb37b9a9d3143dce5d6ba4667583d"
    "...": "other packages"
  }
}
```

### Repodata shard

Individual shards are stored under the URL `<channel>/<subdir>/shards/<sha256>.msgpack.zst`. Where the `sha256` is the lower-case hex representation of the bytes from the index. It is a zstandard compressed msgpack file that contains the metadata of the package.

The files are content-addressable which makes them ideal to be served through a CDN. They SHOULD be served with `Cache-Control: immutable` header.

The shard contains the repodata information that would otherwise have been found in the `repodata.json` file. It is a dictionary that contains the following keys:

**Example (written in JSON for readability):**

```json
{
  "packages": {
    "rich-10.15.2-pyhd8ed1ab_1.tar.bz2": {
      "build": "pyhd8ed1ab_1",
      "build_number": 1,
      "depends": [
        "colorama >=0.4.0,<0.5.0",
        "commonmark >=0.9.0,<0.10.0",
        "dataclasses >=0.7,<0.9",
        "pygments >=2.6.0,<3.0.0",
        "python >=3.6.2",
        "typing_extensions >=3.7.4,<5.0.0"
      ],
      "license": "MIT",
      "license_family": "MIT",
      "md5": "2456071b5d040cba000f72ced5c72032",
      "name": "rich",
      "noarch": "python",
      "sha256": "a38347390191fd3e60b17204f2f6470a013ec8753e1c2e8c9a892683f59c3e40",
      "size": 153963,
      "subdir": "noarch",
      "timestamp": 1638891318904,
      "version": "10.15.2"
    }
  },
  "packages.conda": {
    "rich-13.7.1-pyhd8ed1ab_0.conda": {
      "build": "pyhd8ed1ab_0",
      "build_number": 0,
      "depends": [
        "markdown-it-py >=2.2.0",
        "pygments >=2.13.0,<3.0.0",
        "python >=3.7.0",
        "typing_extensions >=4.0.0,<5.0.0"
      ],
      "license": "MIT",
      "license_family": "MIT",
      "md5": "ba445bf767ae6f0d959ff2b40c20912b",
      "name": "rich",
      "noarch": "python",
      "sha256": "2b26d58aa59e46f933c3126367348651b0dab6e0bf88014e857415bb184a4667",
      "size": 184347,
      "subdir": "noarch",
      "timestamp": 1709150578093,
      "version": "13.7.1"
    }
  }
}
```

## Fetch process

To fetch all needed package records, the client should implement the following steps:

1. Fetch the `repodata_shards.msgpack.zst` file. Standard HTTP caching semantics can be applied to this file.
2. For each package name, start fetching the corresponding hashes from the index file (for both arch & and noarch). 
    Shards can be cached locally and because they are content-addressable no additional round-trips to the server are required to check freshness. The server should also mark these with an `immutable` `Cache-Control` header.
3. Parsing the requirements of the fetched records and add the package names of the requirements to the set of packages to fetch.
4. Loop back to 2. until there are no new package names to fetch.

## Garbage collection

To avoid the cache from growing indefinitely, we propose to implement a garbage collection mechanism that removes shards that have no entry in the index file. The server should keep old shards for a certain amount of time (e.g. 1 week) to allow for clients with older shard-index data to fetch the previous versions.

On the client side, a garbage collection process should run every so often to remove old shards from the cache. This can be done by comparing the cached shards with the index file and removing those that are not referenced anymore.

## Future: Repodata extensions

We propose the following modifications to the current repodata format:

- remove redundant keys: `platform`, `arch` (these can be inferred from `subdir`)

With the total size of the repodata reduced it becomes feasible to add additional fields directly to the repodata records. Exampels are:

- add `purl` as a list of strings (Package URLs to reference to original source of the package) (See: https://github.com/conda-incubator/ceps/pull/63)
- add `run_exports` as a list of strings (run-exports needed to build the package) (See: https://github.com/conda-incubator/ceps/pull/51)

## Future work: Authorization

TODO

## Future work: Update optimization

We want to implement support for smaller index update files. This is done by creating a rolling daily and weekly index update file that can be used instead of fetching the whole `repodata_shards.msgpack.zst` file. The update operation is very simple (just update the hashmap with the new entries).

For this we propose to add the following two files:

- `<channel>/<subdir>/repodata_shards_daily.msgpack.zst`
- `<channel>/<subdir>/repodata_shards_weekly.msgpack.zst`

They will contain the same format as the `repodata_shards.msgpack.zst` file but only contain the packages that have been updated in the last day or week respectively. The `created_at` field in the index file can be used to determine which file to fetch to make sure that the client has the latest information.
