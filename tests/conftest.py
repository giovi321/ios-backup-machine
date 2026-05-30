"""Make the flat-installed app modules importable from the tests.

The app ships flat to /root/iosbackupmachine/ and imports siblings by bare name
(e.g. ``import sync_manager``). Mirror that by putting app/ on sys.path.
"""
import os
import sys

APP_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "app"))
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)
