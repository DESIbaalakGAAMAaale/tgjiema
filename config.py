import os

# Baaki purane variables...
API_ID = int(os.getenv("API_ID", "19950194"))
API_HASH = os.getenv("API_HASH", "ab3271d1adb776ff252959fb850865f8")
BOT_TOKEN = os.getenv("BOT_TOKEN", "8141968367:AAE9m-29gJcE30uqgzHuFEUdbNoWo4lw-Ms")
DEVELOPER_USER_ID = int(os.getenv("DEVELOPER_USER_ID", "6243077977"))
MONGO_URL = os.getenv("MONGO_URL", "mongodb+srv://czcrazybhai_db_user:R00dQqZrWmxFDK85@cluster0.hhssxub.mongodb.net/?appName=Cluster0")
COMMAND_PREFIX = os.getenv("COMMAND_PREFIX", "rolex").split(",")
FORCE_SUB_CHANNEL = os.getenv("FORCE_SUB_CHANNEL", "-1003073644829")

# Naya variable jo missing hai:
LOG_GROUP_ID = int(os.getenv("LOG_GROUP_ID", "-1001667908597"))
