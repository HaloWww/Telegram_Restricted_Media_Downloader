# coding=UTF-8
# Author:Gentlesprite
# Software:PyCharm
# Time:2024/9/5 19:08
# File:main.py
import os

from module import enable_debug_file_log
from module.enums import ENVIRON, MODE
from module.util import check_environ
from module.web import Web
from module.parser import PARSE_ARGS
from module.service import handle_service_command
from module.downloader import TelegramRestrictedMediaDownloader

if __name__ == '__main__':
    if PARSE_ARGS.debug_log:
        enable_debug_file_log(PARSE_ARGS.debug_log)
    if handle_service_command(__file__):
        raise SystemExit(0)
    check_environ()
    if os.environ.get(ENVIRON.TRMD_WEB_PORT) and os.environ.get(ENVIRON.TRMD_WEB_PID) is None:
        web = Web(__file__)
        if PARSE_ARGS.mode == MODE.SESSION:
            web.run_session()
        else:
            web.run_once()
    else:
        trmd = TelegramRestrictedMediaDownloader()
        trmd.run()
