from minio.error import S3Error

from backend.main import settings, storage_client


def main():
    client, bucket = storage_client(), settings().minio_bucket
    if not client.bucket_exists(bucket):
        try:
            client.make_bucket(bucket)
        except S3Error as exc:
            if exc.code not in {"BucketAlreadyOwnedByYou", "BucketAlreadyExists"}:
                raise


if __name__ == "__main__":
    main()
