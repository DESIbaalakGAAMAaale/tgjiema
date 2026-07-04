import hashlib
import hmac
import datetime

import httpx


class R2Storage:
    def __init__(self):
        self._http: httpx.AsyncClient = None
        self._account_id: str = ""
        self._access_key: str = ""
        self._secret_key: str = ""
        self._bucket: str = ""
        self._endpoint: str = ""

    def configure(
        self, account_id: str, access_key: str, secret_key: str,
        bucket: str, endpoint: str = None,
    ):
        self._account_id = account_id
        self._access_key = access_key
        self._secret_key = secret_key
        self._bucket = bucket
        self._endpoint = endpoint or f"{account_id}.r2.cloudflarestorage.com"

    async def connect(self):
        self._http = httpx.AsyncClient(timeout=120)

    async def close(self):
        if self._http:
            await self._http.aclose()
            self._http = None

    @property
    def base_url(self) -> str:
        return f"https://{self._endpoint}/{self._bucket}"

    def _sign(self, method: str, key: str, content_type: str = "",
              payload_hash: str = "UNSIGNED-PAYLOAD", querystring: str = "") -> dict:
        service = "s3"
        region = "auto"
        now = datetime.datetime.now(datetime.timezone.utc)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        date_stamp = now.strftime("%Y%m%d")

        canonical_uri = "/" + self._bucket + "/" + key
        canonical_querystring = querystring
        canonical_headers = (
            f"host:{self._endpoint}\n"
            f"x-amz-content-sha256:{payload_hash}\n"
            f"x-amz-date:{amz_date}\n"
        )
        signed_headers = "host;x-amz-content-sha256;x-amz-date"

        canonical_request = (
            f"{method}\n{canonical_uri}\n{canonical_querystring}\n"
            f"{canonical_headers}\n{signed_headers}\n{payload_hash}"
        )

        algorithm = "AWS4-HMAC-SHA256"
        credential_scope = f"{date_stamp}/{region}/{service}/aws4_request"
        string_to_sign = (
            f"{algorithm}\n{amz_date}\n{credential_scope}\n"
            f"{hashlib.sha256(canonical_request.encode()).hexdigest()}"
        )

        def sign(key, msg):
            return hmac.new(key, msg.encode(), hashlib.sha256).digest()

        k_date = sign(b"AWS4" + self._secret_key.encode(), date_stamp)
        k_region = sign(k_date, region)
        k_service = sign(k_region, service)
        k_signing = sign(k_service, "aws4_request")
        signature = hmac.new(k_signing, string_to_sign.encode(), hashlib.sha256).hexdigest()

        return {
            "Authorization": (
                f"{algorithm} Credential={self._access_key}/{credential_scope}, "
                f"SignedHeaders={signed_headers}, Signature={signature}"
            ),
            "x-amz-content-sha256": payload_hash,
            "x-amz-date": amz_date,
        }

    async def upload(self, key: str, data: bytes, content_type: str = "application/octet-stream"):
        if self._http is None:
            raise RuntimeError("R2Storage not connected, call connect() first")
        url = f"{self.base_url}/{key}"
        payload_hash = hashlib.sha256(data).hexdigest()
        headers = self._sign("PUT", key, content_type, payload_hash=payload_hash)
        headers["Content-Type"] = content_type
        resp = await self._http.put(url, headers=headers, content=data)
        resp.raise_for_status()
        return key

    async def download(self, key: str) -> bytes:
        if self._http is None:
            raise RuntimeError("R2Storage not connected, call connect() first")
        url = f"{self.base_url}/{key}"
        headers = self._sign("GET", key)
        resp = await self._http.get(url, headers=headers)
        resp.raise_for_status()
        return resp.content

    async def list_objects(self, prefix: str = "", max_keys: int = 1000) -> list[dict]:
        """列出 R2 存储桶中指定前缀的对象。返回 [{"key": ..., "size": ..., "last_modified": ...}, ...]"""
        if self._http is None:
            raise RuntimeError("R2Storage not connected, call connect() first")
        import xml.etree.ElementTree as ET
        ns = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}
        result = []
        continuation_token = None
        per_request = min(max_keys, 1000)

        while True:
            query = f"list-type=2&prefix={prefix}&max-keys={per_request}"
            if continuation_token:
                query += f"&continuation-token={continuation_token}"
            url = f"{self.base_url}?{query}"
            headers = self._sign("GET", "", querystring=query)
            resp = await self._http.get(url, headers=headers)
            resp.raise_for_status()
            root = ET.fromstring(resp.text)

            for contents in root.findall("s3:Contents", ns):
                key = contents.find("s3:Key", ns)
                size = contents.find("s3:Size", ns)
                modified = contents.find("s3:LastModified", ns)
                result.append({
                    "key": key.text if key is not None else "",
                    "size": int(size.text) if size is not None and size.text else 0,
                    "last_modified": modified.text if modified is not None else "",
                })

            if len(result) >= max_keys:
                break

            is_truncated = root.find("s3:IsTruncated", ns)
            if is_truncated is None or is_truncated.text != "true":
                break

            next_token = root.find("s3:NextContinuationToken", ns)
            if next_token is None or not next_token.text:
                break
            continuation_token = next_token.text

        return result[:max_keys]

    async def delete(self, key: str):
        if self._http is None:
            raise RuntimeError("R2Storage not connected, call connect() first")
        url = f"{self.base_url}/{key}"
        headers = self._sign("DELETE", key)
        resp = await self._http.delete(url, headers=headers)
        resp.raise_for_status()


_r2: R2Storage = R2Storage()


def get_r2() -> R2Storage:
    return _r2


async def init_r2():
    from config import settings as _settings

    _r2.configure(
        account_id=_settings.R2_ACCOUNT_ID,
        access_key=_settings.R2_ACCESS_KEY_ID,
        secret_key=_settings.R2_SECRET_ACCESS_KEY,
        bucket=_settings.R2_BUCKET_NAME,
        endpoint=_settings.R2_ENDPOINT if _settings.R2_ENDPOINT else None,
    )
    await _r2.connect()


async def close_r2():
    await _r2.close()