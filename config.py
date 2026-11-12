import os

TOKEN = os.environ["TOKEN"]
GROUPS = [int(x) for x in os.environ.get("GROUPS", "").split(",") if x.strip()]
PROFANITY_FILE = "profanity_list.json"
