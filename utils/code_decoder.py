_SUPPORTED_FILE_ID_PREFIXES = (
    "CAAC", "AgAD", "BAAC", "CQAC", "AwAC", "BQAC",
    "AAQ", "AAOF",
)


def is_likely_bot_api_file_id(text: str) -> bool:
    if not text or not isinstance(text, str):
        return False
    cleaned = text.strip().strip('"').strip("'")
    return cleaned.startswith(_SUPPORTED_FILE_ID_PREFIXES)
