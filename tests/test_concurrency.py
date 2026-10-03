"""Offline regressions: no Telegram credentials, network, or real sessions required."""
import asyncio
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

# The application parses CLI arguments on import.
with patch.object(sys, 'argv', ['trmd-tests']):
    import pyrogram
    from pyrogram.errors import FloodWait
    from module.bot import Bot
    from module.client import TelegramRestrictedMediaDownloaderClient as Client
    from module.client import TelegramRestrictedMediaDownloaderSession as Session
    from module.downloader import TelegramRestrictedMediaDownloader as Downloader
    from module.enums import DownloadStatus, DownloadType, LinkType
    from module.task import DownloadTask
    from module.uploader import TelegramUploader


class Progress:
    def __init__(self):
        self.tasks = set()
        self.counter = 0

    def add_task(self, **kwargs):
        self.counter += 1
        self.tasks.add(self.counter)
        return self.counter

    def remove_task(self, task_id):
        self.tasks.discard(task_id)


class ConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.d = Downloader.__new__(Downloader)
        d = self.d
        d.loop = asyncio.get_running_loop()
        d.background_tasks = set()
        d.background_changed = asyncio.Event()
        d.event = asyncio.Event()
        d.active_download_task_counter = 0
        d.active_download_tasks = {}
        d.active_download_task_ids = {}
        d.pending_download_tasks = {}
        d.cancelled_download_task_ids = set()
        d.download_task_semaphore = asyncio.Semaphore(2)
        d.download_path_locks = {}
        d.memory_download_lock = asyncio.Lock()
        d.filename_prompt_lock = asyncio.Lock()
        d.memory_download_used = 0
        d.video_filename_choices = {}
        d.video_filename_choice_counter = 0
        d.bot_task_link = set()
        d.uploader = None
        d.root = [1]
        d.app = SimpleNamespace(
            current_task_num=0, download_type=[DownloadType.DOCUMENT],
            memory_download_limit_bytes=0, memory_download_limit=0,
            max_download_task=2, max_download_retries=1,
            video_filename_prompt_timeout=0, video_filename_default_mode='new',
            download_file_mode=0o644, bot_admin_users=[], bot_allowed_users=[2, 3],
            save_directory=str(self.root / 'saved'), client=None)
        d.pb = SimpleNamespace(progress=Progress(), download=MagicMock())
        d.done_notice = AsyncMock()
        d.env_save_directory = lambda message: d.app.save_directory
        d.app.get_file_type = self.file_type
        d.get_media_meta = self.media_meta
        DownloadTask.LINK_INFO.clear()
        DownloadTask.COMPLETE_LINK.clear()
        self.console_patches = [patch(name, MagicMock()) for name in (
            'module.downloader.console', 'module.task.console')]
        for console_patch in self.console_patches:
            console_patch.start()
        self.errors = []
        original_done = self.d._background_task_done
        def completed(task):
            if not task.cancelled() and task.exception():
                self.errors.append(task.exception())
            original_done(task)
        self.d._background_task_done = completed
        self.stats_patch = patch('module.downloader.MetaData.print_current_task_num')
        self.stats_patch.start()

    def file_type(self, message, name, status):
        if status == DownloadStatus.DOWNLOADING:
            self.d.app.current_task_num += 1
        return DownloadType.DOCUMENT

    def media_meta(self, message, dtype, **kwargs):
        name = f'{message.id}.bin'
        return dict(file_id=message.id, temp_file_path=str(self.root / name),
                    sever_file_size=8, file_name=name,
                    save_directory=str(self.root / 'saved' / name), format_file_size='8 B')

    def message(self, number, user=2):
        return pyrogram.types.Message(
            id=number, chat=pyrogram.types.Chat(id=user, type=pyrogram.enums.ChatType.PRIVATE),
            from_user=pyrogram.types.User(id=user), document=SimpleNamespace(file_size=8))

    def callback(self, user=2):
        return SimpleNamespace(from_user=SimpleNamespace(id=user),
                               message=SimpleNamespace(edit_text=AsyncMock()))

    async def until(self, condition):
        async with asyncio.timeout(3):
            while not condition():
                await asyncio.sleep(0.005)

    async def drain(self):
        await self.until(lambda: not self.d.background_tasks)

    async def asyncTearDown(self):
        while self.d.background_tasks:
            tasks = tuple(self.d.background_tasks)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.sleep(0)
        for console_patch in self.console_patches:
            console_patch.stop()
        self.stats_patch.stop()
        self.temp.cleanup()
        self.assertEqual(self.errors, [], 'Unexpected exception in a background task')

    async def test_full_download_slots_do_not_block_progress_or_cancellation(self):
        gate = asyncio.Event()
        started = []
        async def download(message, **kwargs):
            started.append(message.id)
            await gate.wait()
            return None
        self.d.resume_download = download
        for number in range(1, 9):
            message = self.message(number)
            await self.d.create_download_task(message, request_message=message)
        await self.until(lambda: len(started) == 2 and len(self.d.pending_download_tasks) == 6)
        client = SimpleNamespace(send_message=AsyncMock())
        await asyncio.wait_for(self.d.download_tasks(client, self.message(99)), 0.5)
        self.assertIn('运行 2', client.send_message.call_args.kwargs['text'])
        pending_id = next(iter(self.d.pending_download_tasks))
        pending = self.d.pending_download_tasks[pending_id]['task']
        await asyncio.wait_for(self.d._TelegramRestrictedMediaDownloader__cancel_active_download_task(
            self.callback(), pending_id), 0.5)
        await self.until(pending.done)
        self.assertTrue(pending.cancelled())
        active_id = next(iter(self.d.active_download_tasks))
        await self.d._TelegramRestrictedMediaDownloader__cancel_active_download_task(self.callback(), active_id)
        await self.until(lambda: len(started) == 3)
        self.assertEqual(self.d.app.current_task_num, 2)
        self.assertEqual(len(self.d.pending_download_tasks), 4)

    async def test_dispatcher_handler_returns_while_work_waits(self):
        gate = asyncio.Event()
        async def work(client, update):
            await gate.wait()
        await asyncio.wait_for(self.d.background_handler(work)(None, None), 0.1)
        self.assertEqual(len(self.d.background_tasks), 1)
        gate.set()
        await self.drain()

    async def test_album_files_have_independent_pending_tasks_and_slots(self):
        members = [self.message(i) for i in (11, 12, 13)]
        DownloadTask('album', LinkType.SINGLE, 3, 0, set(), {})
        gate = asyncio.Event()
        async def download(**kwargs):
            await gate.wait()
        self.d.resume_download = download
        await self.d._TelegramRestrictedMediaDownloader__add_task(
            2, LinkType.SINGLE, 'album', members, {'id': -1, 'count': 0}, request_message=members[0])
        await self.until(lambda: len(self.d.active_download_tasks) == 2)
        self.assertEqual(len(self.d.pending_download_tasks), 1)
        self.assertEqual(len(self.d.background_tasks), 3)

    async def test_preparation_error_does_not_leak_slots_or_memory(self):
        self.d.app.memory_download_limit_bytes = 100
        self.d.pb.progress.add_task = MagicMock(side_effect=RuntimeError('progress failed'))
        with self.assertLogs('rich', level='ERROR'):
            await self.d.create_download_task(self.message(5))
            await self.drain()
        self.assertEqual(self.d.download_task_semaphore._value, 2)
        self.assertEqual(self.d.memory_download_used, 0)
        self.assertEqual(self.d.app.current_task_num, 0)
        self.assertFalse(self.d.pending_download_tasks)
        self.assertEqual(len(self.errors), 1)
        self.errors.clear()

    async def test_naming_wait_is_immediately_cancellable(self):
        entered = asyncio.Event()
        async def choose(**kwargs):
            entered.set()
            await asyncio.Event().wait()
        self.d._TelegramRestrictedMediaDownloader__choose_video_filename_mode = choose
        await self.d.create_download_task(self.message(4), request_message=self.message(4))
        await entered.wait()
        task_id = next(iter(self.d.pending_download_tasks))
        await self.d._TelegramRestrictedMediaDownloader__cancel_active_download_task(self.callback(), task_id)
        await self.drain()
        self.assertEqual(self.d.download_task_semaphore._value, 2)
        self.assertFalse(self.d.cancelled_download_task_ids)

    async def test_user_cannot_cancel_another_users_task(self):
        gate = asyncio.Event()
        # Accept keyword arguments while retaining the indefinitely pending operation.
        async def choose(**kwargs):
            await gate.wait()
        self.d._TelegramRestrictedMediaDownloader__choose_video_filename_mode = choose
        await self.d.create_download_task(self.message(4), request_message=self.message(4))
        task_id = next(iter(self.d.pending_download_tasks))
        task = self.d.pending_download_tasks[task_id]['task']
        await self.d._TelegramRestrictedMediaDownloader__cancel_active_download_task(self.callback(user=3), task_id)
        self.assertFalse(task.cancelled())
        self.assertIn(task_id, self.d.pending_download_tasks)

    async def test_task_pages_are_bounded_and_keep_all_cancel_buttons_reachable(self):
        for number in range(1, 101):
            self.d.pending_download_tasks[str(number)] = dict(
                link='https://t.me/' + 'x' * 200, file_name='😀' * 120, request_user_id=2)
        seen = set()
        for page in range(17):
            text, keyboard = self.d._TelegramRestrictedMediaDownloader__download_task_page(2, page)
            self.assertLess(len(text.encode('utf-16-le')) // 2, 4096)
            for row in keyboard.inline_keyboard:
                for button in row:
                    if button.callback_data.startswith('download_task_cancel:'):
                        seen.add(button.callback_data.split(':')[1])
        self.assertEqual(len(seen), 100)

    async def test_resume_discards_partial_chunk_tail(self):
        target = self.root / 'resume.bin'
        Path(str(target) + '.temp').write_bytes(b'abcdXX')
        offsets = []
        async def stream_media(message, offset):
            offsets.append(offset)
            yield b'efgh'
        client = SimpleNamespace(stream_media=stream_media)
        await self.d.resume_download(self.message(1), str(target), chunk_size=4, compare_size=8, download_client=client)
        self.assertEqual(offsets, [1])
        self.assertEqual(target.read_bytes(), b'abcdefgh')

    async def test_flood_wait_resumes_at_current_offset(self):
        offsets = []
        async def stream_media(message, offset):
            offsets.append(offset)
            if offset == 0:
                yield b'abcd'
                raise FloodWait(0)
            yield b'efgh'
        client = SimpleNamespace(stream_media=stream_media)
        target = self.root / 'flood.bin'
        await self.d.resume_download(self.message(1), str(target), chunk_size=4, compare_size=8, download_client=client)
        self.assertEqual(offsets, [0, 1])
        self.assertEqual(target.read_bytes(), b'abcdefgh')

    async def test_slow_disk_write_keeps_loop_responsive_and_cancellation_closes_stream(self):
        started = threading.Event()
        release = threading.Event()
        closed = asyncio.Event()
        async def stream_media(message, offset):
            try:
                yield b'abcdefgh'
            finally:
                closed.set()
        real_open = open
        class SlowFile:
            def __init__(self, *args):
                self.f = real_open(*args)
            def __getattr__(self, name):
                return getattr(self.f, name)
            def write(self, value):
                started.set()
                release.wait(2)
                return self.f.write(value)
        with patch('module.downloader.open', SlowFile, create=True):
            task = asyncio.create_task(self.d.resume_download(
                self.message(1), str(self.root / 'slow.bin'), compare_size=8,
                download_client=SimpleNamespace(stream_media=stream_media)))
            await self.until(started.is_set)
            try:
                await asyncio.wait_for(asyncio.sleep(0), 0.1)
                task.cancel()
                await asyncio.sleep(0)
                self.assertFalse(task.done())
            finally:
                release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue(closed.is_set())
        self.assertEqual(Path(str(self.root / 'slow.bin') + '.temp').read_bytes(), b'abcdefgh')

    async def test_retry_preserves_album_completed_files(self):
        message = self.message(3)
        link = '2/3'
        DownloadTask(link, LinkType.SINGLE, 3, 1, {'one.bin'}, {'two.bin': 'failure'})
        self.d._TelegramRestrictedMediaDownloader__add_task = AsyncMock()
        await self.d.create_download_task(message, retry={'id': 3, 'count': 1})
        self.assertEqual(DownloadTask.get(link, 'file_name'), {'one.bin'})
        self.assertEqual(DownloadTask.get(link, 'complete_num'), 1)

    async def test_move_failure_is_not_reported_as_success(self):
        target = self.root / 'move.bin'
        target.write_bytes(b'abcdefgh')
        with patch('module.downloader.move_to_save_directory', return_value={'e_code': 'disk full'}):
            self.assertFalse(self.d._TelegramRestrictedMediaDownloader__check_download_finish(
                self.message(1), 8, str(target), self.d.app.save_directory))

    async def test_duplicate_path_downloads_are_serialized(self):
        self.d.get_media_meta = lambda message, dtype, **kw: dict(
            file_id=message.id, temp_file_path=str(self.root / 'same.bin'), sever_file_size=8,
            file_name='same.bin', save_directory=str(self.root / 'saved' / 'same.bin'), format_file_size='8 B')
        gate = asyncio.Event()
        starts = []
        async def download(message, **kwargs):
            starts.append(message.id)
            await gate.wait()
            (self.root / 'same.bin').write_bytes(b'abcdefgh')
            return str(self.root / 'same.bin')
        self.d.resume_download = download
        await self.d.create_download_task(self.message(1))
        await self.d.create_download_task(self.message(2))
        await self.until(lambda: len(starts) == 1)
        gate.set()
        await self.drain()
        self.assertEqual(len(starts), 1)
        self.assertEqual(self.d.download_task_semaphore._value, 2)

    async def test_retry_is_drained_and_only_success_counts_as_complete(self):
        attempts = []
        async def download(message, **kwargs):
            attempts.append(message.id)
            if len(attempts) == 1:
                return None
            target = self.root / f'{message.id}.bin'
            target.write_bytes(b'abcdefgh')
            return str(target)
        self.d.resume_download = download
        await self.d.create_download_task(self.message(7))
        await self.drain()
        self.assertEqual(attempts, [7, 7])
        self.assertEqual(DownloadTask.get('2/7', 'complete_num'), 1)
        self.assertIn('2/7', DownloadTask.COMPLETE_LINK)
        self.assertEqual(self.d.download_task_semaphore._value, 2)
        self.assertEqual(self.d.app.current_task_num, 0)
        self.assertFalse(self.d.active_download_tasks)

    async def test_cancellation_before_preparation_starts_cleans_registration(self):
        await self.d.create_download_task(self.message(1), request_message=self.message(1))
        task_id = next(iter(self.d.pending_download_tasks))
        await self.d._TelegramRestrictedMediaDownloader__cancel_active_download_task(self.callback(), task_id)
        await self.drain()
        self.assertFalse(self.d.pending_download_tasks)
        self.assertFalse(self.d.cancelled_download_task_ids)
        self.assertEqual(self.d.download_task_semaphore._value, 2)

    async def test_finalizer_cancelled_before_start_releases_resources(self):
        self.d.download_task_semaphore = asyncio.Semaphore(0)
        self.d.app.current_task_num = 1
        lock = asyncio.Lock()
        await lock.acquire()
        transfer = asyncio.create_task(asyncio.sleep(0))
        await transfer
        self.d.active_download_task_ids[transfer] = '1'
        self.d.active_download_tasks['1'] = {'task': transfer, 'path_lock': lock}
        self.d._TelegramRestrictedMediaDownloader__schedule_download_completion(
            8, '', '2/1', self.message(1), '1.bin', 0, 1, '8 B', 1,
            None, None, None, None, None, transfer)
        finalizer = self.d.active_download_tasks['1']['task']
        finalizer.cancel()
        await self.drain()
        self.assertEqual(self.d.download_task_semaphore._value, 1)
        self.assertFalse(lock.locked())
        self.assertFalse(self.d.active_download_tasks)
        self.assertFalse(self.d.active_download_task_ids)

    async def test_memory_download_releases_reservation_on_completion(self):
        self.d.app.memory_download_limit_bytes = 100
        async def stream_media(message, offset):
            yield b'abcdefgh'
        self.d.app.client = SimpleNamespace(stream_media=stream_media)
        await self.d.create_download_task(self.message(9))
        await self.drain()
        self.assertEqual((self.root / 'saved' / '9.bin').read_bytes(), b'abcdefgh')
        self.assertEqual(self.d.memory_download_used, 0)
        self.assertEqual(self.d.download_task_semaphore._value, 2)

    async def test_terminal_filename_prompt_keeps_loop_responsive(self):
        entered = threading.Event()
        release = threading.Event()
        original = self.d.get_media_meta
        def metadata(*args, **kwargs):
            entered.set()
            release.wait(2)
            return original(*args, **kwargs)
        self.d.get_media_meta = metadata
        await self.d.create_download_task(self.message(1), request_message=self.message(1))
        await self.until(entered.is_set)
        task_id = next(iter(self.d.pending_download_tasks))
        try:
            await asyncio.wait_for(asyncio.sleep(0), 0.1)
            await self.d._TelegramRestrictedMediaDownloader__cancel_active_download_task(self.callback(), task_id)
        finally:
            release.set()
        await self.drain()
        self.assertEqual(self.d.download_task_semaphore._value, 2)

    async def test_permanent_notification_error_does_not_spin(self):
        self.d.gc = SimpleNamespace(get_config=lambda key: True)
        self.d.bot = SimpleNamespace(send_message=AsyncMock(side_effect=RuntimeError('chat unavailable')))
        self.d.last_client = self.d.last_message = None
        with self.assertLogs('rich', level='ERROR'):
            await asyncio.wait_for(Bot.done_notice(self.d, 'done', chat_id=2), 0.2)
        self.d.bot.send_message.assert_awaited_once()

    async def test_cli_waits_for_retry_producers_before_stopping_client(self):
        self.d.app.bot_token = None
        self.d.app.links = 'https://t.me/example/7'
        self.d.app.client = SimpleNamespace(start=AsyncMock(), stop=AsyncMock(), is_connected=True)
        self.d.pb.progress.start = MagicMock()
        self.d.is_bot_running = False
        self.d.running_log = set()
        self.d.bot = None
        attempts = []
        async def download(message, **kwargs):
            attempts.append(message.id)
            if len(attempts) > 1:
                (self.root / '7.bin').write_bytes(b'abcdefgh')
            return str(self.root / '7.bin')
        self.d.resume_download = download
        metadata = dict(link_type=LinkType.SINGLE, chat_id=2, message=self.message(7), member_num=1)
        with patch('module.downloader.get_my_id', AsyncMock(return_value=1)), \
                patch('module.downloader.get_message_by_link', AsyncMock(return_value=metadata)):
            await asyncio.wait_for(self.d._TelegramRestrictedMediaDownloader__download_media_from_links(), 3)
        self.assertEqual(attempts, [7, 7])
        self.assertIn(self.d.app.links, DownloadTask.COMPLETE_LINK)
        self.assertFalse(self.d.background_tasks)
        self.d.app.client.stop.assert_awaited_once()

    async def test_upload_hash_runs_off_event_loop(self):
        target = self.root / 'empty.bin'
        target.write_bytes(b'')
        entered, release = threading.Event(), threading.Event()
        def checksum(path):
            entered.set()
            release.wait(2)
            return 'digest'
        upload = TelegramUploader.__new__(TelegramUploader)
        upload.is_premium = False
        upload_task = SimpleNamespace(file_path=str(target), sha256='', chat_id=None)
        with patch('module.uploader.calc_sha256', checksum):
            task = asyncio.create_task(upload.create_upload_task(2, upload_task))
            await self.until(entered.is_set)
            try:
                await asyncio.wait_for(asyncio.sleep(0), 0.1)
            finally:
                release.set()
            await task
        self.assertEqual(upload_task.sha256, 'digest')


class SessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_rpc_discards_pending_result(self):
        session = Session.__new__(Session)
        entered = asyncio.Event()
        async def send(payload):
            entered.set()
            await asyncio.Event().wait()
        session.results = {}
        session.msg_factory = SimpleNamespace(create=AsyncMock(return_value=SimpleNamespace(msg_id=42)))
        session.client = SimpleNamespace(loop=asyncio.get_running_loop())
        session.connection = SimpleNamespace(send=send, protocol=SimpleNamespace(crypto_executor=None))
        session.salt, session.session_id, session.auth_key, session.auth_key_id = 0, 0, b'', 0
        with patch('module.client.mtproto.pack', return_value=b'payload'):
            task = asyncio.create_task(session.send(SimpleNamespace()))
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(session.results, {})

    async def test_concurrent_media_sessions_wait_for_initialization(self):
        client = Client.__new__(Client)
        client.storage = SimpleNamespace(dc_id=AsyncMock(return_value=1), test_mode=AsyncMock(return_value=False))
        client.session = SimpleNamespace(auth_key=b'key')
        client.media_sessions = {}
        client.sessions = {}
        client.ipv6 = False
        client.get_dc_option = AsyncMock(return_value=SimpleNamespace(ip_address='127.0.0.1', port=443))
        entered = asyncio.Event()
        ready = asyncio.Event()
        async def start():
            entered.set()
            await ready.wait()
        session = SimpleNamespace(start=start, stop=AsyncMock())
        with patch('module.client.TelegramRestrictedMediaDownloaderSession', return_value=session) as factory:
            first = asyncio.create_task(client.get_session(1, is_media=True))
            await entered.wait()
            second = asyncio.create_task(client.get_session(1, is_media=True))
            await asyncio.sleep(0)
            self.assertFalse(second.done())
            self.assertEqual(client.media_sessions, {})
            ready.set()
            self.assertIs(await first, session)
            self.assertIs(await second, session)
            factory.assert_called_once()

    async def test_cancelled_session_initialization_is_not_cached(self):
        client = Client.__new__(Client)
        client.storage = SimpleNamespace(dc_id=AsyncMock(return_value=1), test_mode=AsyncMock(return_value=False))
        client.session = SimpleNamespace(auth_key=b'key')
        client.media_sessions, client.sessions = {}, {}
        client.ipv6 = False
        client.get_dc_option = AsyncMock(return_value=SimpleNamespace(ip_address='127.0.0.1', port=443))
        entered = asyncio.Event()
        async def start():
            entered.set()
            await asyncio.Event().wait()
        session = SimpleNamespace(start=start, stop=AsyncMock())
        with patch('module.client.TelegramRestrictedMediaDownloaderSession', return_value=session):
            task = asyncio.create_task(client.get_session(1, is_media=True))
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertFalse(client.media_sessions)
        session.stop.assert_awaited_once()


if __name__ == '__main__':
    unittest.main()
