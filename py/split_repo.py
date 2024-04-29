import requests
import json
import boto3
from pathlib import Path
from botocore.exceptions import ClientError
import zstandard as zstd
import hashlib
import msgpack
import sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import argparse
from datetime import datetime
from rich.progress import Progress, track

def download_file(url):
    response = requests.get(url, stream=True)
    response.raise_for_status()  # Raises an HTTPError for bad responses

    # Total size in bytes, might be None if the server doesn't provide it
    total_size = int(response.headers.get('content-length', 0))

    # Initialize the progress bar
    with Progress() as progress:
        task = progress.add_task("[cyan]Downloading...", total=total_size)
        file_content = bytearray()

        # Download the file in chunks
        for data in response.iter_content(chunk_size=4096):
            file_content.extend(data)
            progress.update(task, advance=len(data))

    return file_content


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def split_repo(repo_url, subdir, folder):
    repodata = folder / subdir / "repodata.json"

    if not repodata.parent.exists():
        repodata.parent.mkdir(parents=True)

    if not repodata.exists():
        repo_url = repo_url.rstrip("/")
        response = download_file(f"{repo_url}/{subdir}/repodata.json")
        repodata.write_text(response.text)

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
        hash = sha256(compressed)

        before += len(encoded)
        after_compression += len(compressed)

        shard = shards / f"{hash}.msgpack.zst"
        shard.write_bytes(compressed)

        shards_index["shards"][name] = {"sha256": hash, "size": shard.stat().st_size}

    print("Before compression: ", before)
    print("After compression: ", after_compression)
    print("Percentage saved by zstd: ", (1 - after_compression / before) * 100, "%")

    # write a repodata_shards.json file that has an index of all the shards
    repodata_shards_file = folder / subdir / "repodata_shards.msgpack.zst"
    repodata_shards = compressor.compress(msgpack.dumps(shards_index))
    repodata_shards_file.write_bytes(repodata_shards)
    return package_names

s3_client = boto3.client(
    service_name="s3",
    endpoint_url="https://e1a7cde76f1780ec06bac859036dbaf7.r2.cloudflarestorage.com",
    aws_access_key_id="dd3f46a5410496fe2967363731a3e5f6",
    aws_secret_access_key="8c1af42a037b38ed4f3e344998c31f50a32046bf3d9ef51acadcdad4509ffa07",
    region_name="weur",
)

def upload(file_name: Path, bucket: str, object_name=None):
    # If S3 object_name was not specified, use file_name
    if object_name is None:
        object_name = file_name.name

    print("Uploading: ", object_name)
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


if __name__ == "__main__":

    # Create the parser
    parser = argparse.ArgumentParser(description="Process some integers.")

    # Add arguments
    parser.add_argument('--channel', type=str, help='The channel name')
    parser.add_argument('--cache-dir', type=str, help='The cache directory to use', default="cache")
    parser.add_argument('--subdirs', type=str, nargs='+', help='List of subdirs to clone')
    parser.add_argument('--all-subdirs', help='Whether to clone all subdirs', action='store_true')

    # Parse the arguments
    args = parser.parse_args()
    all_subdirs = ["noarch", "osx-arm64", "linux-64", "win-64", "osx-64", "linux-aarch64", "linux-ppc64le"]
    subdirs = args.subdirs if not args.all_subdirs else all_subdirs

    channel_name = args.channel
    outpath = Path(args.cache_dir) / channel_name
    timestamp = datetime.now().strftime("%Y-%m-%d-%H-%M-%S")

    for subdir in subdirs:
        channel_url = f"https://conda.anaconda.org/{channel_name}/"
        split_repo(channel_url, subdir, outpath)

        with ThreadPoolExecutor(max_workers=50) as executor:
            # first download current index file from the fast-repo
            index_file = outpath / "old" / timestamp / subdir / "repodata_shards.json"
            index_file.parent.mkdir(parents=True, exist_ok=True)
            index_url = f"https://fast.prefiks.dev/{channel_name}/{subdir}/repodata_shards.msgpack.zst"
            
            response = requests.get(index_url)

            shard_hashes = set()
            if response.status_code == 200:
                # decode with zstd and msgpack
                decompressor = zstd.ZstdDecompressor()
                decompressed = decompressor.decompress(response.content)
                index_data = msgpack.loads(decompressed)
                index_file.write_text(json.dumps(index_data))

                # find all shard hashes already in the index
                for name, shard in index_data["shards"].items():
                    shard_hashes.add(shard["sha256"])

            skipped = 0
            total = 0
            # Iterate over all files in the directory
            for file in (outpath / subdir).rglob("**/*"):
                total += 1
                if file.is_file():
                    # Skip the 'repodata.json' file
                    if file.name == "repodata.json":
                        continue

                    # skip if we have the shard already
                    filename = file.name
                    # remove msgpack.zst extension
                    if filename.endswith(".msgpack.zst"):
                        filename = filename[:-12]

                    if filename in shard_hashes:
                        skipped += 1
                        continue

                    # Submit the 'upload' function to the executor for each file
                    object_name = f"{channel_name}/{file.relative_to(outpath)}"
                    print("Uploading: ", object_name)
                    executor.submit(upload, file, "fast-repo", object_name=object_name)

            print(f"Skipped {skipped} out of {total} files")
            print(f"Percentage skipped: {skipped/total*100}%")