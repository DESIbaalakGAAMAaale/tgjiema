import hashlib
import hmac
import datetime
from urllib.parse import quote

import httpx


class R2Storage:
    """S3 兼容对象存储客户端(支持 Cloudflare R2 和 MinIO)。

    R76 O7 / 10.G: 统一对象存储接口,通过 ``configure()`` 的 ``endpoint`` 参数
    切换 R2(HTTPS, virtual-hosted)和 MinIO(HTTP, path-style)。
    ``base_url`` 自动检测 endpoint 是否包含协议前缀(http:// 或 https://),
    若无则默认 HTTPS(R2 兼容)。
    """

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
        """配置存储客户端。

        Args:
            account_id: R2 account ID(MinIO 模式可留空)
            access_key: S3 access key
            secret_key: S3 secret key
            bucket: bucket 名称
            endpoint: 完整 endpoint URL 或主机名。
                - R2: ``<account_id>.r2.cloudflarestorage.com``(无协议,默认 HTTPS)
                - MinIO: ``http://minio:9000``(含协议,支持 HTTP for CI)
        """
        self._account_id = account_id
        self._access_key = access_key
        self._secret_key = secret_key
        self._bucket = bucket
        self._endpoint = endpoint or f"{account_id}.r2.cloudflarestorage.com"

    async def connect(self):
        # 防止反复调用导致旧 client 泄漏:先关闭旧的再创建新的
        if self._http is not None:
            try:
                await self._http.aclose()
            except Exception:
                pass
        self._http = httpx.AsyncClient(timeout=120)

    async def close(self):
        if self._http:
            await self._http.aclose()
            self._http = None

    @property
    def base_url(self) -> str:
        """构造 base URL,自动检测 endpoint 是否包含协议前缀。

        - endpoint 含 ``http://`` 或 ``https://`` → 直接使用(MinIO CI 模式)
        - endpoint 不含协议 → 默认 HTTPS(R2 生产模式)
        """
        if self._endpoint.startswith("http://") or self._endpoint.startswith("https://"):
            return f"{self._endpoint}/{self._bucket}"
        return f"https://{self._endpoint}/{self._bucket}"

    def _sign(self, method: str, key: str, content_type: str = "",
              payload_hash: str = "UNSIGNED-PAYLOAD", querystring: str = "") -> dict:
        service = "s3"
        region = "auto"
        now = datetime.datetime.now(datetime.timezone.utc)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        date_stamp = now.strftime("%Y%m%d")

        canonical_uri = self._canonical_uri(key)
        canonical_querystring = querystring
        # R76 O7: 提取 host(去掉协议前缀),支持 MinIO HTTP endpoint
        # ``http://minio:9000`` → host = ``minio:9000``
        # ``<account_id>.r2.cloudflarestorage.com`` → host = 原样
        host_header = self._endpoint
        if host_header.startswith("http://"):
            host_header = host_header[len("http://"):]
        elif host_header.startswith("https://"):
            host_header = host_header[len("https://"):]
        canonical_headers = (
            f"host:{host_header}\n"
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

    @staticmethod
    def _encode_object_key(key: str) -> str:
        """按 SigV4/S3 URI 规则编码 object key，同时保留路径分隔符。"""
        return quote(key, safe="/-_.~")

    def _canonical_uri(self, key: str) -> str:
        """返回与实际 HTTP URL 完全一致的 SigV4 canonical URI。"""
        bucket_path = "/" + self._bucket
        if not key:
            return bucket_path
        return bucket_path + "/" + self._encode_object_key(key)

    async def upload(self, key: str, data: bytes, content_type: str = "application/octet-stream"):
        if self._http is None:
            raise RuntimeError("R2Storage not connected, call connect() first")
        # S3 object key 的 '/' 是路径分隔符，必须保留；其余特殊字符逐段编码。
        # canonical URI 与实际请求路径必须完全一致，否则 MinIO 会把 `%2F` 当成
        # 字面路径并返回 404，而 SigV4 又按未编码的 '/' 签名。
        safe_key = self._encode_object_key(key)
        url = f"{self.base_url}/{safe_key}"
        payload_hash = hashlib.sha256(data).hexdigest()
        headers = self._sign("PUT", key, content_type, payload_hash=payload_hash)
        headers["Content-Type"] = content_type
        resp = await self._http.put(url, headers=headers, content=data)
        resp.raise_for_status()
        return key

    async def download(self, key: str) -> bytes:
        if self._http is None:
            raise RuntimeError("R2Storage not connected, call connect() first")
        # 保留 S3 object key 的路径分隔符，与 SigV4 canonical URI 一致。
        safe_key = self._encode_object_key(key)
        url = f"{self.base_url}/{safe_key}"
        headers = self._sign("GET", key)
        resp = await self._http.get(url, headers=headers)
        resp.raise_for_status()
        return resp.content

    async def list_objects(self, prefix: str = "", max_keys: int = 1000) -> list[dict]:
        """列出 R2 存储桶中指定前缀的对象。返回 [{"key": ..., "size": ..., "last_modified": ...}, ...]"""
        if self._http is None:
            raise RuntimeError("R2Storage not connected, call connect() first")
        import xml.etree.ElementTree as ET
        from urllib.parse import quote
        ns = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}
        result = []
        continuation_token = None
        per_request = min(max_keys, 1000)

        while True:
            # S3 SigV4 要求 canonical querystring 按参数名字典序排序
            # 构建参数字典后按键排序,确保 continuation-token('c') < list-type('l') < max-keys('m') < prefix('p')
            params_dict = {
                "list-type": "2",
                "max-keys": str(per_request),
            }
            if prefix:
                params_dict["prefix"] = prefix
            if continuation_token:
                params_dict["continuation-token"] = continuation_token
            ordered_params = sorted(params_dict.items(), key=lambda x: x[0])
            # 用 quote 编码特殊字符(如 / + 空格 + =),signature 与 URL 必须用同一编码
            query = "&".join(f"{k}={quote(v, safe='')}" for k, v in ordered_params)
            url = f"{self.base_url}?{query}"
            headers = self._sign("GET", "", querystring=query)
            resp = await self._http.get(url, headers=headers)
            resp.raise_for_status()
            root = ET.fromstring(resp.text)  # nosec B314 — R2 S3 API XML 响应(可信内部服务,非用户输入)

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
        # 保留 S3 object key 的路径分隔符，与 SigV4 canonical URI 一致。
        safe_key = self._encode_object_key(key)
        url = f"{self.base_url}/{safe_key}"
        headers = self._sign("DELETE", key)
        resp = await self._http.delete(url, headers=headers)
        resp.raise_for_status()


_r2: R2Storage = R2Storage()


def get_r2() -> R2Storage:
    return _r2


async def configure_r2_dynamic():
    """优先从 config 表读取 R2 凭证（r2_secret_key 解密），fallback 到 .env。

    R26-M1: 让 admin /set_r2 写入的加密凭证真正参与运行时消费，
    与 relay api_hash 的「写入加密→读取解密→使用明文」范式对齐。
    config 表任一字段缺失时，对应字段回退到 .env。
    """
    from config import settings as _settings
    cfg = {}
    try:
        from database.session import get_r2_config
        cfg = await get_r2_config()
    except Exception as e:
        # DB 未就绪/查询失败：静默回退到 .env（启动早期常见）
        import logging
        logging.getLogger(__name__).debug(f"[r2] 读取 config 表失败，回退 .env: {e}")

    account_id = cfg.get("account_id") or _settings.R2_ACCOUNT_ID
    access_key = cfg.get("access_key") or _settings.R2_ACCESS_KEY_ID
    secret_key = cfg.get("secret_key") or _settings.R2_SECRET_ACCESS_KEY
    bucket = cfg.get("bucket") or _settings.R2_BUCKET_NAME
    endpoint = cfg.get("endpoint") or (_settings.R2_ENDPOINT if _settings.R2_ENDPOINT else None)

    _r2.configure(
        account_id=account_id,
        access_key=access_key,
        secret_key=secret_key,
        bucket=bucket,
        endpoint=endpoint,
    )
    await _r2.connect()


async def init_r2():
    """启动时初始化 R2（优先 config 表，fallback .env）。"""
    await configure_r2_dynamic()


async def close_r2():
    await _r2.close()


# ════════════════════════════════════════════════════════════════
# R76 O7 / 10.G: 统一对象存储工厂(支持 R2 和 MinIO 切换)
# ════════════════════════════════════════════════════════════════
# ``OBJECT_STORAGE_BACKEND`` 控制后端选择:
#   - ``r2``     → Cloudflare R2(生产,从 R2_* 配置读取)
#   - ``minio``  → MinIO(CI/本地测试,从 S3_* 配置读取)
# 两者都使用相同的 S3 兼容协议(SigV4 + path-style),只是 endpoint 和凭证来源不同。


async def configure_storage_from_settings():
    """R76 O7: 根据 ``OBJECT_STORAGE_BACKEND`` 配置统一存储客户端。

    - ``r2``: 调用 ``configure_r2_dynamic()``(优先 config 表,fallback .env R2_*)
    - ``minio``: 从 ``S3_*`` 环境变量读取配置,注入到同一 ``_r2`` 单例

    生产边界:
        - ``OBJECT_STORAGE_BACKEND=minio`` 在生产环境会被 Settings validator 拒绝
          (R76 O1 约束 4: production 时禁止 minio)
        - ``r2`` 模式缺 endpoint/access key/secret 时失败,不允许 fallback 到 MinIO

    Args:
        无(从 ``config.settings`` 读取配置)

    Raises:
        ValueError: minio 模式缺 S3_ENDPOINT_URL / S3_ACCESS_KEY_ID / S3_SECRET_ACCESS_KEY /
                    S3_BUCKET_NAME 任一字段
    """
    from config import settings as _settings

    backend = (getattr(_settings, "OBJECT_STORAGE_BACKEND", "r2") or "r2").strip().lower()

    if backend == "minio":
        # MinIO 模式:从 S3_* 读取配置(CI 临时凭证)
        endpoint = (getattr(_settings, "S3_ENDPOINT_URL", "") or "").strip()
        access_key = (getattr(_settings, "S3_ACCESS_KEY_ID", "") or "").strip()
        secret_key = (getattr(_settings, "S3_SECRET_ACCESS_KEY", "") or "").strip()
        bucket = (getattr(_settings, "S3_BUCKET_NAME", "") or "").strip()

        if not endpoint:
            raise ValueError(
                "R76 O7: OBJECT_STORAGE_BACKEND=minio 时 S3_ENDPOINT_URL 必须配置"
                "(例如 http://minio:9000)"
            )
        if not access_key:
            raise ValueError(
                "R76 O7: OBJECT_STORAGE_BACKEND=minio 时 S3_ACCESS_KEY_ID 必须配置"
            )
        if not secret_key:
            raise ValueError(
                "R76 O7: OBJECT_STORAGE_BACKEND=minio 时 S3_SECRET_ACCESS_KEY 必须配置"
            )
        if not bucket:
            raise ValueError(
                "R76 O7: OBJECT_STORAGE_BACKEND=minio 时 S3_BUCKET_NAME 必须配置"
            )

        _r2.configure(
            account_id="",  # MinIO 不需要 account_id
            access_key=access_key,
            secret_key=secret_key,
            bucket=bucket,
            endpoint=endpoint,
        )
        await _r2.connect()
        return

    if backend == "r2":
        # R2 模式:使用现有 configure_r2_dynamic(优先 config 表,fallback .env)
        await configure_r2_dynamic()
        # R76 O7: r2 模式缺凭证时失败,不允许 fallback 到 MinIO
        if not _r2._access_key or not _r2._secret_key:
            raise ValueError(
                "R76 O7: OBJECT_STORAGE_BACKEND=r2 时 R2 凭证未配置"
                "(R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY 缺失)"
            )
        return

    raise ValueError(
        f"R76 O7: 不支持的 OBJECT_STORAGE_BACKEND={backend!r}"
        "(仅支持 'r2' 或 'minio')"
    )


async def init_storage():
    """R76 O7: 启动时初始化对象存储(根据 OBJECT_STORAGE_BACKEND 选择后端)。"""
    await configure_storage_from_settings()


async def close_storage():
    """R76 O7: 关闭对象存储连接。"""
    await _r2.close()