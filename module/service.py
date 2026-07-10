# coding=UTF-8
# Author:Gentlesprite
# Software:PyCharm
# Time:2026/7/5 00:00
# File:service.py
import os
import re
import sys
import shlex
import shutil
import subprocess

from module import console, SOFTWARE_SHORT_NAME
from module.enums import MODE
from module.parser import PARSE_ARGS


class LinuxService:
    SERVICE_DIRECTORY = '/etc/systemd/system'
    DEFAULT_SERVICE_NAME = SOFTWARE_SHORT_NAME.lower()
    DEFAULT_WEB_PORT = 2921

    def __init__(self, main_file: str):
        self.main_file = os.path.abspath(main_file)
        self.service_name = self.normalize_service_name(PARSE_ARGS.service_name)
        self.service_port = self.normalize_service_port(PARSE_ARGS.service_port)
        self.service_file = os.path.join(self.SERVICE_DIRECTORY, f'{self.service_name}.service')

    @staticmethod
    def normalize_service_name(service_name: str) -> str:
        service_name = (service_name or LinuxService.DEFAULT_SERVICE_NAME).strip()
        if service_name.endswith('.service'):
            service_name = service_name[:-len('.service')]
        if not re.fullmatch(r'[A-Za-z0-9_.@-]+', service_name):
            raise ValueError('服务名称只能包含字母、数字、"_"、"-"、"."、"@"。')
        return service_name

    @staticmethod
    def normalize_service_port(service_port: int) -> int:
        try:
            service_port = int(service_port)
        except (TypeError, ValueError):
            service_port = LinuxService.DEFAULT_WEB_PORT
        if not 0 < service_port <= 65535:
            raise ValueError('服务Web端口必须在1~65535之间。')
        return service_port

    @staticmethod
    def check_linux_systemd() -> None:
        if sys.platform != 'linux':
            raise RuntimeError('仅支持在Linux系统中安装systemd服务。')
        if not shutil.which('systemctl'):
            raise RuntimeError('未找到systemctl,当前系统可能不支持systemd。')
        if hasattr(os, 'geteuid') and os.geteuid() != 0:
            raise PermissionError('安装或卸载systemd服务需要root权限,请使用sudo或root用户运行。')

    @staticmethod
    def shell_join(args: list) -> str:
        return ' '.join(shlex.quote(str(arg)) for arg in args)

    def get_program_args(self) -> list:
        executable = os.path.abspath(sys.argv[0])
        if executable.endswith('.py'):
            args = [sys.executable, self.main_file]
        else:
            args = [executable]
        args.append('--quiet')
        if PARSE_ARGS.config:
            args.extend(['--config', os.path.abspath(PARSE_ARGS.config)])
        if PARSE_ARGS.session:
            args.extend(['--session', os.path.abspath(PARSE_ARGS.session)])
        if PARSE_ARGS.temp:
            args.extend(['--temp', os.path.abspath(PARSE_ARGS.temp)])
        if PARSE_ARGS.memory is not None:
            args.extend(['--memory', str(PARSE_ARGS.memory)])
        if PARSE_ARGS.debug_log:
            args.extend(['--debug-log', os.path.abspath(PARSE_ARGS.debug_log)])
        if PARSE_ARGS.web is not None:
            web_port = PARSE_ARGS.web if PARSE_ARGS.web != 0 else self.service_port
            args.extend(['--web', str(web_port), '--mode', MODE.SESSION])
        return args

    def render_service(self) -> str:
        work_directory = os.path.dirname(os.path.abspath(sys.argv[0])) or os.getcwd()
        exec_start = self.shell_join(self.get_program_args())
        return f'''[Unit]
Description=Telegram Restricted Media Downloader
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory={work_directory}
ExecStart={exec_start}
Restart=unless-stopped
RestartSec=5
Environment=TZ=Asia/Shanghai
Environment=TERM=xterm-256color

[Install]
WantedBy=multi-user.target
'''

    def systemctl(self, *args) -> None:
        subprocess.run(['systemctl', *args], check=True)

    def install(self) -> None:
        self.check_linux_systemd()
        os.makedirs(self.SERVICE_DIRECTORY, exist_ok=True)
        with open(self.service_file, 'w', encoding='UTF-8') as f:
            f.write(self.render_service())
        self.systemctl('daemon-reload')
        self.systemctl('enable', self.service_name)
        self.systemctl('restart', self.service_name)
        console.print(f'Linux服务已安装并启动:{self.service_file}', style='#B1DB74')
        if PARSE_ARGS.web is None:
            console.print('服务模式:直接运行下载器主程序', style='#B1DB74')
        else:
            web_port = PARSE_ARGS.web if PARSE_ARGS.web != 0 else self.service_port
            console.print(f'服务模式:Web终端SESSION,端口:{web_port}', style='#B1DB74')
        console.print(f'查看状态: systemctl status {self.service_name}', style='#B1DB74')
        console.print(f'查看日志: journalctl -u {self.service_name} -f', style='#B1DB74')

    def uninstall(self) -> None:
        self.check_linux_systemd()
        subprocess.run(['systemctl', 'disable', '--now', self.service_name], check=False)
        if os.path.exists(self.service_file):
            os.remove(self.service_file)
        self.systemctl('daemon-reload')
        subprocess.run(['systemctl', 'reset-failed', self.service_name], check=False)
        console.print(f'Linux服务已卸载:{self.service_name}', style='#B1DB74')


def handle_service_command(main_file: str) -> bool:
    if not PARSE_ARGS.install_service and not PARSE_ARGS.uninstall_service:
        return False
    try:
        if PARSE_ARGS.install_service and PARSE_ARGS.uninstall_service:
            raise ValueError('不能同时安装和卸载Linux服务。')
        service = LinuxService(main_file=main_file)
        if PARSE_ARGS.install_service:
            service.install()
        if PARSE_ARGS.uninstall_service:
            service.uninstall()
        return True
    except Exception as e:
        console.print(f'Linux服务操作失败:{e}', style='#FF4689')
        raise SystemExit(1)
