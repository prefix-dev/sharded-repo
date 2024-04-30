# Shared Repodata

This is a showcase for the [conda sharded repodata proposal](https://github.com/conda-incubator/ceps/pull/75).

The CI of this repository generates sharded repodata mirrors for some popular channels (conda-forge, robostack, bioconda, etc.) and uploads the files to a cloudflare R2 bucket. The files are then made available through `https://fast.prefiks.dev`.
