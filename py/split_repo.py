import requests
import os
import json
import boto3
from pathlib import Path
from botocore.exceptions import ClientError
import zstandard as zstd
import hashlib
import msgpack
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import argparse
from datetime import datetime
from rich.progress import (
    Progress,
    track,
    TransferSpeedColumn,
    TimeElapsedColumn,
    BarColumn,
)


def download_file(url):
    response = requests.get(url, stream=True)
    response.raise_for_status()  # Raises an HTTPError for bad responses

    # Total size in bytes, might be None if the server doesn't provide it
    total_size = int(response.headers.get("content-length", 0)) or None

    # Initialize the progress bar
    with Progress(BarColumn(), TransferSpeedColumn(), TimeElapsedColumn()) as progress:
        task = progress.add_task("[cyan]Downloading...", total=total_size)
        file_content = bytearray()

        # Download the file in chunks
        for data in response.iter_content(chunk_size=4096):
            file_content.extend(data)
            progress.update(task, advance=len(data))

    if url.endswith(".zst"):
        file_content = zstd.decompress(file_content)

    return file_content


def sha256(data):
    hash = hashlib.sha256(data)
    return hash.digest(), hash.hexdigest()


def split_repo(repo_url, subdir, folder):
    repodata = folder / subdir / "repodata.json"

    if not repodata.parent.exists():
        repodata.parent.mkdir(parents=True)

    if not repodata.exists():
        repo_url = repo_url.rstrip("/")
        if "conda-forge" in repo_url:
            response = download_file(f"{repo_url}/{subdir}/repodata.json.zst")
        else:
            response = download_file(f"{repo_url}/{subdir}/repodata.json")
        repodata.write_bytes(response)
    else:
        print(f"Skipping download of {subdir}/repodata.json. Using cached file.")
    # Parse repodata.json and split into shards
    repodata = json.loads(repodata.read_text())
    packages = repodata["packages"]
    package_names = dict()
    for fn, package in packages.items():
        name = package["name"]
        if name not in package_names:
            package_names[name] = []
        package_names[name].append(fn)

    conda_packages = repodata["packages.conda"]
    conda_package_names = dict()
    for fn, package in conda_packages.items():
        name = package["name"]
        if name not in conda_package_names:
            conda_package_names[name] = []
        conda_package_names[name].append(fn)

    all_names = set(package_names.keys()) | set(conda_package_names.keys())

    # write out the shards into `folder/shards/<package_name>.json`
    shards = folder / subdir / "shards"
    if shards.exists():
        for file in shards.glob("*.msgpack.zst"):
            file.unlink()

    shards.mkdir(exist_ok=True)
    shards_index = {"info": repodata["info"], "shards": {}}
    shards_index["info"]["base_url"] = repo_url
    compressor = zstd.ZstdCompressor(level=19)

    before = 0
    after_compression = 0

    # create a rich progress bar
    for name in track(all_names, description=f"Processing {subdir}"):
        d = {"packages": {fn: packages[fn] for fn in package_names.get(name, [])}}
        d["packages.conda"] = {
            fn: conda_packages[fn] for fn in conda_package_names.get(name, [])
        }

        encoded = msgpack.dumps(d)
        # encode with zstd
        compressed = compressor.compress(encoded)
        # use the sha hash of the compressed data as the filename
        digest, hexdigest = sha256(compressed)

        before += len(encoded)
        after_compression += len(compressed)

        shard = shards / f"{hexdigest}.msgpack.zst"
        shard.write_bytes(compressed)

        # store the byte digest of the shard / compressed data
        shards_index["shards"][name] = digest

    print("Before compression: ", before)
    print("After compression: ", after_compression)
    if before > 0:
        print("Percentage saved by zstd: ", (1 - after_compression / before) * 100, "%")

    # write a repodata_shards.json file that has an index of all the shards
    repodata_shards_file = folder / subdir / "repodata_shards.msgpack.zst"
    repodata_shards = compressor.compress(msgpack.dumps(shards_index))
    repodata_shards_file.write_bytes(repodata_shards)
    return package_names


s3_client = boto3.client(
    service_name="s3",
    endpoint_url="https://e1a7cde76f1780ec06bac859036dbaf7.r2.cloudflarestorage.com",
    aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
    aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
    region_name="weur",
)


def upload(file_name: Path, bucket: str, object_name=None):
    # If S3 object_name was not specified, use file_name
    if object_name is None:
        object_name = file_name.name

    if file_name.name.startswith("repodata"):
        cache = "public, max-age=36000"
    else:
        cache = "public, max-age=31536000, immutable"

    try:
        response = s3_client.upload_file(
            file_name,
            bucket,
            object_name,
            ExtraArgs={
                "CacheControl": cache,
            },
        )
        print("Upload successful: ", file_name)
    except ClientError as e:
        print(e)
        return False
    return True


def files_to_upload(outpath, timestamp, subdir, channel_name):
    # first download current index file from the fast-repo
    index_file = outpath / "old" / timestamp / subdir / "repodata_shards.msgpack"
    index_file.parent.mkdir(parents=True, exist_ok=True)
    index_url = f"https://fast.prefiks.dev/{channel_name}/{subdir}/repodata_shards.msgpack.zst?bust_cache={timestamp}"

    response = requests.get(index_url)
    files = []
    shard_hashes = set()
    if response.status_code == 200:
        # decode with zstd and msgpack
        decompressor = zstd.ZstdDecompressor()
        decompressed = decompressor.decompress(response.content)
        index_data = msgpack.loads(decompressed)
        index_file.write_bytes(decompressed)

        # find all shard hashes already in the index
        print("Reading shard hashes from index file")
        for name, shard in index_data["shards"].items():
            if isinstance(shard, bytes):
                shard: bytes = shard
                shard_hashes.add(shard.hex())
            else:
                shard_hashes.add(shard["sha256"])

    skipped = 0
    total = 0
    # Iterate over all files in the directory
    for file in (outpath / subdir).rglob("**/*"):
        if file.is_file():
            # Skip the 'repodata.json' file
            if file.name.startswith("repodata.json"):
                continue

            # skip if we have the shard already
            filename = file.name
            # remove msgpack.zst extension
            if filename.endswith(".msgpack.zst"):
                filename = filename[:-12]

            total += 1
            if filename in shard_hashes:
                skipped += 1
                continue

            # Submit the 'upload' function to the executor for each file
            object_name = f"{channel_name}/{file.relative_to(outpath)}"
            print("Uploading: ", object_name)
            files.append((file, object_name))

    print(f"Skipped {skipped} out of {total} files")
    print(f"Percentage skipped: {skipped/total*100}%")

    return files


if __name__ == "__main__":
    # Create the parser
    parser = argparse.ArgumentParser(description="Process some integers.")

    # Add arguments
    parser.add_argument("--channel", type=str, help="The channel name")
    parser.add_argument(
        "--cache-dir", type=str, help="The cache directory to use", default="cache"
    )
    parser.add_argument(
        "--subdirs", type=str, nargs="+", help="List of subdirs to clone"
    )
    parser.add_argument(
        "--all-subdirs", help="Whether to clone all subdirs", action="store_true"
    )

    # Parse the arguments
    args = parser.parse_args()
    all_subdirs = [
        "noarch",
        "osx-arm64",
        "linux-64",
        "win-64",
        "osx-64",
        "linux-aarch64",
        "linux-ppc64le",
    ]
    subdirs = args.subdirs if not args.all_subdirs else all_subdirs

    final_subdirs = []
    for s in subdirs:
        if "," in s:
            final_subdirs.extend(s.split(","))
        else:
            final_subdirs.append(s)
    subdirs = final_subdirs

    channel_name = args.channel
    outpath = Path(args.cache_dir) / channel_name
    timestamp = datetime.now().strftime("%Y-%m-%d-%H-%M-%S")

    for subdir in subdirs:
        channel_url = f"https://conda.anaconda.org/{channel_name}/"
        split_repo(channel_url, subdir, outpath)

        files = files_to_upload(outpath, timestamp, subdir, channel_name)

        with ThreadPoolExecutor(max_workers=50) as executor:
            for file, object_name in files:
                executor.submit(upload, file, "fast-repo", object_name=object_name)
