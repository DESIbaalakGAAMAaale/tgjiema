"""R82: S3 object key path encoding regression tests."""

from __future__ import annotations

from urllib.parse import urlsplit
from unittest.mock import AsyncMock, MagicMock

import pytest

from storage.r2 import R2Storage


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["upload", "download", "delete"])
async def test_s3_object_key_preserves_path_separator(method: str):
    store = R2Storage()
    store.configure(
        account_id="",
        access_key="access",
        secret_key="secret",
        bucket="tgjiema-backup",
        endpoint="http://minio:9000",
    )
    response = MagicMock()
    response.content = b"payload"
    client = AsyncMock()
    client.put.return_value = response
    client.get.return_value = response
    client.delete.return_value = response
    store._http = client

    key = "db_backup/payload with space.enc"
    if method == "upload":
        await store.upload(key, b"payload")
        url = client.put.await_args.args[0]
    elif method == "download":
        await store.download(key)
        url = client.get.await_args.args[0]
    else:
        await store.delete(key)
        url = client.delete.await_args.args[0]

    assert "/db_backup/payload%20with%20space.enc" in url
    assert "%2F" not in url
    assert store._canonical_uri(key) == urlsplit(url).path
