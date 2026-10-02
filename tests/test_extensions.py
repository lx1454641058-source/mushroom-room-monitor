import copy
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch
from firmware.controller import Controller
from firmware.climate import DEFAULTS, validate_climate_config
from firmware.scd4x import words, SCD4x
from room.runtime import Runtime, parse_status
from room.storage import Store
from room.agent import RoomAgent
from room.vision import VisionService, validate_result, infer
from room.maintenance import backup


class ClimateTests(unittest.TestCase):
    def ready(self, temp=16, co2=700, now=30000):
        c = Controller()
        c.heartbeat(now)
        c.sample(temp, 75, True, now)
        c.climate.sample(co2, now)
        c.command('start', None, now)
        c.command('climate', {'operation': 'enable'}, now)
        return c

    def tick(self, c, temp, co2, now):
        c.heartbeat(now)
        c.sample(temp, 75, True, now)
        c.climate.sample(co2, now)
        c.step(now)

    def test_heat_cool_stop_bands_and_direction_rest(self):
        c = self.ready()
        self.assertTrue(c.climate.heater)
        self.tick(c, 19, 700, 31000)
        self.assertFalse(c.climate.heater)
        self.tick(c, 28, 700, 32000)
        self.assertFalse(c.climate.cooler)
        self.tick(c, 28, 700, 61000)
        self.assertTrue(c.climate.cooler)
        self.assertFalse(c.climate.heater)
        self.tick(c, 23, 700, 62000)
        self.assertFalse(c.climate.cooler)

    def test_co2_overrides_manual_off_and_has_hysteresis(self):
        c = self.ready(23, 1800)
        c.command('fan', 'off', 30000)
        self.assertTrue(c.fan)
        self.tick(c, 23, 1000, 31000)
        self.assertTrue(c.fan)
        self.tick(c, 23, 700, 32000)
        self.assertFalse(c.fan)

    def test_fault_and_120_second_limit_latch_all_outputs_off(self):
        for temp, co2, elapsed in ((40, 700, 1000), (4, 700, 1000), (16, None, 1000), (16, 700, 120000)):
            c = self.ready()
            self.tick(c, temp, co2, 30000 + elapsed)
            self.assertFalse(c.enabled)
            self.assertFalse(c.climate.heater or c.climate.cooler or c.fan or c.mist)
            self.tick(c, 16, 700, 31000 + elapsed)
            self.assertFalse(c.climate.enabled)

    def test_co2_stale_heartbeat_and_stop(self):
        c = self.ready()
        c.heartbeat(45000); c.sample(16, 75, True, 45000); c.step(45000)
        self.assertFalse(c.enabled)
        c = self.ready(); c.step(40000)
        self.assertFalse(c.climate.heater)
        c = self.ready(); c.command('stop', None, 31000)
        self.assertFalse(c.climate.heater or c.climate.ventilation)

    def test_config_ranges_and_no_live_changes(self):
        for key, bad in [('temperature_low', True), ('temperature_high', 40), ('co2_high', 400), ('co2_low', float('nan'))]:
            with self.assertRaises(ValueError):
                validate_climate_config({**DEFAULTS, key: bad})
        c = self.ready()
        with self.assertRaises(ValueError):
            c.command('climate', {'operation': 'config', 'config': dict(DEFAULTS)}, 30000)

    def test_scd41_documented_crc_vectors_and_ready_mask(self):
        self.assertEqual(words(bytes.fromhex('beef92')), [0xBEEF])
        self.assertEqual(words(bytes.fromhex('01f4336667a25eb93c')), [500, 26215, 24249])
        # The v1.5 measurement illustration prints 0x7b for 0x01f4, which
        # disagrees with its CRC algorithm (0x33). Never weaken CRC validation.
        with self.assertRaises(ValueError):
            words(bytes.fromhex('01f47c'))
        class Bus:
            def __init__(self): self.sent = []
            def writeto(self, address, data): self.sent.append((address, data))
            def readfrom(self, address, size): return bytes.fromhex('8000a2')
        bus = Bus()
        with patch('firmware.scd4x.time.sleep_ms', create=True):
            sensor = SCD4x(bus)
            self.assertIsNone(sensor.read())
        self.assertIn((0x62, bytes.fromhex('2416000081')), bus.sent)


class ExtensionIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / 'demo', 'demo')
        self.runtime = Runtime('demo', self.store)
        self.runtime.demo_tick(0)
        self.agent = RoomAgent(self.runtime, self.store)

    def tearDown(self):
        self.store.close(); self.temp.cleanup()

    def ask(self, text, number):
        return self.agent.handle('climate-%04d' % number, text)

    def test_agent_config_enable_and_stop(self):
        self.assertTrue(self.ask('把温度下限设为17，上限设为25', 1)['ok'])
        self.assertEqual(self.runtime.control.climate.config['temperature_low'], 17)
        self.assertTrue(self.ask('把二氧化碳下限设为700，上限设为1400', 2)['ok'])
        self.assertFalse(self.ask('启用温控与二氧化碳联动', 3)['ok'])
        self.assertTrue(self.ask('启动系统', 4)['ok'])
        self.assertTrue(self.ask('启用温控与二氧化碳联动', 5)['ok'])
        self.runtime.scenario('co2_high')
        self.assertTrue(self.runtime.control.climate.ventilation)
        self.assertTrue(self.ask('全部停止', 6)['ok'])
        self.assertFalse(self.runtime.control.climate.enabled)

    def test_old_status_and_offline_are_not_new_capabilities(self):
        old = copy.deepcopy(self.runtime.status); old.pop('climate')
        self.runtime.status = parse_status(old)
        with self.assertRaises(ValueError):
            self.runtime.command('climate', {'operation': 'enable'})
        self.runtime.demo_tick(0); self.runtime.scenario('offline')
        self.assertIsNone(self.runtime.snapshot()['status']['climate'])

    def test_conflicting_thermal_status_is_rejected(self):
        packet = copy.deepcopy(self.runtime.status)
        packet['climate'].update(heater=True, cooler=True)
        with self.assertRaises(ValueError): parse_status(packet)
        packet = copy.deepcopy(self.runtime.status)
        packet['climate']['co2'] = float('nan')
        with self.assertRaises(ValueError): parse_status(packet)

    def test_visual_action_requires_browser_result(self):
        result = self.ask('识别选定照片', 1)
        self.assertEqual(result['client_action'], 'camera_analyze')
        self.assertTrue(result['trace'][0]['pending'])

    def result(self):
        return {'image_usable': True, 'mushroom_visible': True, 'stage': 'growing',
                'disease': 'suspected', 'observations': ['测试标注样本'], 'reason': '测试桩输出，不是实际识别'}

    def test_recognition_is_bound_to_photo_and_backup_keeps_record(self):
        identifier = 'a' * 32
        self.store.add_photo(identifier, 'image/png', b'test-fixture')
        service = VisionService(self.store, 'fake-model', lambda *args: self.result())
        record = service.analyze(identifier, '平菇')
        self.assertEqual(record['photo_id'], identifier)
        self.assertFalse(record['human_verified'])
        self.assertFalse(self.runtime.control.enabled)
        saved = json.loads((self.store.directory / 'vision' / (record['id'] + '.json')).read_text(encoding='utf-8'))
        self.assertEqual(saved['result'], self.result())
        info = backup(self.store.directory.parent, 'demo')
        with zipfile.ZipFile(info['vision_path']) as z:
            self.assertEqual(json.loads(z.read(record['id'] + '.json'))['id'], record['id'])

    def test_unclear_images_abstain_and_invalid_model_results_fail(self):
        value = validate_result({**self.result(), 'mushroom_visible': False})
        self.assertEqual((value['stage'], value['disease']), ('unknown', 'unknown'))
        for value in ({}, {**self.result(), 'stage': 'healthy'}, {**self.result(), 'image_usable': 1}):
            with self.assertRaises(ValueError): validate_result(value)
        with self.assertRaises(ValueError): VisionService(self.store).analyze('a' * 32, '平菇')
        with self.assertRaises(ValueError): VisionService(self.store, 'fake').analyze('a' * 32, '')

    def test_model_unavailable_has_actionable_error(self):
        import urllib.error
        for error, text in [(urllib.error.HTTPError('http://local', 404, 'missing', {}, None), '尚未就绪'),
                            (urllib.error.URLError('refused'), '无法连接'),
                            (TimeoutError(), '超时')]:
            with patch('room.vision.urllib.request.build_opener') as opener:
                opener.return_value.open.side_effect = error
                with self.assertRaisesRegex(ValueError, text): infer(b'fixture', '平菇', 'fake')

    def test_model_empty_final_or_truncation_not_replaced_by_thinking(self):
        for value in ({'done': True, 'message': {'content': '', 'thinking': json.dumps(self.result())}},
                      {'done': True, 'done_reason': 'length', 'message': {'content': json.dumps(self.result())}}):
            response = io.BytesIO(json.dumps(value).encode())
            with patch('room.vision.urllib.request.build_opener') as opener:
                opener.return_value.open.return_value = response
                with self.assertRaises(ValueError): infer(b'fixture', '平菇', 'fake')


if __name__ == '__main__': unittest.main()
