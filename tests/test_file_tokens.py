import importlib
import sys
import tempfile
import time
import types
import unittest
import uuid
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from core.file_tokens import register_reference_file


class FileTokenServiceStub:
    """Small stand-in for the public file-token service API."""

    def __init__(self, default_timeout=300):
        self.default_timeout = default_timeout
        self.staged_files = {}

    async def register_file(self, file_path, timeout=None, *, single_use=True):
        if not Path(file_path).exists():
            raise FileNotFoundError(file_path)
        token = str(uuid.uuid4())
        lifetime = self.default_timeout if timeout is None else timeout
        self.staged_files[token] = (file_path, time.time() + lifetime, single_use)
        return token

    async def handle_file(self, token):
        entry = self.staged_files.get(token)
        if entry is None or entry[1] < time.time():
            self.staged_files.pop(token, None)
            raise KeyError(token)
        path, _, single_use = entry
        if single_use:
            self.staged_files.pop(token)
        if not Path(path).exists():
            raise FileNotFoundError(path)
        return path


class LegacyFileTokenServiceStub:
    def __init__(self):
        self.calls = []
        self.staged_files = {}

    async def register_file(self, file_path, timeout=None):
        self.calls.append((file_path, timeout))
        token = str(uuid.uuid4())
        self.staged_files[token] = file_path
        return token

    async def handle_file(self, token):
        return self.staged_files.pop(token)


@contextmanager
def load_plugin_with_stubs(service, data_dir):
    """Import the actual entry point with only its external dependencies stubbed."""
    plugin_root = Path(__file__).resolve().parents[1]
    modules = {}

    def module(name, package=False, **attributes):
        result = types.ModuleType(name)
        if package:
            result.__path__ = []
        result.__dict__.update(attributes)
        modules[name] = result
        return result

    def decorator(*args, **kwargs):
        return lambda target: target

    module("aiohttp")
    module("astrbot", package=True)
    module("astrbot.api", package=True, logger=mock.Mock())
    filter_module = module("astrbot.api.event.filter", command=decorator, llm_tool=decorator)
    module("astrbot.api.event", package=True, AstrMessageEvent=object, filter=filter_module)

    class Image:
        def __init__(self, path):
            self.file = str(path)
            self.url = ""

        async def convert_to_file_path(self):
            return self.file

    module("astrbot.api.message_components", Image=Image, Video=object, Plain=object, BaseMessageComponent=object)
    module("astrbot.api.star", Context=object, Star=object, register=decorator)
    module("astrbot.core", package=True, astrbot_config={}, file_token_service=service)
    module("astrbot.core.message", package=True)
    module("astrbot.core.message.message_event_result", MessageEventResult=object)
    module("astrbot.core.star", package=True)
    star_tools = type("StarTools", (), {"get_data_dir": staticmethod(lambda name: data_dir)})
    module("astrbot.core.star.star_tools", StarTools=star_tools)

    package = module("agnes_plugin_test", package=True)
    package.__path__ = [str(plugin_root)]
    module("agnes_plugin_test.core.image_service", ImageService=object)
    module("agnes_plugin_test.core.video_service", VideoService=object)
    module("agnes_plugin_test.core.uploader", Uploader=object, DirectUrlImage=object)
    module(
        "agnes_plugin_test.agnes_api",
        AgnesAPIError=Exception,
        AgnesRequestConfig=object,
        AgnesVideoRequestConfig=object,
        PRESET_ASPECT_RATIOS={},
        PRESET_RESOLUTIONS={},
        PRESET_QUALITIES={},
        _close_agnes_sessions=None,
        generate_image=None,
        generate_video_task=None,
    )

    with mock.patch.dict(sys.modules, modules):
        try:
            plugin = importlib.import_module("agnes_plugin_test.main")
            yield plugin, Image
        finally:
            for name in list(sys.modules):
                if name.startswith("agnes_plugin_test."):
                    sys.modules.pop(name, None)


class RegisterReferenceFileTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.path = Path(self.temp_dir.name) / "reference.png"
        self.path.write_bytes(b"reference image")

    async def test_current_service_keeps_reference_and_consumes_other_token(self):
        service = FileTokenServiceStub()
        original_handler = service.handle_file.__func__
        reference = await register_reference_file(service, str(self.path))
        other = await service.register_file(str(self.path))

        self.assertEqual(len(service.staged_files[reference]), 3)
        self.assertIs(service.staged_files[reference][2], False)
        self.assertEqual(await service.handle_file(reference), str(self.path))
        self.assertEqual(await service.handle_file(reference), str(self.path))
        self.assertEqual(await service.handle_file(other), str(self.path))
        with self.assertRaises(KeyError):
            await service.handle_file(other)
        self.assertIs(service.handle_file.__func__, original_handler)

    async def test_expired_invalid_and_missing_file_paths_keep_service_errors(self):
        service = FileTokenServiceStub(default_timeout=-1)
        expired = await register_reference_file(service, str(self.path))
        with self.assertRaises(KeyError):
            await service.handle_file(expired)
        with self.assertRaises(KeyError):
            await service.handle_file("invalid-token")

        service.default_timeout = 300
        missing = await register_reference_file(service, str(self.path))
        self.path.unlink()
        with self.assertRaises(FileNotFoundError):
            await service.handle_file(missing)

    async def test_legacy_signature_uses_original_behavior(self):
        service = LegacyFileTokenServiceStub()
        token = await register_reference_file(service, str(self.path))
        self.assertEqual(service.calls, [(str(self.path), None)])
        self.assertEqual(await service.handle_file(token), str(self.path))
        with self.assertRaises(KeyError):
            await service.handle_file(token)

    async def test_internal_type_error_is_not_retried(self):
        class BrokenService:
            calls = 0

            async def register_file(self, file_path, *, single_use=True):
                self.calls += 1
                raise TypeError("failure inside service")

        service = BrokenService()
        with self.assertRaisesRegex(TypeError, "failure inside service"):
            await register_reference_file(service, str(self.path))
        self.assertEqual(service.calls, 1)

    async def test_keyword_wrapper_receives_single_use_false(self):
        class WrappedService:
            async def register_file(self, file_path, **kwargs):
                self.kwargs = kwargs
                return "token"

        service = WrappedService()
        self.assertEqual(await register_reference_file(service, str(self.path)), "token")
        self.assertEqual(service.kwargs, {"single_use": False})

    async def test_uninspectable_callable_uses_original_interface(self):
        class UninspectableRegister:
            @property
            def __signature__(self):
                raise ValueError("signature unavailable")

            async def __call__(self, file_path):
                self.path = file_path
                return "token"

        register = UninspectableRegister()
        service = types.SimpleNamespace(register_file=register)
        self.assertEqual(await register_reference_file(service, str(self.path)), "token")
        self.assertEqual(register.path, str(self.path))

    async def test_plugin_initialization_and_reference_image_flow(self):
        service = FileTokenServiceStub()
        original_handler = service.handle_file.__func__
        with load_plugin_with_stubs(service, Path(self.temp_dir.name)) as (module, image_type):
            plugin = module.AgnesImagePlugin.__new__(module.AgnesImagePlugin)
            plugin.plugin_config = types.SimpleNamespace(
                video_img_handling_method="astrbot",
                video_file_service_base_url="https://example.test",
            )
            image = image_type(self.path)

            async def dimensions(path):
                return (640, 480)

            plugin.image_service = types.SimpleNamespace(
                _get_all_message_components=lambda event: [image],
                _is_public_url=lambda value: False,
                _read_image_dimensions=dimensions,
            )
            await plugin.initialize()
            refs, notices, status, dims = await plugin._extract_video_reference_images(object())

            self.assertEqual(status, "astrbot")
            self.assertEqual(dims, [(640, 480)])
            self.assertEqual(len(notices), 1)
            token = refs[0].rsplit("/", 1)[-1]
            self.assertEqual(refs[0], f"https://example.test/api/file/{token}")
            stored = Path(await service.handle_file(token))
            self.assertEqual(stored.read_bytes(), self.path.read_bytes())
            self.assertEqual(await service.handle_file(token), str(stored))
            other = await service.register_file(str(self.path))
            await service.handle_file(other)
            with self.assertRaises(KeyError):
                await service.handle_file(other)
            self.assertIs(service.handle_file.__func__, original_handler)
            self.assertFalse(hasattr(service, "_agnes_magic_patched"))


if __name__ == "__main__":
    unittest.main()
